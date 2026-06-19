"""CLI entry point. `gua` 가 canonical CLI, `gpu-usage-audit` 는 compatibility CLI.

서브커맨드:
  daemon   실 NVIDIA NVML 텔레메트리를 SQLite 에 적재 (운영용, 백그라운드)
  start    daemon alias
  status   백그라운드 collector 상태 확인
  stop     백그라운드 collector 종료
  report   누적 DB 에서 §1~§5 retrospective 리포트
  demo     데모용 — fake telemetry 로 30 tick 적재 + 즉시 report (한 프로세스)
  version  버전 출력
  help     usage 출력

argparse stdlib 사용. NVML Python binding 은 실제 daemon/doctor probe 시점에 로드.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import __version__
from .cloud.client import CloudError, claim_enrollment, post_observation
from .cloud.config import (
    CloudConfig,
    CloudConfigError,
    load_cloud_config,
    save_cloud_config,
)
from .cloud.snapshot import (
    ERROR_NVML_INIT_FAILED,
    build_observation_payload,
    derive_collection_status,
)
from .daemon import install_signal_handlers, resolve_proc_identities, run_daemon
from .db import open_db, write_snapshot
from .doctor import build_doctor_report, doctor_report_to_dict, render_doctor
from .identity import system_process_name_lookup, system_user_lookup
from .model import HostMeta, Snapshot
from .nvml import NVMLNotAvailableError, NVMLTier
from .paths import (
    DEFAULT_CLOUD_CONFIG_PATH,
    DEFAULT_DB_PATH,
    DEFAULT_LOG_PATH,
    DEFAULT_PID_PATH,
    expand_path,
    is_default_db_path,
)
from .render import (
    render_headline,
    render_heatmap,
    render_idle_capacity,
    render_per_gpu,
    render_top_identities,
)
from .report import (
    load_headline,
    load_heatmap,
    load_host,
    load_idle_capacity,
    load_per_gpu,
    load_top_identities,
)
from .tier import FakeTier
from .usage_state import classify_usage_states

_DURATION_RE = re.compile(r"^(?P<v>\d+(?:\.\d+)?)(?P<u>ms|s|m|h|d)$")
_DURATION_UNITS = {
    "ms": "milliseconds",
    "s": "seconds",
    "m": "minutes",
    "h": "hours",
    "d": "days",
}
DISPLAY_COMMAND_ENV = "GPU_USAGE_AUDIT_DISPLAY_COMMAND"
LOCAL_ENV_KIND = "bare"
STARTUP_CHECK_SECONDS = 0.3


def _duration(s: str) -> timedelta:
    """argparse type: "30s", "1h", "200ms" → timedelta.

    지원 단위: ms, s, m, h, d. 상한 없음 — `--since 365d` 도 받음.
    """
    m = _DURATION_RE.match(s)
    if not m:
        raise argparse.ArgumentTypeError(f"invalid duration {s!r} (expected e.g. 30s, 1h, 200ms)")
    return timedelta(**{_DURATION_UNITS[m.group("u")]: float(m.group("v"))})


def build_parser() -> argparse.ArgumentParser:
    """argparse parser 구성. daemon (운영) / demo (학습) / report (조회) 의 분리."""
    parser = argparse.ArgumentParser(
        prog="gpu-usage-audit",
        description="Surface idle-held NVIDIA GPU memory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='Use "gpu-usage-audit <command> -h" for command-specific flags.',
    )
    parser.add_argument("--version", action="version", version=__version__)

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p_daemon = sub.add_parser(
        "daemon",
        help="Real NVML sampling into SQLite (long-running, NVIDIA host required)",
    )
    p_daemon.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help=f"Path to SQLite database file [default: {DEFAULT_DB_PATH}]",
    )
    p_daemon.add_argument(
        "--interval",
        type=_duration,
        default=timedelta(seconds=30),
        help="Tick interval (e.g. 30s, 1m, 200ms) [default: 30s]",
    )
    _add_cloud_args(p_daemon)
    p_daemon.set_defaults(func=_cmd_daemon)

    p_report = sub.add_parser(
        "report",
        help="Print §1–§5 retrospective report from an accumulated database",
    )
    p_report.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help=f"Path to SQLite database file [default: {DEFAULT_DB_PATH}]",
    )
    p_report.add_argument(
        "--since",
        type=_duration,
        default=timedelta(hours=1),
        help="Report window (e.g. 1h, 24h, 5m) [default: 1h]",
    )
    p_report.add_argument(
        "--interval",
        type=_duration,
        default=None,
        help=(
            "Override recorded daemon interval for §2 Idle capacity / §4 time conversion "
            "[default: read from DB; legacy rows fall back to 30s]"
        ),
    )
    p_report.add_argument(
        "--width",
        type=int,
        default=60,
        help="Width of the headline bar [default: 60]",
    )
    p_report.set_defaults(func=_cmd_report)

    p_demo = sub.add_parser(
        "demo",
        help="Run a self-contained demo with fake telemetry (no GPU required)",
    )
    p_demo.add_argument(
        "--db",
        default=None,
        help="SQLite database path [default: a fresh temporary file]",
    )
    p_demo.add_argument(
        "--ticks",
        type=int,
        default=30,
        help="Number of fake ticks to record before printing the report [default: 30]",
    )
    p_demo.add_argument(
        "--interval",
        type=_duration,
        default=timedelta(seconds=1),
        help="Tick interval for the fake daemon [default: 1s]",
    )
    p_demo.set_defaults(func=_cmd_demo)

    sub.add_parser("version", help="Print version")
    sub.add_parser("help", help="Show this message")

    return parser


def _add_daemon_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help=f"Path to SQLite database file [default: {DEFAULT_DB_PATH}]",
    )
    parser.add_argument(
        "--interval",
        type=_duration,
        default=timedelta(seconds=30),
        help="Tick interval (e.g. 30s, 1m, 200ms) [default: 30s]",
    )


def _add_cloud_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--cloud",
        action="store_true",
        help="After each tick, push the latest snapshot to GUA Board (requires `gua enroll`)",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CLOUD_CONFIG_PATH),
        help=f"Cloud config path (used with --cloud) [default: {DEFAULT_CLOUD_CONFIG_PATH}]",
    )


def _add_report_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help=f"Path to SQLite database file [default: {DEFAULT_DB_PATH}]",
    )
    parser.add_argument(
        "--since",
        type=_duration,
        default=timedelta(hours=1),
        help="Report window (e.g. 1h, 24h, 5m) [default: 1h]",
    )
    parser.add_argument(
        "--interval",
        type=_duration,
        default=None,
        help=(
            "Override recorded daemon interval for §2 Idle capacity / §4 time conversion "
            "[default: read from DB; legacy rows fall back to 30s]"
        ),
    )
    parser.add_argument(
        "--width",
        type=int,
        default=60,
        help="Width of the headline bar [default: 60]",
    )


def _add_demo_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        default=None,
        help="SQLite database path [default: a fresh temporary file]",
    )
    parser.add_argument(
        "--ticks",
        type=int,
        default=30,
        help="Number of fake ticks to record before printing the report [default: 30]",
    )
    parser.add_argument(
        "--interval",
        type=_duration,
        default=timedelta(seconds=1),
        help="Tick interval for the fake daemon [default: 1s]",
    )


def _add_runtime_file_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--pid-file",
        default=str(DEFAULT_PID_PATH),
        help=f"Background daemon PID file [default: {DEFAULT_PID_PATH}]",
    )
    parser.add_argument(
        "--log-file",
        default=str(DEFAULT_LOG_PATH),
        help=f"Background daemon log file [default: {DEFAULT_LOG_PATH}]",
    )


def build_gua_parser() -> argparse.ArgumentParser:
    """사용자용 `gua` command surface 구성."""
    parser = argparse.ArgumentParser(
        prog="gua",
        description="Audit local bare-metal NVIDIA GPU usage.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='Use "gua <command> -h" for command-specific flags.',
    )
    parser.add_argument("--version", action="version", version=__version__)

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p_doctor = sub.add_parser(
        "doctor",
        help="Check local NVIDIA/NVML readiness",
    )
    p_doctor.add_argument(
        "--json",
        action="store_true",
        help="Print the read-only doctor report as JSON",
    )
    p_doctor.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help=f"Path the daemon would write to [default: {DEFAULT_DB_PATH}]",
    )
    p_doctor.set_defaults(func=_cmd_gua_doctor)

    p_daemon = sub.add_parser(
        "daemon",
        help="Start the collector in the background",
    )
    _add_daemon_args(p_daemon)
    _add_runtime_file_args(p_daemon)
    _add_cloud_args(p_daemon)
    p_daemon.add_argument(
        "--foreground",
        action="store_true",
        help="Run in the foreground instead of starting a background process",
    )
    p_daemon.set_defaults(func=_cmd_gua_daemon)

    p_start = sub.add_parser(
        "start",
        help="Alias for `gua daemon`",
    )
    _add_daemon_args(p_start)
    _add_runtime_file_args(p_start)
    _add_cloud_args(p_start)
    p_start.set_defaults(func=_cmd_gua_start)

    p_status = sub.add_parser(
        "status",
        help="Show background collector status",
    )
    _add_runtime_file_args(p_status)
    p_status.set_defaults(func=_cmd_gua_status)

    p_stop = sub.add_parser(
        "stop",
        help="Stop the background collector",
    )
    _add_runtime_file_args(p_stop)
    p_stop.set_defaults(func=_cmd_gua_stop)

    p_report = sub.add_parser(
        "report",
        help="Print §1–§5 retrospective report",
    )
    _add_report_args(p_report)
    p_report.set_defaults(func=_cmd_report, display_command="gua report")

    p_demo = sub.add_parser(
        "demo",
        help="Run a self-contained demo with fake telemetry",
    )
    _add_demo_args(p_demo)
    p_demo.set_defaults(func=_cmd_demo)

    p_enroll = sub.add_parser(
        "enroll",
        help="Connect this host to a GUA Board workspace (optional cloud sync)",
    )
    p_enroll.add_argument(
        "--server-url",
        required=True,
        help="GUA Board base URL, e.g. https://board.example.com",
    )
    p_enroll.add_argument(
        "--enrollment-token",
        required=True,
        help="One-time enrollment token issued by the GUA Board web UI",
    )
    p_enroll.add_argument(
        "--config",
        default=str(DEFAULT_CLOUD_CONFIG_PATH),
        help=f"Cloud config path to write [default: {DEFAULT_CLOUD_CONFIG_PATH}]",
    )
    p_enroll.add_argument(
        "--hostname",
        default=None,
        help="Reported hostname [default: system hostname]",
    )
    p_enroll.add_argument(
        "--agent-version",
        default=__version__,
        help=f"Reported agent version [default: {__version__}]",
    )
    p_enroll.add_argument(
        "--driver-version",
        default=None,
        help="Reported NVIDIA driver version [default: detected via NVML if available]",
    )
    p_enroll.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing cloud config",
    )
    p_enroll.set_defaults(func=_cmd_gua_enroll)

    p_sync = sub.add_parser(
        "sync-once",
        help="Collect one snapshot and push the latest state to GUA Board",
    )
    p_sync.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help=f"Local SQLite history database [default: {DEFAULT_DB_PATH}]",
    )
    p_sync.add_argument(
        "--config",
        default=str(DEFAULT_CLOUD_CONFIG_PATH),
        help=f"Cloud config path [default: {DEFAULT_CLOUD_CONFIG_PATH}]",
    )
    p_sync.add_argument(
        "--fake",
        action="store_true",
        help="Use deterministic fake telemetry instead of NVML",
    )
    p_sync.set_defaults(func=_cmd_gua_sync_once)

    sub.add_parser("version", help="Print version")
    sub.add_parser("help", help="Show this message")

    return parser


def _cmd_gua_doctor(args: argparse.Namespace) -> int:
    """읽기 전용 로컬 호스트 readiness 진단을 출력한다."""
    report = build_doctor_report(db_path=args.db)
    if args.json:
        print(json.dumps(doctor_report_to_dict(report), indent=2, sort_keys=True))
        return 0
    print(render_doctor(report))
    return 0


def _probe_driver_version_or_none() -> str | None:
    """NVML 로 driver version 을 시도하되, GPU 없는 머신에서도 막지 않는다."""
    tier = NVMLTier()
    try:
        return tier.probe()
    except NVMLNotAvailableError:
        return None
    finally:
        tier.close()


def _cmd_gua_enroll(args: argparse.Namespace) -> int:
    """one-time enrollment token 을 claim 해 host-scoped agent token 을 저장한다."""
    config_path = expand_path(args.config)
    if config_path.exists() and not args.force:
        print(
            f"gua enroll: cloud config already exists: {config_path}; pass --force to overwrite",
            file=sys.stderr,
        )
        return 2

    hostname = args.hostname or socket.gethostname() or "unknown"
    driver_version = args.driver_version or _probe_driver_version_or_none()

    try:
        config = claim_enrollment(
            server_url=args.server_url,
            enrollment_token=args.enrollment_token,
            hostname=hostname,
            agent_version=args.agent_version,
            driver_version=driver_version,
        )
    except (CloudError, CloudConfigError) as exc:
        print(f"gua enroll: {exc}", file=sys.stderr)
        return 1

    # claim 은 성공했지만 저장에 실패하면 one-time enrollment token 은 이미 소비됨 —
    # 새 token 이 필요함을 분명히 알린다 (재시도 시 혼란 방지).
    try:
        saved = save_cloud_config(config, config_path, overwrite=args.force)
    except CloudConfigError as exc:
        print(
            f"gua enroll: enrollment succeeded but saving config failed: {exc}. "
            "The one-time enrollment token is now used; request a new one to retry.",
            file=sys.stderr,
        )
        return 1

    print(f"gua enroll: connected host {config.display_name} ({config.host_id})")
    print(f"  token prefix: {config.token_prefix}")
    print(f"  config: {saved}")
    print("  next: gua sync-once")
    return 0


def _cmd_gua_sync_once(args: argparse.Namespace) -> int:
    """한 틱을 수집해 local DB 에 기록한 뒤 latest snapshot 을 push 한다.

    순서 불변식: collect → local DB write → cloud push. push 실패는 이미
    커밋된 local write 를 되돌리지 않는다 (non-zero exit 로만 신호).
    """
    try:
        config = load_cloud_config(args.config)
    except CloudConfigError as exc:
        print(f"gua sync-once: {exc}", file=sys.stderr)
        return 2

    observed_at = datetime.now(UTC)
    hostname = socket.gethostname() or "unknown"
    process_list_unavailable = False
    if args.fake:
        tier = FakeTier()
        driver = tier.probe()
        snap = tier.collect(observed_at)
        classify_usage_states(snap, sync_once=True)
    else:
        nvml_tier = NVMLTier()
        try:
            try:
                driver = nvml_tier.probe()
            except NVMLNotAvailableError as exc:
                # 드라이버를 잃은 host 도 board 가 non-ok freshness 로 보여줄 수
                # 있게 error heartbeat 를 push 한다 (GPU inventory 없음, local
                # write 도 없음 — 기록할 데이터가 없다).
                return _push_error_heartbeat(config, hostname, observed_at, exc)
            snap = nvml_tier.collect(observed_at)
            process_list_unavailable = nvml_tier.last_process_list_unavailable
            resolve_proc_identities(snap.procs, system_user_lookup, system_process_name_lookup)
            classify_usage_states(snap, sync_once=True)
        finally:
            nvml_tier.close()

    collection_status, errors = derive_collection_status(
        snap, process_list_unavailable=process_list_unavailable
    )
    host = HostMeta(
        hostname=hostname,
        env_kind=LOCAL_ENV_KIND,
        driver_version=driver,
        first_seen=observed_at,
    )

    # local write 먼저 — cloud push 와 무관하게 history 를 보존한다.
    # one-shot sync 는 append-only 라 daemon 의 clobber 가드가 필요 없고, 부모
    # 디렉토리를 만들어 두어 user-supplied --db 경로도 바로 동작하게 한다.
    db_path = expand_path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(db_path)
    try:
        write_snapshot(conn, observed_at, host, snap)
    finally:
        conn.close()

    try:
        payload = build_observation_payload(
            snapshot=snap,
            hostname=hostname,
            driver_version=driver,
            agent_version=__version__,
            observed_at=observed_at,
            host_id=config.host_id,
            display_name=config.display_name,
            collection_status=collection_status,
            errors=errors,
        )
    except ValueError as exc:
        print(
            f"gua sync-once: local snapshot saved to {db_path}, "
            f"but could not build a valid payload: {exc}",
            file=sys.stderr,
        )
        return 1

    try:
        post_observation(config, payload)
    except CloudError as exc:
        print(
            f"gua sync-once: local snapshot saved to {db_path}, but cloud push failed: {exc}",
            file=sys.stderr,
        )
        return 1

    status_note = "" if collection_status == "ok" else f" [{collection_status}: {','.join(errors)}]"
    print(
        f"gua sync-once: pushed {len(payload['gpus'])} GPUs to "
        f"{config.display_name} ({config.server_url}){status_note}"
    )
    print(f"  local snapshot saved to {db_path}")
    return 0


def _push_error_heartbeat(
    config: CloudConfig,
    hostname: str,
    observed_at: datetime,
    exc: NVMLNotAvailableError,
) -> int:
    """NVML init 실패 시 board 에 `error` heartbeat 만 push 한다 (데이터 없음).

    GPU inventory 가 비어 있어 local DB 에 쓸 게 없으므로 push 만 한다. push
    실패는 비치명적 — 에러를 보고하고 non-zero exit 로 신호한다.
    """
    payload = build_observation_payload(
        snapshot=Snapshot(),
        hostname=hostname,
        driver_version="unknown",
        agent_version=__version__,
        observed_at=observed_at,
        host_id=config.host_id,
        display_name=config.display_name,
        collection_status="error",
        errors=[ERROR_NVML_INIT_FAILED],
    )
    try:
        post_observation(config, payload)
    except CloudError as push_exc:
        print(
            f"gua sync-once: {exc}; error heartbeat push also failed: {push_exc}",
            file=sys.stderr,
        )
        return 1
    print(
        f"gua sync-once: {exc}; pushed error heartbeat to "
        f"{config.display_name} ({config.server_url})",
        file=sys.stderr,
    )
    return 1


def _cmd_gua_daemon(args: argparse.Namespace) -> int:
    if args.foreground:
        args.display_command = "gua daemon --foreground"
        return _cmd_daemon(args)
    return _cmd_gua_start(args)


def _cmd_gua_start(args: argparse.Namespace) -> int:
    db_path = expand_path(args.db)
    pid_path = expand_path(args.pid_file)
    log_path = expand_path(args.log_file)

    existing_pid = _read_pid(pid_path)
    if existing_pid is not None:
        if _pid_alive(existing_pid) and _pid_is_managed_daemon(existing_pid):
            print(f"gua daemon: already running (pid {existing_pid})")
            return 0
        if _pid_alive(existing_pid):
            print(
                f"gua daemon: pid {existing_pid} belongs to another process; "
                "clearing stale pid file"
            )
        _unlink_if_exists(pid_path)

    if db_path.exists() and not is_default_db_path(db_path):
        print(
            f"gua daemon: {db_path} already exists; "
            "run `gua report --db PATH` for existing data or choose another --db path.",
            file=sys.stderr,
        )
        return 2

    pid_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "gpu_usage_audit",
        "daemon",
        "--db",
        str(db_path),
        "--interval",
        _duration_cli_value(args.interval),
    ]
    # cloud sync 옵션을 백그라운드 프로세스로 전파한다.
    if getattr(args, "cloud", False):
        command += ["--cloud", "--config", str(args.config)]
    env = os.environ.copy()
    env[DISPLAY_COMMAND_ENV] = "gua daemon --foreground"
    with log_path.open("ab") as log:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            env=env,
            start_new_session=True,
        )

    pid_path.write_text(f"{proc.pid}\n", encoding="utf-8")
    time.sleep(STARTUP_CHECK_SECONDS)
    rc = proc.poll()
    if rc is not None:
        _unlink_if_exists(pid_path)
        print(f"gua daemon: failed to start (exit {rc}); log: {log_path}", file=sys.stderr)
        tail = _tail_text(log_path)
        if tail:
            print(tail, file=sys.stderr)
        return rc or 1

    print(f"gua daemon: started pid {proc.pid}")
    print(f"  db: {db_path}")
    print(f"  log: {log_path}")
    print("  stop: gua stop")
    return 0


def _cmd_gua_status(args: argparse.Namespace) -> int:
    pid_path = expand_path(args.pid_file)
    log_path = expand_path(args.log_file)
    pid = _read_pid(pid_path)
    if pid is None:
        print("gua daemon: not running")
        return 0
    if _pid_alive(pid) and _pid_is_managed_daemon(pid):
        print(f"gua daemon: running (pid {pid})")
        print(f"  pid file: {pid_path}")
        print(f"  log: {log_path}")
        return 0
    if _pid_alive(pid):
        _unlink_if_exists(pid_path)
        print(
            f"gua daemon: not running (pid {pid} belongs to another process; "
            "cleared stale pid file)"
        )
    else:
        print(f"gua daemon: not running (stale pid {pid})")
        _unlink_if_exists(pid_path)
    return 0


def _cmd_gua_stop(args: argparse.Namespace) -> int:
    pid_path = expand_path(args.pid_file)
    pid = _read_pid(pid_path)
    if pid is None:
        print("gua daemon: not running")
        return 0
    if not _pid_alive(pid):
        _unlink_if_exists(pid_path)
        print(f"gua daemon: not running (removed stale pid {pid})")
        return 0
    if not _pid_is_managed_daemon(pid):
        _unlink_if_exists(pid_path)
        print(
            f"gua daemon: not running (pid {pid} belongs to another process; "
            "cleared stale pid file)"
        )
        return 0

    # The identity check above closes the common stale-PID-file case. A tiny
    # check-then-kill race remains if the process exits and the OS reuses the
    # PID before SIGTERM; avoiding that needs a stronger lock model.
    try:
        os.kill(pid, signal.SIGTERM)
    except PermissionError:
        print(f"gua daemon: permission denied stopping pid {pid}", file=sys.stderr)
        return 1
    except ProcessLookupError:
        _unlink_if_exists(pid_path)
        print(f"gua daemon: not running (removed stale pid {pid})")
        return 0

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            _unlink_if_exists(pid_path)
            print(f"gua daemon: stopped pid {pid}")
            return 0
        time.sleep(0.1)

    print(f"gua daemon: sent SIGTERM to pid {pid}, but it is still running", file=sys.stderr)
    return 1


def _cmd_daemon(args: argparse.Namespace) -> int:
    """실 NVML 데몬 — 운영용."""
    display_command = getattr(
        args,
        "display_command",
        os.environ.get(DISPLAY_COMMAND_ENV, "gpu-usage-audit daemon"),
    )
    db_path = expand_path(args.db)
    if db_path.exists() and not is_default_db_path(db_path):
        print(
            f"{display_command}: {db_path} already exists; "
            "choose another --db path or remove the existing file before starting.",
            file=sys.stderr,
        )
        return 2
    if is_default_db_path(db_path):
        db_path.parent.mkdir(parents=True, exist_ok=True)

    # cloud sync 가 켜졌으면 enroll 설정을 *먼저* 검증한다(미enroll 이면 NVML 열기 전에 중단).
    cloud_config = None
    if getattr(args, "cloud", False):
        try:
            cloud_config = load_cloud_config(args.config)
        except CloudConfigError as exc:
            print(f"{display_command}: {exc}", file=sys.stderr)
            return 2

    tier = NVMLTier()
    try:
        try:
            driver = tier.probe()
        except NVMLNotAvailableError as e:
            print(f"{display_command}: {e}", file=sys.stderr)
            return 1
        conn = open_db(db_path)
        try:
            hostname = socket.gethostname() or "unknown"
            host = HostMeta(
                hostname=hostname,
                env_kind=LOCAL_ENV_KIND,
                driver_version=driver,
                first_seen=datetime.now(UTC),
            )

            # cloud on_tick 후크: local write 이후 latest snapshot 을 board 로 push.
            # 빌드/푸시 실패는 daemon._tick 이 잡아 로그만 남기고 다음 틱을 계속한다.
            on_tick = None
            if cloud_config is not None:

                def push_snapshot(snap: Snapshot, ts: datetime) -> None:
                    # on_tick 은 같은 데몬 스레드에서 tier.collect() 직후 동기로
                    # 호출되므로, tier 의 last-collect 상태가 이 snap 과 일치한다.
                    collection_status, errors = derive_collection_status(
                        snap, process_list_unavailable=tier.last_process_list_unavailable
                    )
                    payload = build_observation_payload(
                        snapshot=snap,
                        hostname=hostname,
                        driver_version=driver,
                        agent_version=__version__,
                        observed_at=ts,
                        host_id=cloud_config.host_id,
                        display_name=cloud_config.display_name,
                        collection_status=collection_status,
                        errors=errors,
                    )
                    post_observation(cloud_config, payload)

                on_tick = push_snapshot
                print(
                    f"{display_command}: cloud sync enabled -> "
                    f"{cloud_config.display_name} ({cloud_config.server_url})"
                )

            stop = threading.Event()
            install_signal_handlers(stop)
            run_daemon(
                tier=tier,
                db=conn,
                host=host,
                interval=args.interval,
                lookup=system_user_lookup,
                name_lookup=system_process_name_lookup,
                stop=stop,
                on_tick=on_tick,
            )
            total = conn.execute("SELECT COUNT(*) FROM gpu_sample").fetchone()[0]
            print(f"\n{args.db}: {total} total gpu_sample rows")
            return 0
        finally:
            conn.close()
    finally:
        tier.close()


def _cmd_report(args: argparse.Namespace) -> int:
    display_command = getattr(args, "display_command", "gpu-usage-audit report")
    db_path = expand_path(args.db)
    if not db_path.exists():
        print(
            f"{display_command}: {db_path} does not exist; "
            "run `gua daemon` first or pass --db PATH.",
            file=sys.stderr,
        )
        return 2
    conn = open_db(db_path)
    try:
        cutoff = datetime.now(UTC) - args.since
        host = load_host(conn)
        headline = load_headline(conn, cutoff)
        idle_capacity = load_idle_capacity(conn, cutoff, args.interval)
        per_gpu = load_per_gpu(conn, cutoff)
        top = load_top_identities(conn, cutoff, args.interval)
        heat = load_heatmap(conn, cutoff)
        render_headline(sys.stdout, host, headline, args.since, args.width)
        render_idle_capacity(sys.stdout, idle_capacity)
        render_per_gpu(sys.stdout, per_gpu)
        render_top_identities(sys.stdout, top)
        render_heatmap(sys.stdout, heat)
        return 0
    finally:
        conn.close()


def _cmd_demo(args: argparse.Namespace) -> int:
    """데모 — fake telemetry 로 한 프로세스 안에서 daemon + report.

    GPU 없는 머신에서 *형식과 동선* 을 보여주는 1분짜리 셀프 컨테인드
    시나리오. 임시 DB 자동 생성 (또는 --db 명시), N tick 적재 후 즉시
    report 출력하고 종료.
    """
    if args.db is None:
        tmpdir = tempfile.mkdtemp(prefix="gpu-usage-audit-demo-")
        db_path = str(Path(tmpdir) / "demo.db")
        print(f"(using temporary database: {db_path})", file=sys.stderr)
    else:
        db_path = str(expand_path(args.db))

    conn = open_db(db_path)
    try:
        tier = FakeTier()
        driver = tier.probe()
        host = HostMeta(
            hostname=socket.gethostname() or "unknown",
            env_kind=LOCAL_ENV_KIND,
            driver_version=driver,
            first_seen=datetime.now(UTC),
        )

        # 데몬 단계: 결정적 fake 데이터를 N tick 적재. signal handler 안 박는다 —
        # demo 는 *짧고 자동 종료* 가 의도라 SIGINT 도 그냥 KeyboardInterrupt 로.
        print(
            f"# Recording {args.ticks} fake ticks at {args.interval} interval...",
            file=sys.stderr,
        )
        # demo 는 system user lookup 안 함 — FakeTier 가 식별자 미리 박음.
        run_daemon(
            tier=tier,
            db=conn,
            host=host,
            interval=args.interval,
            max_ticks=args.ticks,
            out=sys.stderr,
        )

        # 리포트 단계: 데이터 *전체* 윈도우 (since 를 충분히 크게).
        print(file=sys.stderr)
        window = max(args.interval * args.ticks * 2, timedelta(minutes=1))
        cutoff = datetime.now(UTC) - window
        loaded_host = load_host(conn)
        render_headline(sys.stdout, loaded_host, load_headline(conn, cutoff), window, width=60)
        render_idle_capacity(sys.stdout, load_idle_capacity(conn, cutoff, args.interval))
        render_per_gpu(sys.stdout, load_per_gpu(conn, cutoff))
        render_top_identities(sys.stdout, load_top_identities(conn, cutoff, args.interval))
        render_heatmap(sys.stdout, load_heatmap(conn, cutoff))
        return 0
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    """Entry point. argv=None 이면 sys.argv 사용."""
    _configure_logging()

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "version":
        print(__version__)
        return 0
    if args.command == "help":
        parser.print_help()
        return 0
    if hasattr(args, "func"):
        result: int = args.func(args)
        return result

    parser.print_help(sys.stderr)
    return 2


def gua_main(argv: list[str] | None = None) -> int:
    """새 `gua` command surface entry point."""
    _configure_logging()
    parser = build_gua_parser()
    args = parser.parse_args(argv)

    if args.command == "version":
        print(__version__)
        return 0
    if args.command == "help":
        parser.print_help()
        return 0
    if hasattr(args, "func"):
        result: int = args.func(args)
        return result

    parser.print_help(sys.stderr)
    return 2


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%Y/%m/%d %H:%M:%S",
        stream=sys.stderr,
    )


def _duration_cli_value(value: timedelta) -> str:
    seconds = value.total_seconds()
    milliseconds = seconds * 1000
    if 0 < milliseconds < 1000 and milliseconds.is_integer():
        return f"{int(milliseconds)}ms"
    return f"{seconds:g}s"


def _read_pid(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        pid = int(raw)
    except ValueError:
        return None
    return pid if pid > 0 else None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_is_managed_daemon(pid: int) -> bool:
    """Return True for the subprocess shape created by `_cmd_gua_start`.

    Keep this in sync with the spawn command in `_cmd_gua_start`; status/stop
    use it to avoid acting on unrelated processes from stale PID files.
    """
    args = _read_proc_cmdline(pid)
    for i, arg in enumerate(args):
        if arg == "-m" and args[i + 1 : i + 3] == ["gpu_usage_audit", "daemon"]:
            return True
    return False


def _read_proc_cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]


def _unlink_if_exists(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


def _tail_text(path: Path, *, max_lines: int = 12) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-max_lines:])


if __name__ == "__main__":
    raise SystemExit(main())

"""CLI entry point. `python -m gpu_usage_audit` 와 `uvx gpu-usage-audit` 둘 다 여기로.

서브커맨드:
  daemon   실 NVIDIA NVML 텔레메트리를 SQLite 에 적재 (운영용, 백그라운드)
  report   누적 DB 에서 §1~§5 retrospective 리포트
  demo     데모용 — fake telemetry 로 30 tick 적재 + 즉시 report (한 프로세스)
  version  버전 출력
  help     usage 출력

argparse stdlib 사용 — 의존성 0.
"""

from __future__ import annotations

import argparse
import logging
import re
import socket
import sys
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import __version__
from .daemon import install_signal_handlers, run_daemon
from .db import open_db
from .env import detect_env_kind
from .identity import system_user_lookup
from .model import HostMeta
from .nvml import NVMLNotAvailableError, NVMLTier
from .render import (
    render_headline,
    render_heatmap,
    render_per_gpu,
    render_top_identities,
    render_waste,
)
from .report import (
    load_headline,
    load_heatmap,
    load_host,
    load_per_gpu,
    load_top_identities,
    load_waste,
)
from .tier import FakeTier

_DURATION_RE = re.compile(r"^(?P<v>\d+(?:\.\d+)?)(?P<u>ms|s|m|h|d)$")
_DURATION_UNITS = {
    "ms": "milliseconds",
    "s": "seconds",
    "m": "minutes",
    "h": "hours",
    "d": "days",
}
_NO_SYSTEM_CHANGES = "No system, service, cluster, or database changes were made."


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
    p_daemon.add_argument("--db", required=True, help="Path to SQLite database file")
    p_daemon.add_argument(
        "--interval",
        type=_duration,
        default=timedelta(seconds=30),
        help="Tick interval (e.g. 30s, 1m, 200ms) [default: 30s]",
    )
    p_daemon.set_defaults(func=_cmd_daemon)

    p_report = sub.add_parser(
        "report",
        help="Print §1–§5 retrospective report from an accumulated database",
    )
    p_report.add_argument("--db", required=True, help="Path to SQLite database file")
    p_report.add_argument(
        "--since",
        type=_duration,
        default=timedelta(hours=1),
        help="Report window (e.g. 1h, 24h, 5m) [default: 1h]",
    )
    p_report.add_argument(
        "--interval",
        type=_duration,
        default=timedelta(seconds=30),
        help="Daemon tick interval — for §2 Waste / §4 time conversion [default: 30s]",
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


def build_gua_parser() -> argparse.ArgumentParser:
    """New auto-runtime command surface skeleton.

    The existing `gpu-usage-audit daemon/report/demo` path remains the
    compatibility CLI while `gua` grows the auto-runtime workflow.
    """
    parser = argparse.ArgumentParser(
        prog="gua",
        description="Auto-runtime command surface for gpu-usage-audit.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='Use "gua <command> -h" for command-specific flags.',
    )
    parser.add_argument("--version", action="version", version=__version__)

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p_doctor = sub.add_parser(
        "doctor",
        help="Inspect the host and recommend a runtime plan (placeholder)",
    )
    p_doctor.set_defaults(func=_cmd_gua_doctor)

    p_start = sub.add_parser(
        "start",
        help="Start a managed collector runtime (dry-run skeleton only)",
    )
    p_start.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the placeholder start path without changing system state",
    )
    p_start.set_defaults(func=_cmd_gua_start)

    p_status = sub.add_parser(
        "status",
        help="Show managed runtime status (placeholder)",
    )
    p_status.set_defaults(func=_cmd_gua_status)

    p_report = sub.add_parser(
        "report",
        help="Show a state-aware audit report (placeholder)",
    )
    p_report.set_defaults(func=_cmd_gua_report)

    p_stop = sub.add_parser(
        "stop",
        help="Stop a managed collector runtime (placeholder)",
    )
    p_stop.set_defaults(func=_cmd_gua_stop)

    p_uninstall = sub.add_parser(
        "uninstall",
        help="Remove managed runtime artifacts (placeholder)",
    )
    p_uninstall.set_defaults(func=_cmd_gua_uninstall)

    return parser


def _print_gua_placeholder(command: str, detail: str) -> int:
    print(f"gua {command}: {detail}")
    print(_NO_SYSTEM_CHANGES)
    return 0


def _cmd_gua_doctor(args: argparse.Namespace) -> int:
    return _print_gua_placeholder(
        args.command,
        "runtime detection is not implemented yet; this skeleton ran no checks.",
    )


def _cmd_gua_start(args: argparse.Namespace) -> int:
    if not args.dry_run:
        print("gua start: only `gua start --dry-run` is available in this skeleton.", file=sys.stderr)
        print(_NO_SYSTEM_CHANGES, file=sys.stderr)
        return 2
    return _print_gua_placeholder(
        "start --dry-run",
        "runtime planning is not implemented yet; no start plan was applied.",
    )


def _cmd_gua_status(args: argparse.Namespace) -> int:
    return _print_gua_placeholder(
        args.command,
        "install-state tracking is not implemented yet; no managed runtime is known.",
    )


def _cmd_gua_report(args: argparse.Namespace) -> int:
    return _print_gua_placeholder(
        args.command,
        "state-aware reporting is not implemented yet; use `gpu-usage-audit report --db PATH`.",
    )


def _cmd_gua_stop(args: argparse.Namespace) -> int:
    return _print_gua_placeholder(
        args.command,
        "runtime management is not implemented yet; no managed runtime was stopped.",
    )


def _cmd_gua_uninstall(args: argparse.Namespace) -> int:
    return _print_gua_placeholder(
        args.command,
        "runtime cleanup is not implemented yet; no files or runtime artifacts were removed.",
    )


def _cmd_daemon(args: argparse.Namespace) -> int:
    """실 NVML 데몬 — 운영용."""
    conn = open_db(args.db)
    tier = NVMLTier()
    try:
        try:
            driver = tier.probe()
        except NVMLNotAvailableError as e:
            print(f"gpu-usage-audit daemon: {e}", file=sys.stderr)
            return 1
        host = HostMeta(
            hostname=socket.gethostname() or "unknown",
            env_kind=detect_env_kind("/proc"),
            driver_version=driver,
            first_seen=datetime.now(UTC),
        )
        stop = threading.Event()
        install_signal_handlers(stop)
        run_daemon(
            tier=tier,
            db=conn,
            host=host,
            interval=args.interval,
            lookup=system_user_lookup,
            stop=stop,
        )
        total = conn.execute("SELECT COUNT(*) FROM gpu_sample").fetchone()[0]
        print(f"\n{args.db}: {total} total gpu_sample rows")
        return 0
    finally:
        tier.close()
        conn.close()


def _cmd_report(args: argparse.Namespace) -> int:
    conn = open_db(args.db)
    try:
        cutoff = datetime.now(UTC) - args.since
        host = load_host(conn)
        headline = load_headline(conn, cutoff)
        waste = load_waste(conn, cutoff, args.interval)
        per_gpu = load_per_gpu(conn, cutoff)
        top = load_top_identities(conn, cutoff, args.interval)
        heat = load_heatmap(conn, cutoff)
        render_headline(sys.stdout, host, headline, args.since, args.width)
        render_waste(sys.stdout, waste)
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
        db_path = args.db

    conn = open_db(db_path)
    try:
        tier = FakeTier()
        driver = tier.probe()
        host = HostMeta(
            hostname=socket.gethostname() or "unknown",
            env_kind=detect_env_kind("/proc"),
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
        render_waste(sys.stdout, load_waste(conn, cutoff, args.interval))
        render_per_gpu(sys.stdout, load_per_gpu(conn, cutoff))
        render_top_identities(sys.stdout, load_top_identities(conn, cutoff, args.interval))
        render_heatmap(sys.stdout, load_heatmap(conn, cutoff))
        return 0
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    """Entry point. argv=None 이면 sys.argv 사용."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%Y/%m/%d %H:%M:%S",
        stream=sys.stderr,
    )

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
    """Entry point for the new `gua` command surface."""
    parser = build_gua_parser()
    args = parser.parse_args(argv)

    if hasattr(args, "func"):
        result: int = args.func(args)
        return result

    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

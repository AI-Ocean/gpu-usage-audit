"""CLI entry point. `python -m gpu_usage_audit` 와 `uvx gpu-usage-audit` 둘 다 여기로.

서브커맨드:
  daemon   실 NVIDIA NVML 텔레메트리를 SQLite 에 적재 (운영용, 백그라운드)
  report   누적 DB 에서 §1~§5 retrospective 리포트
  demo     데모용 — fake telemetry 로 30 tick 적재 + 즉시 report (한 프로세스)
  version  버전 출력
  help     usage 출력

argparse stdlib 사용. NVML Python binding 은 실제 daemon/doctor probe 시점에 로드.
"""

from __future__ import annotations

import argparse
import json
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
from .doctor import (
    DEFAULT_DB_PATH as DOCTOR_DEFAULT_DB_PATH,
)
from .doctor import (
    build_doctor_report,
    doctor_report_to_dict,
    render_doctor,
)
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
DEFAULT_DB_PATH = DOCTOR_DEFAULT_DB_PATH


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
        help=f"Path to a new SQLite database file [default: {DEFAULT_DB_PATH}]",
    )
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
    """로컬 bare-metal `gua` command surface 구성."""
    parser = argparse.ArgumentParser(
        prog="gua",
        description="Local bare-metal readiness checks for gpu-usage-audit.",
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

    return parser


def _cmd_gua_doctor(args: argparse.Namespace) -> int:
    """읽기 전용 로컬 호스트 readiness 진단을 출력한다."""
    report = build_doctor_report(db_path=args.db)
    if args.json:
        print(json.dumps(doctor_report_to_dict(report), indent=2, sort_keys=True))
        return 0
    print(render_doctor(report))
    return 0


def _cmd_daemon(args: argparse.Namespace) -> int:
    """실 NVML 데몬 — 운영용."""
    db_path = Path(args.db)
    if db_path.exists():
        print(
            f"gpu-usage-audit daemon: {db_path} already exists; "
            "choose another --db path or remove the existing file before starting.",
            file=sys.stderr,
        )
        return 2
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
    db_path = Path(args.db)
    if not db_path.exists():
        print(
            f"gpu-usage-audit report: {db_path} does not exist; "
            "run `gpu-usage-audit daemon` first or pass --db PATH.",
            file=sys.stderr,
        )
        return 2
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
    """새 `gua` command surface entry point."""
    parser = build_gua_parser()
    args = parser.parse_args(argv)

    if hasattr(args, "func"):
        result: int = args.func(args)
        return result

    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""CLI entry point. `python -m gpu_usage_audit` 와 `uvx gpu-usage-audit` 둘 다 여기로.

서브커맨드 라인업 (v0.2.0a1):
  daemon   FakeTier 위에서 한 틱씩 SQLite 에 적재
  report   누적 DB 에서 §1~§5 retrospective 리포트
  version  버전 출력
  help     usage 출력

argparse stdlib 사용 — Go flag 와 동등한 라인업, 의존성 0.
"""

from __future__ import annotations

import argparse
import logging
import re
import socket
import sys
import threading
from datetime import UTC, datetime, timedelta

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
from .tier import FakeTier, Tier

_DURATION_RE = re.compile(r"^(?P<v>\d+(?:\.\d+)?)(?P<u>ms|s|m|h|d)$")
_DURATION_UNITS = {
    "ms": "milliseconds",
    "s": "seconds",
    "m": "minutes",
    "h": "hours",
    "d": "days",
}


def _duration(s: str) -> timedelta:
    """argparse type: "30s", "1h", "200ms" → timedelta.

    Go 의 time.ParseDuration 부분집합 — v0.1.0 의 인터페이스 유지.
    """
    m = _DURATION_RE.match(s)
    if not m:
        raise argparse.ArgumentTypeError(f"invalid duration {s!r} (expected e.g. 30s, 1h, 200ms)")
    return timedelta(**{_DURATION_UNITS[m.group("u")]: float(m.group("v"))})


def build_parser() -> argparse.ArgumentParser:
    """argparse parser 구성. Go 의 flag.NewFlagSet 디스패처와 동등 의도."""
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
        help="Sample GPU/process telemetry into SQLite at a fixed interval",
    )
    p_daemon.add_argument("--db", required=True, help="Path to SQLite database file")
    p_daemon.add_argument(
        "--interval",
        type=_duration,
        default=timedelta(seconds=30),
        help="Tick interval (e.g. 30s, 1m, 200ms) [default: 30s]",
    )
    p_daemon.add_argument(
        "--tier",
        choices=("fake", "nvml"),
        default="fake",
        help="Telemetry source: 'fake' (deterministic stub, default) "
        "or 'nvml' (real NVIDIA driver — requires [nvml] extra)",
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

    sub.add_parser("version", help="Print version")
    sub.add_parser("help", help="Show this message")

    return parser


def _make_tier(kind: str) -> Tier:
    """--tier 선택 → Tier 인스턴스. NVML 실패 시 명확한 stderr 메시지."""
    if kind == "fake":
        return FakeTier()
    if kind == "nvml":
        return NVMLTier()
    raise ValueError(f"unknown tier: {kind!r}")


def _cmd_daemon(args: argparse.Namespace) -> int:
    conn = open_db(args.db)
    tier = _make_tier(args.tier)
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
        # NVMLTier 인 경우 nvmlShutdown 까지 호출하려면 close 가 필요.
        # FakeTier 는 close 가 없어도 무방 — hasattr 로 polymorphic.
        if hasattr(tier, "close"):
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


def main(argv: list[str] | None = None) -> int:
    """Entry point. argv=None 이면 sys.argv 사용.

    logging 은 INFO 레벨로 stderr 출력 — Go log.Printf 와 같은 동선.
    """
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


if __name__ == "__main__":
    raise SystemExit(main())

"""CLI entry point. `python -m gpu_usage_audit` 와 `uvx gpu-usage-audit` 둘 다 여기로.

현재 (v0.2.0a0) 는 *스캐폴드* 상태 — Go v0.1.0 의 daemon/report 명령을
Python 으로 옮기는 작업이 진행 중. version subcommand 만 동작.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__

USAGE_EPILOG = (
    'Use "gpu-usage-audit <command> -h" for command-specific flags.\n'
    "\n"
    "Note: v0.2.0a0 ships only `version` while the daemon/report commands\n"
    "are being ported from the Go v0.1.0 implementation."
)


def build_parser() -> argparse.ArgumentParser:
    """argparse parser 구성. Go 의 flag.NewFlagSet 디스패처와 동등 의도."""
    parser = argparse.ArgumentParser(
        prog="gpu-usage-audit",
        description="Surface idle-held NVIDIA GPU memory.",
        epilog=USAGE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=__version__)

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # version subcommand — `gpu-usage-audit version` 도 동작하게.
    # argparse 의 --version 만으로는 subcommand 형태를 못 잡아서 별도 등록.
    sub.add_parser("version", help="Print version")

    # 향후 등록 자리:
    # sub.add_parser("daemon", ...)
    # sub.add_parser("report", ...)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. argv=None 이면 sys.argv 사용."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "version":
        print(__version__)
        return 0

    # 인자 없이 호출되면 usage 를 stderr 로.
    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

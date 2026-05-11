"""CLI scaffold + duration parsing 통합 smoke 테스트.

세부 동작 (분류/렌더/DB) 은 각 모듈 테스트에 위임. 이 파일은 CLI
entry point + argparse 구조 + duration 파서가 *살아있는지* 만 짚는다.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from gpu_usage_audit import __version__
from gpu_usage_audit.__main__ import _duration, build_parser, main


def test_version_string_is_nonempty() -> None:
    assert __version__
    assert isinstance(__version__, str)


def test_parser_registers_subcommands() -> None:
    p = build_parser()
    # 알려진 subcommands 모두 등록됐는지.
    for cmd in ("daemon", "report", "demo", "version", "help"):
        ns = p.parse_args([cmd, *_required_args_for(cmd)])
        assert ns.command == cmd


def _required_args_for(cmd: str) -> list[str]:
    # daemon/report 는 --db 필수. demo 는 --db 옵셔널. version/help 는 추가 인자 없음.
    if cmd in ("daemon", "report"):
        return ["--db", "/tmp/dummy.db"]
    return []


def test_main_version_command_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["version"])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out == __version__


def test_main_help_command_prints_usage(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["help"])
    assert rc == 0
    assert "usage:" in capsys.readouterr().out.lower()


def test_main_no_args_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main([])
    assert rc == 2
    assert "usage:" in capsys.readouterr().err.lower()


def test_cli_entry_point_runs_in_subprocess() -> None:
    # installed entry point `gpu-usage-audit` 와 `python -m` 둘 다 동일 main().
    result = subprocess.run(
        [sys.executable, "-m", "gpu_usage_audit", "version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == __version__


def test_demo_command_records_and_prints_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # demo: 작은 ticks 로 self-contained 시연. stdout 에 §1~§5 등장,
    # DB 에 sample 적재 확인.
    db_path = tmp_path / "demo.db"
    rc = main(
        [
            "demo",
            "--db",
            str(db_path),
            "--ticks",
            "3",
            "--interval",
            "10ms",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0
    # §1~§5 다 등장.
    for section in ("§1 Headline", "§2 Waste", "§3 Per-GPU", "§4 Top identities", "§5"):
        assert section in captured.out, f"{section} not in demo output"
    # DB 파일 생성됐는지.
    assert db_path.exists()


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("30s", timedelta(seconds=30)),
        ("1h", timedelta(hours=1)),
        ("200ms", timedelta(milliseconds=200)),
        ("0.5m", timedelta(seconds=30)),
        ("2d", timedelta(days=2)),
        # 상한 없음 — 365d 도 OK.
        ("365d", timedelta(days=365)),
    ],
)
def test_duration_parser_valid(text: str, want: timedelta) -> None:
    assert _duration(text) == want


@pytest.mark.parametrize("bad", ["30", "h", "1y", "1.5", "1 s", "", "1w"])
def test_duration_parser_invalid(bad: str) -> None:
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        _duration(bad)

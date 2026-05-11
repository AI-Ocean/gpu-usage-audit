"""Scaffold smoke 테스트. v0.2.0a0 시점에 import + CLI 형태가 살아있나 확인.

번역이 진행되면서 *진짜 단위 테스트* 가 다른 test_*.py 파일들로 추가됨.
이 파일은 scaffold 가 깨지지 않았다는 최소 보증.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from gpu_usage_audit import __version__
from gpu_usage_audit.__main__ import build_parser, main


def test_version_string_is_nonempty() -> None:
    assert __version__
    assert isinstance(__version__, str)


def test_parser_constructs() -> None:
    p = build_parser()
    # version 서브커맨드 등록 확인.
    ns = p.parse_args(["version"])
    assert ns.command == "version"


def test_main_version_command_prints_version(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["version"])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out == __version__


def test_main_no_args_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    # Go 의 v0.1.0 도 인자 없을 때 exit 2 + usage 출력. 동일 동작 유지.
    # argparse 가 만드는 usage 문은 소문자 "usage:" 로 시작하므로
    # 대소문자 무시로 검사.
    rc = main([])
    assert rc == 2
    assert "usage:" in capsys.readouterr().err.lower()


def test_cli_entry_point_runs() -> None:
    # 모듈 실행 형태로 외부 프로세스에서도 동작하는지.
    # (installed entry point `gpu-usage-audit` 도 동일 main() 호출.)
    result = subprocess.run(
        [sys.executable, "-m", "gpu_usage_audit", "version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == __version__

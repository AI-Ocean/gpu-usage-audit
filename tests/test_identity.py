"""identity._parse_loginuid 단위 테스트.

system_user_lookup 자체는 pwd.getpwuid 의 시스템 의존이 있어 e2e
테스트 없이 두지만 — `_parse_loginuid` 가 그 안의 의사결정 대부분을
담고 있어 단위 테스트로 충분.
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path

import pytest

from gpu_usage_audit.identity import (
    LOGIN_UID_UNSET,
    _parse_loginuid,
    system_owner_lookup,
    system_process_name_lookup,
    system_user_lookup,
)


def test_system_owner_lookup_reads_dir_owner(tmp_path: Path) -> None:
    # /proc/<pid> 대역 디렉토리는 테스트 유저 소유 → 그 유저명이 나와야.
    (tmp_path / "123").mkdir()
    want = pwd.getpwuid(os.getuid()).pw_name
    assert system_owner_lookup(123, proc_root=tmp_path) == want


def test_system_owner_lookup_missing_pid_is_none(tmp_path: Path) -> None:
    assert system_owner_lookup(999, proc_root=tmp_path) is None


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("0\n", 0),
        ("1000", 1000),
        ("  1000  \n", 1000),
        (str(LOGIN_UID_UNSET) + "\n", None),  # UNSET sentinel
        ("", None),
        ("   \n", None),
        ("notanumber", None),
    ],
)
def test_parse_loginuid(raw: str, want: int | None) -> None:
    assert _parse_loginuid(raw) == want


def test_system_user_lookup_missing_proc_returns_none(tmp_path: object) -> None:
    # proc_root 는 존재하지만 PID 디렉토리/파일 없음 → None.
    assert system_user_lookup(99999, proc_root=tmp_path) is None  # type: ignore[arg-type]


def test_system_process_name_lookup_reads_comm(tmp_path: Path) -> None:
    pid_dir = tmp_path / "4242"
    pid_dir.mkdir()
    (pid_dir / "comm").write_text("python\n")
    assert system_process_name_lookup(4242, proc_root=tmp_path) == "python"


def test_system_process_name_lookup_missing_or_blank_returns_none(tmp_path: Path) -> None:
    assert system_process_name_lookup(99999, proc_root=tmp_path) is None
    blank = tmp_path / "1" / "comm"
    blank.parent.mkdir()
    blank.write_text("  \n")
    assert system_process_name_lookup(1, proc_root=tmp_path) is None

"""identity._parse_loginuid 단위 테스트.

system_user_lookup 자체는 pwd.getpwuid 의 시스템 의존이 있어 e2e
테스트 없이 두지만 — `_parse_loginuid` 가 그 안의 의사결정 대부분을
담고 있어 단위 테스트로 충분.
"""

from __future__ import annotations

import pytest

from gpu_usage_audit.identity import LOGIN_UID_UNSET, _parse_loginuid, system_user_lookup


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

"""Classify 테이블 드리븐 테스트. Go v0.1.0 의 TestClassify 와 동일 케이스."""

from __future__ import annotations

import pytest

from gpu_usage_audit.classify import Class, Sample, classify


@pytest.mark.parametrize(
    ("name", "util", "mem", "want"),
    [
        ("util 정확히 임계", 10, 0, Class.ACTIVE),
        ("util 임계 직전", 9, 0, Class.TRULY_IDLE),
        ("util 임계 위 + 메모리 큼", 80, 70000, Class.ACTIVE),
        ("util 낮음 + 메모리 임계 위", 2, 101, Class.IDLE_HELD),
        ("util 낮음 + 메모리 정확히 임계", 2, 100, Class.TRULY_IDLE),
        ("util 0 + 메모리 0", 0, 0, Class.TRULY_IDLE),
        ("util 음수 — 방어적", -1, 0, Class.TRULY_IDLE),
    ],
)
def test_classify(name: str, util: int, mem: int, want: Class) -> None:
    got = classify(Sample(util_pct=util, proc_mem_mb=mem))
    assert got == want, f"{name}: classify(util={util}, mem={mem}) = {got}, want {want}"


def test_class_str_values_stable() -> None:
    # v0.1.0 (Go) 와 *DB/로그 호환* 을 위해 문자열 값은 변경 금지.
    assert Class.ACTIVE.value == "active"
    assert Class.IDLE_HELD.value == "idle-held"
    assert Class.TRULY_IDLE.value == "truly-idle"

"""FakeTier 결정성 + phase 사이클 테스트.

Go v0.1.0 의 TestFakeTier_phaseCycle + TestFakeTier_determinism 동등.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gpu_usage_audit.tier import FakeTier, Tier

TS = datetime(2026, 5, 11, 0, 0, 0, tzinfo=UTC)

# GPU-0 (압축 워크로드):
#   tick 0..1 → util=80, mem=70000  (active)
#   tick 2..3 → util=2,  mem=70000  (idle-held)
#   tick 4    → util=0,  mem=0       (truly-idle, proc 없음)
#   tick 5+   → 다시 0번 패턴 반복
GPU0_PHASE_EXPECT = [
    (80, 70000),  # tick 0
    (80, 70000),  # tick 1
    (2, 70000),  # tick 2
    (2, 70000),  # tick 3
    (0, 0),  # tick 4 — proc 없음
    (80, 70000),  # tick 5 — 사이클 반복
    (80, 70000),  # tick 6
]


def test_fake_tier_satisfies_protocol() -> None:
    # 구조적 호환 — FakeTier 가 Tier 프로토콜을 만족하는지 정적으로
    # 확인 (Python 의 runtime_checkable 없이도 mypy 가 잡지만,
    # 명시적으로 한 번 더).
    f: Tier = FakeTier()
    assert f.probe() == "560.35.05-fake"


@pytest.mark.parametrize(
    ("tick_idx", "util", "mem"),
    [(i, u, m) for i, (u, m) in enumerate(GPU0_PHASE_EXPECT)],
)
def test_fake_tier_phase_cycle(tick_idx: int, util: int, mem: int) -> None:
    # 인스턴스를 tick_idx + 1 번 호출해 *그 시점* 의 상태 확인.
    # parametrize 가 각 케이스에서 새 인스턴스 만들도록 강제 — 정확한 격리.
    f = FakeTier()
    last_snap = None
    for _ in range(tick_idx + 1):
        last_snap = f.collect(TS)
    assert last_snap is not None

    # GPU-0 util
    gpu0 = next(g for g in last_snap.gpus if g.uuid == "GPU-0")
    assert gpu0.util_pct == util, f"tick {tick_idx}: GPU-0 util"

    # GPU-0 메모리 (해당 카드 proc 합)
    gpu0_mem = sum((p.mem_used_mb or 0) for p in last_snap.procs if p.gpu_uuid == "GPU-0")
    assert gpu0_mem == mem, f"tick {tick_idx}: GPU-0 mem"


def test_fake_tier_gpu1_and_gpu2_invariants() -> None:
    # GPU-1: 매 틱 util=2 (Jupyter idle-held)
    # GPU-2: 매 틱 util=0 (graphics-only idle)
    f = FakeTier()
    for _ in range(7):
        snap = f.collect(TS)
        gpu1 = next(g for g in snap.gpus if g.uuid == "GPU-1")
        gpu2 = next(g for g in snap.gpus if g.uuid == "GPU-2")
        assert gpu1.util_pct == 2
        assert gpu2.util_pct == 0


def test_fake_tier_determinism_across_instances() -> None:
    # 두 인스턴스가 같은 호출 시퀀스에 같은 결과 — 상태 격리 보장.
    a, b = FakeTier(), FakeTier()
    for _ in range(12):
        sa = a.collect(TS)
        sb = b.collect(TS)
        assert [(g.uuid, g.util_pct) for g in sa.gpus] == [(g.uuid, g.util_pct) for g in sb.gpus]
        assert [(p.gpu_uuid, p.pid, p.mem_used_mb) for p in sa.procs] == [
            (p.gpu_uuid, p.pid, p.mem_used_mb) for p in sb.procs
        ]

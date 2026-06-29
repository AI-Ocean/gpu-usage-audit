"""FakeTier.collect_util 단위테스트 (0032-2 T2.1). 가벼운 util 표본 경로."""

from datetime import UTC, datetime

from gpu_usage_audit.tier import FakeTier


def test_collect_util_returns_all_gpus() -> None:
    samples = FakeTier().collect_util(0.0)
    assert [s.uuid for s in samples] == ["GPU-0", "GPU-1", "GPU-2"]
    assert all(s.ts == 0.0 for s in samples)


def test_collect_util_phase_varies_with_ts() -> None:
    # phase = int(ts) % 5: <2 학습(80), <4 idle-held(2), else cleanup(0)
    assert FakeTier().collect_util(0.0)[0].util_pct == 80
    assert FakeTier().collect_util(3.0)[0].util_pct == 2
    assert FakeTier().collect_util(4.0)[0].util_pct == 0
    assert FakeTier().collect_util(4.0)[0].mem_used_mb == 0


def test_collect_util_does_not_advance_snapshot_phase() -> None:
    """collect_util 은 스냅샷 _tick 을 건드리지 않는다 → collect() 는 여전히 phase0부터."""
    tier = FakeTier()
    tier.collect_util(99.0)
    tier.collect_util(99.0)
    snap = tier.collect(datetime.now(UTC))  # 첫 collect → phase0 = GPU-0 util 80
    assert snap.gpus[0].util_pct == 80

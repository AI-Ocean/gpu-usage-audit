from __future__ import annotations

from gpu_usage_audit.model import GPUSample, ProcSample, Snapshot
from gpu_usage_audit.usage_state import LOW_UTIL_STREAK_WINDOW, UsageStateTracker


def _snapshot(
    *,
    util_pct: int,
    compute_resident: bool = True,
    graphics_resident: bool = False,
    compute_unavailable: bool = False,
) -> Snapshot:
    procs: list[ProcSample] = []
    if compute_resident:
        procs.append(
            ProcSample(
                gpu_uuid="GPU-0",
                pid=100,
                mem_used_mb=None,
                process_type="compute",
            )
        )
    if graphics_resident:
        procs.append(
            ProcSample(
                gpu_uuid="GPU-0",
                pid=200,
                mem_used_mb=128,
                process_type="graphics",
            )
        )
    return Snapshot(
        gpus=[GPUSample(uuid="GPU-0", util_pct=util_pct)],
        procs=procs,
        compute_processes_unavailable_uuids={"GPU-0"} if compute_unavailable else set(),
    )


def test_graphics_only_is_idle() -> None:
    tracker = UsageStateTracker()
    snap = _snapshot(util_pct=0, compute_resident=False, graphics_resident=True)

    tracker.apply(snap)

    assert snap.gpus[0].usage_state == "idle"


def test_sync_once_low_util_compute_resident_is_idle_held() -> None:
    tracker = UsageStateTracker()
    snap = _snapshot(util_pct=0, compute_resident=True)

    tracker.apply(snap, sync_once=True)

    assert snap.gpus[0].usage_state == "idle_held"


def test_active_util_is_active() -> None:
    tracker = UsageStateTracker()
    snap = _snapshot(util_pct=80, compute_resident=True)

    tracker.apply(snap)

    assert snap.gpus[0].usage_state == "active"


def test_cold_start_low_util_compute_resident_is_idle_held() -> None:
    tracker = UsageStateTracker()
    snap = _snapshot(util_pct=0, compute_resident=True)

    tracker.apply(snap)

    assert snap.gpus[0].usage_state == "idle_held"


def test_recent_active_low_util_stays_active_until_window() -> None:
    tracker = UsageStateTracker()
    active = _snapshot(util_pct=80, compute_resident=True)
    tracker.apply(active)
    assert active.gpus[0].usage_state == "active"

    low = active
    for _ in range(LOW_UTIL_STREAK_WINDOW - 1):
        low = _snapshot(util_pct=0, compute_resident=True)
        tracker.apply(low)
        assert low.gpus[0].usage_state == "active"

    low = _snapshot(util_pct=0, compute_resident=True)
    tracker.apply(low)

    assert low.gpus[0].usage_state == "idle_held"


def test_compute_list_unavailable_omits_usage_state() -> None:
    tracker = UsageStateTracker()
    snap = _snapshot(
        util_pct=0,
        compute_resident=False,
        graphics_resident=True,
        compute_unavailable=True,
    )

    tracker.apply(snap)

    assert snap.gpus[0].usage_state is None

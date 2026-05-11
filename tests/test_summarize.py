"""Summarize 테스트. Go v0.1.0 의 TestSummarize 시나리오 그대로."""

from __future__ import annotations

from gpu_usage_audit.classify import Class
from gpu_usage_audit.model import GPUSample, ProcSample, Snapshot
from gpu_usage_audit.summarize import summarize


def test_summarize_three_gpus_with_one_unknown_proc() -> None:
    snap = Snapshot(
        gpus=[
            GPUSample(uuid="GPU-0", util_pct=80),  # active, 학습 중
            GPUSample(uuid="GPU-1", util_pct=2),  # idle-held (메모리 잡혀있음)
            GPUSample(uuid="GPU-2", util_pct=0),  # truly-idle (proc 없음)
        ],
        procs=[
            ProcSample(gpu_uuid="GPU-0", pid=100, mem_used_mb=30000, loginuid_user="alice"),
            ProcSample(gpu_uuid="GPU-0", pid=101, mem_used_mb=40000, loginuid_user="alice"),
            ProcSample(gpu_uuid="GPU-1", pid=200, mem_used_mb=70000, loginuid_user="alice"),
            # 알 수 없는 UUID — 드랍돼야 한다.
            ProcSample(gpu_uuid="GPU-99", pid=999, mem_used_mb=1234, loginuid_user=None),
        ],
    )

    got = summarize(snap)
    assert len(got) == 3, "len(summarize) must equal len(snap.gpus)"

    # (1)(2)(3): GPU-0 — 두 proc 합 70000, util 80 → active, 정렬 40000>30000
    g0 = got[0]
    assert (g0.uuid, g0.proc_mem_mb, g0.klass) == ("GPU-0", 70000, Class.ACTIVE)
    assert [p.mem_used_mb for p in g0.procs] == [40000, 30000]

    # (2): GPU-1 — proc 메모리 70000, util 2 → idle-held
    g1 = got[1]
    assert (g1.uuid, g1.proc_mem_mb, g1.klass) == ("GPU-1", 70000, Class.IDLE_HELD)

    # (5): GPU-2 — proc 없음 → mem=0, truly-idle
    g2 = got[2]
    assert (g2.uuid, g2.proc_mem_mb, g2.klass, len(g2.procs)) == (
        "GPU-2",
        0,
        Class.TRULY_IDLE,
        0,
    )

    # (4): GPU-99 의 proc 은 어디에도 안 들어가야.
    for c in got:
        assert all(p.pid != 999 for p in c.procs), f"unknown-GPU proc leaked into {c.uuid}"


def test_summarize_empty_snapshot() -> None:
    # 빈 Snapshot → 빈 list (방어).
    assert summarize(Snapshot()) == []

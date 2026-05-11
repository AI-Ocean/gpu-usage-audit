"""Snapshot → list[CardSummary] 변환. Go v0.1.0 의 Summarize 와 동등.

검증할 *행위 자체* 는 단순:
  1. 카드별 메모리 = 그 카드의 proc 메모리 합.
  2. Class = classify(util, 합산 메모리).
  3. Procs 는 mem_used_mb *내림차순* 정렬.
  4. snap.gpus 에 없는 UUID 의 proc 은 *드랍*.
  5. proc 0개 카드는 mem=0, truly-idle 로 분류.
"""

from __future__ import annotations

from collections import defaultdict

from .classify import Sample, classify
from .model import CardSummary, ProcSample, Snapshot


def summarize(snap: Snapshot) -> list[CardSummary]:
    """Snapshot 한 틱을 카드 단위로 접는다.

    snap.gpus 의 *순서를 유지* — 출력 list 의 i-th 항목은 snap.gpus[i] 의
    카드. 알 수 없는 UUID 에 매달린 proc 은 결과 어디에도 들어가지 않음.
    """
    procs_by_gpu: dict[str, list[ProcSample]] = defaultdict(list)
    for p in snap.procs:
        procs_by_gpu[p.gpu_uuid].append(p)

    # mem 내림차순 정렬 — render 측이 §1/§3 표시에 활용.
    for uuid in procs_by_gpu:
        procs_by_gpu[uuid].sort(key=lambda p: p.mem_used_mb, reverse=True)

    out: list[CardSummary] = []
    for g in snap.gpus:
        procs = procs_by_gpu.get(g.uuid, [])
        mem = sum(p.mem_used_mb for p in procs)
        out.append(
            CardSummary(
                uuid=g.uuid,
                util_pct=g.util_pct,
                proc_mem_mb=mem,
                klass=classify(Sample(util_pct=g.util_pct, proc_mem_mb=mem)),
                procs=procs,
            )
        )
    return out

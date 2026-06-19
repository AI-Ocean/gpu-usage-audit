"""Snapshot → `availability.snapshot.v1` payload builder.

GUA Board 의 AgentSnapshotV1 은 camelCase + `extra="forbid"` 이라, 여기서
*정확히* 정의된 필드만 camelCase 로 만든다. 서버 422 를 피하려고 클라이언트
측에서 방어적으로 clamp 한다 (util 0–100, memoryUsed ≤ memoryTotal,
linuxUser/name non-empty).

full command line 등 민감 필드는 절대 포함하지 않는다 — process name 은
`/proc/<pid>/comm`(executable basename) 으로 제한한다.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from ..model import ProcSample, Snapshot
from . import SCHEMA_VERSION

CollectionStatus = str  # "ok" | "partial" | "error" — 서버가 enum 검증.
_UNKNOWN = "unknown"

# 안정적인 short error code — 서버/board 가 freshness 신호로 표시한다.
# 새 code 는 여기 한곳에 모아 builder/CLI/daemon 이 같은 문자열을 공유한다.
ERROR_PROCESS_LIST_UNAVAILABLE = "process_list_unavailable"
ERROR_NVML_INIT_FAILED = "nvml_init_failed"


def derive_collection_status(
    snapshot: Snapshot,
    *,
    process_list_unavailable: bool = False,
) -> tuple[CollectionStatus, list[str]]:
    """수집 결과에서 `(collectionStatus, errors)` 를 도출한다.

    contract 와 같은 곳에 둬서 CLI(`sync-once`)/daemon 두 call site 가 같은
    규칙을 공유한다. builder 의 검증(`partial`/`error` 는 errors ≥1, `ok` 는
    errors 비어야 함)과 일관되게 만든다.

    - GPU 가 하나라도 수집되고 일부 카드의 process list 만 권한/일시오류로
      비었으면 → `partial` + `process_list_unavailable`. core GPU metric 은
      그대로 push 한다.
    - 그 외(모든 카드 정상, 또는 애초에 GPU 0개여도 push 흐름 자체는 정상) → `ok`.

    NVML init 자체가 실패해 GPU inventory 가 아예 없는 `error` heartbeat 는
    수집을 못 한 상황이라 이 함수가 아니라 call site 에서 직접 구성한다.
    """
    if snapshot.gpus and process_list_unavailable:
        return "partial", [ERROR_PROCESS_LIST_UNAVAILABLE]
    return "ok", []


def build_observation_payload(
    *,
    snapshot: Snapshot,
    hostname: str,
    driver_version: str,
    agent_version: str,
    observed_at: datetime,
    host_id: str | None = None,
    display_name: str | None = None,
    collection_status: CollectionStatus = "ok",
    errors: list[str] | None = None,
) -> dict[str, Any]:
    """in-memory Snapshot 을 서버가 받을 payload dict 로 변환한다."""
    errs = list(errors or [])
    if collection_status in {"partial", "error"} and not errs:
        raise ValueError("partial/error snapshot must include at least one error")
    if collection_status == "ok" and errs:
        raise ValueError("ok snapshot must not include collection errors")

    host: dict[str, Any] = {
        "hostname": hostname,
        "agentVersion": agent_version,
        "driverVersion": driver_version,
    }
    if host_id is not None:
        host["hostId"] = host_id
    if display_name is not None:
        host["displayName"] = display_name

    procs_by_uuid: dict[str, list[ProcSample]] = defaultdict(list)
    for p in snapshot.procs:
        procs_by_uuid[p.gpu_uuid].append(p)

    gpus: list[dict[str, Any]] = []
    for position, g in enumerate(snapshot.gpus):
        if g.memory_total_mb is None or g.memory_total_mb <= 0:
            raise ValueError(f"gpu {g.uuid} is missing a positive memoryTotalMb")
        total = g.memory_total_mb
        used = _clamp(g.memory_used_mb or 0, 0, total)
        gpu_payload: dict[str, Any] = {
            "index": g.index if g.index is not None else position,
            "uuid": g.uuid,
            "name": g.name or _UNKNOWN,
            "memoryTotalMb": total,
            "utilPct": _clamp(g.util_pct, 0, 100),
            "memoryUsedMb": used,
            # 서버 contract 는 temperatureC/powerW 를 ge=0 으로 검증 — util/memory 와
            # 같이 방어적으로 음수 sentinel 을 0 으로 눌러 한 GPU의 이상치가 전체
            # snapshot 을 422 로 떨어뜨리지 않게 한다.
            "temperatureC": _nonneg_or_none(g.temperature_c),
            "powerW": _nonneg_or_none(g.power_w),
            "processes": _build_processes(procs_by_uuid.get(g.uuid, [])),
        }
        if g.usage_state is not None:
            gpu_payload["usageState"] = g.usage_state
        gpus.append(gpu_payload)

    return {
        "schemaVersion": SCHEMA_VERSION,
        "collectionStatus": collection_status,
        "observedAt": observed_at.isoformat(),
        "host": host,
        "gpus": gpus,
        "errors": errs,
    }


def _build_processes(procs: list[ProcSample]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in procs:
        if p.pid <= 0:  # 서버는 pid>0 만 허용 — 방어적으로 제외.
            continue
        out.append(
            {
                "pid": p.pid,
                "linuxUser": p.loginuid_user or _UNKNOWN,
                "name": p.process_name or _UNKNOWN,
                "type": p.process_type,
                "memoryUsedMb": None if p.mem_used_mb is None else max(0, p.mem_used_mb),
            }
        )
    return out


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _nonneg_or_none(value: int | None) -> int | None:
    return max(0, value) if value is not None else None

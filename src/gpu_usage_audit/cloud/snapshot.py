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
        gpus.append(
            {
                "index": g.index if g.index is not None else position,
                "uuid": g.uuid,
                "name": g.name or _UNKNOWN,
                "memoryTotalMb": total,
                "utilPct": _clamp(g.util_pct, 0, 100),
                "memoryUsedMb": used,
                "temperatureC": g.temperature_c,
                "powerW": g.power_w,
                "processes": _build_processes(procs_by_uuid.get(g.uuid, [])),
            }
        )

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
                "memoryUsedMb": max(0, p.mem_used_mb),
            }
        )
    return out


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))

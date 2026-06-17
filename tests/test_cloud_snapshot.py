"""cloud.snapshot — payload builder shape + 방어적 clamp."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gpu_usage_audit.cloud.snapshot import build_observation_payload
from gpu_usage_audit.model import GPUSample, ProcSample, Snapshot
from gpu_usage_audit.tier import FakeTier

OBSERVED_AT = datetime(2026, 6, 17, 4, 0, 0, tzinfo=UTC)


def _build(snapshot: Snapshot, **kwargs: object) -> dict[str, Any]:
    return build_observation_payload(
        snapshot=snapshot,
        hostname="server-a",
        driver_version="560.35.05",
        agent_version="1.1.0",
        observed_at=OBSERVED_AT,
        **kwargs,  # type: ignore[arg-type]
    )


def test_builds_contract_shaped_payload() -> None:
    snap = Snapshot(
        gpus=[
            GPUSample(
                uuid="GPU-0",
                util_pct=3,
                index=0,
                name="NVIDIA RTX A6000",
                memory_total_mb=49140,
                memory_used_mb=18420,
                temperature_c=54,
                power_w=72,
            )
        ],
        procs=[
            ProcSample(
                gpu_uuid="GPU-0",
                pid=12345,
                mem_used_mb=18200,
                loginuid_user="lee",
                gpu_index=0,
                process_name="python",
            )
        ],
    )
    payload = _build(snap, host_id="host-123", display_name="a6000-01")

    assert payload["schemaVersion"] == "availability.snapshot.v1"
    assert payload["collectionStatus"] == "ok"
    assert payload["observedAt"] == "2026-06-17T04:00:00+00:00"
    assert payload["errors"] == []
    assert payload["host"] == {
        "hostname": "server-a",
        "agentVersion": "1.1.0",
        "driverVersion": "560.35.05",
        "hostId": "host-123",
        "displayName": "a6000-01",
    }
    gpu = payload["gpus"][0]
    assert gpu == {
        "index": 0,
        "uuid": "GPU-0",
        "name": "NVIDIA RTX A6000",
        "memoryTotalMb": 49140,
        "utilPct": 3,
        "memoryUsedMb": 18420,
        "temperatureC": 54,
        "powerW": 72,
        "processes": [{"pid": 12345, "linuxUser": "lee", "name": "python", "memoryUsedMb": 18200}],
    }


def test_clamps_and_fills_unknown_identities() -> None:
    snap = Snapshot(
        gpus=[
            GPUSample(
                uuid="GPU-0",
                util_pct=150,  # > 100 → clamp
                index=0,
                name=None,  # → "unknown"
                memory_total_mb=1000,
                memory_used_mb=5000,  # > total → clamp to total
            )
        ],
        procs=[
            ProcSample(
                gpu_uuid="GPU-0", pid=1, mem_used_mb=10, loginuid_user=None, process_name=None
            ),
            ProcSample(gpu_uuid="GPU-0", pid=0, mem_used_mb=10),  # pid<=0 → dropped
        ],
    )
    gpu = _build(snap)["gpus"][0]

    assert gpu["utilPct"] == 100
    assert gpu["memoryUsedMb"] == 1000
    assert gpu["name"] == "unknown"
    assert gpu["temperatureC"] is None and gpu["powerW"] is None
    assert gpu["processes"] == [
        {"pid": 1, "linuxUser": "unknown", "name": "unknown", "memoryUsedMb": 10}
    ]


def test_missing_memory_total_is_rejected() -> None:
    snap = Snapshot(gpus=[GPUSample(uuid="GPU-0", util_pct=0)])
    with pytest.raises(ValueError, match="memoryTotalMb"):
        _build(snap)


def test_collection_status_error_requires_errors() -> None:
    with pytest.raises(ValueError, match="at least one error"):
        _build(Snapshot(), collection_status="error")
    # ok + errors 도 모순.
    with pytest.raises(ValueError, match="must not include"):
        _build(Snapshot(), collection_status="ok", errors=["boom"])


def test_fake_tier_snapshot_builds_valid_payload() -> None:
    # FakeTier 출력이 그대로 contract 모양으로 변환되는지 (sync-once --fake 기반).
    snap = FakeTier().collect(OBSERVED_AT)
    payload = _build(snap)
    assert [g["index"] for g in payload["gpus"]] == [0, 1, 2]
    for gpu in payload["gpus"]:
        assert gpu["memoryTotalMb"] > 0
        assert 0 <= gpu["utilPct"] <= 100
        assert gpu["memoryUsedMb"] <= gpu["memoryTotalMb"]
        for proc in gpu["processes"]:
            assert proc["linuxUser"]
            assert proc["name"]

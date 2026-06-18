"""cloud.snapshot — payload builder shape + 방어적 clamp."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gpu_usage_audit.cloud.snapshot import (
    build_observation_payload,
    derive_collection_status,
)
from gpu_usage_audit.model import GPUSample, ProcSample, Snapshot
from gpu_usage_audit.tier import FakeTier

_GPU = GPUSample(uuid="GPU-0", util_pct=0, index=0, name="GPU", memory_total_mb=1000)

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


def test_negative_temperature_and_power_are_clamped_to_nonneg() -> None:
    # 일부 드라이버/SKU는 음수 sentinel을 반환 — 서버 ge=0 검증에 막히지 않게 0으로.
    snap = Snapshot(
        gpus=[
            GPUSample(
                uuid="GPU-0",
                util_pct=10,
                index=0,
                name="GPU",
                memory_total_mb=1000,
                memory_used_mb=10,
                temperature_c=-1,
                power_w=-5,
            )
        ]
    )
    gpu = _build(snap)["gpus"][0]
    assert gpu["temperatureC"] == 0
    assert gpu["powerW"] == 0


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


# ── derive_collection_status ─────────────────────────────────────


def test_derive_status_ok_when_no_degradation() -> None:
    snap = Snapshot(gpus=[_GPU])
    assert derive_collection_status(snap) == ("ok", [])
    assert derive_collection_status(snap, process_list_unavailable=False) == ("ok", [])


def test_derive_status_partial_when_process_list_unavailable() -> None:
    snap = Snapshot(gpus=[_GPU])
    status, errors = derive_collection_status(snap, process_list_unavailable=True)
    assert status == "partial"
    assert errors == ["process_list_unavailable"]
    # builder 검증을 그대로 통과해야 한다 (partial 은 errors ≥1).
    payload = _build(snap, collection_status=status, errors=errors)
    assert payload["collectionStatus"] == "partial"
    assert payload["errors"] == ["process_list_unavailable"]


def test_derive_status_partial_flag_ignored_when_no_gpus() -> None:
    # GPU 가 0개면 partial 로 판정할 카드가 없다 — ok.
    assert derive_collection_status(Snapshot(), process_list_unavailable=True) == ("ok", [])


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

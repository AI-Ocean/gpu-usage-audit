"""NVMLTier 의 *변환 로직* 을 mock pynvml 로 검증.

실 nvmlInit 은 GPU 머신에서만 동작 — 여기서는 호출 시퀀스 + bytes/str
decode + usedGpuMemory=None skip 같은 *데이터 모양* 만 확인.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from gpu_usage_audit.nvml import NVMLNotAvailableError, NVMLTier, _decode

TS = datetime(2026, 5, 11, 0, 0, 0, tzinfo=UTC)


# ── _decode ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("560.35.05", "560.35.05"),
        (b"560.35.05", "560.35.05"),
    ],
)
def test_decode(raw: str | bytes, want: str) -> None:
    assert _decode(raw) == want


# ── NVMLNotAvailableError when pynvml missing ────────────────────


def test_probe_raises_when_pynvml_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    # pynvml 모듈을 sys.modules 에서 가린 채 import 시도 → 실패 경로.
    monkeypatch.setitem(sys.modules, "pynvml", None)
    tier = NVMLTier()
    with pytest.raises(NVMLNotAvailableError, match="pynvml not installed"):
        tier.probe()


# ── Collect 변환 로직 (mock pynvml) ──────────────────────────────


def _make_mock_pynvml(
    *,
    driver: str | bytes,
    gpus: list[dict[str, Any]],
) -> MagicMock:
    """가짜 pynvml 모듈. NVMLTier 가 호출하는 함수들만 구현.

    gpus 항목은 {"uuid", "util", "procs": [{"pid", "mem"}]} 형태.
    mem=None 이면 usedGpuMemory 가 None — skip 검증용.
    """

    class _FakeNVMLError(Exception):
        pass

    nvml = MagicMock()
    nvml.NVMLError = _FakeNVMLError
    nvml.nvmlInit = MagicMock(return_value=None)
    nvml.nvmlShutdown = MagicMock(return_value=None)
    nvml.nvmlSystemGetDriverVersion = MagicMock(return_value=driver)
    nvml.nvmlDeviceGetCount = MagicMock(return_value=len(gpus))

    # GPU 핸들은 그저 index → dict 로 직접 매핑.
    nvml.nvmlDeviceGetHandleByIndex = MagicMock(side_effect=lambda i: gpus[i])
    nvml.nvmlDeviceGetUUID = MagicMock(side_effect=lambda h: h["uuid"])
    nvml.nvmlDeviceGetUtilizationRates = MagicMock(
        side_effect=lambda h: SimpleNamespace(gpu=h["util"], memory=0)
    )
    nvml.nvmlDeviceGetComputeRunningProcesses = MagicMock(
        side_effect=lambda h: [
            SimpleNamespace(pid=p["pid"], usedGpuMemory=p["mem"]) for p in h["procs"]
        ]
    )
    return nvml


def test_probe_returns_driver_version_and_decodes_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _make_mock_pynvml(driver=b"560.35.05", gpus=[])
    monkeypatch.setitem(sys.modules, "pynvml", fake)

    tier = NVMLTier()
    try:
        assert tier.probe() == "560.35.05"
        fake.nvmlInit.assert_called_once()
    finally:
        tier.close()
    fake.nvmlShutdown.assert_called_once()


def test_collect_converts_bytes_and_memory_units(monkeypatch: pytest.MonkeyPatch) -> None:
    # 70 GB = 70 * 1024^3 bytes → 70 * 1024 MB = 71680 MB.
    fake = _make_mock_pynvml(
        driver="560.35.05",
        gpus=[
            {
                "uuid": b"GPU-aaaa",
                "util": 80,
                "procs": [{"pid": 1234, "mem": 70 * 1024 * 1024 * 1024}],
            },
            {
                "uuid": "GPU-bbbb",
                "util": 2,
                "procs": [
                    {"pid": 5678, "mem": 8 * 1024 * 1024 * 1024},
                    # usedGpuMemory=None → skip (NVML 권한 부족 케이스).
                    {"pid": 9999, "mem": None},
                ],
            },
        ],
    )
    monkeypatch.setitem(sys.modules, "pynvml", fake)

    with NVMLTier() as tier:
        tier.probe()
        snap = tier.collect(TS)

    assert [(g.uuid, g.util_pct) for g in snap.gpus] == [
        ("GPU-aaaa", 80),
        ("GPU-bbbb", 2),
    ]
    assert [(p.gpu_uuid, p.pid, p.mem_used_mb) for p in snap.procs] == [
        ("GPU-aaaa", 1234, 71680),
        ("GPU-bbbb", 5678, 8192),
    ]


def test_collect_before_probe_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _make_mock_pynvml(driver="x", gpus=[])
    monkeypatch.setitem(sys.modules, "pynvml", fake)
    with pytest.raises(NVMLNotAvailableError, match="before probe"):
        NVMLTier().collect(TS)


def test_collect_handles_per_device_running_processes_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _make_mock_pynvml(
        driver="x",
        gpus=[{"uuid": "GPU-x", "util": 0, "procs": []}],
    )

    # 한 카드의 process list 가 NVMLError 로 실패해도 그 카드 자체는 유지.
    def _raise(_h: Any) -> Any:
        raise fake.NVMLError("permission denied")

    fake.nvmlDeviceGetComputeRunningProcesses = MagicMock(side_effect=_raise)
    monkeypatch.setitem(sys.modules, "pynvml", fake)

    with NVMLTier() as tier:
        tier.probe()
        snap = tier.collect(TS)
    assert [g.uuid for g in snap.gpus] == ["GPU-x"]
    assert snap.procs == []


def test_probe_translates_nvml_error_to_friendly(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _make_mock_pynvml(driver="x", gpus=[])
    fake.nvmlInit = MagicMock(side_effect=fake.NVMLError("no driver"))
    monkeypatch.setitem(sys.modules, "pynvml", fake)

    with pytest.raises(NVMLNotAvailableError, match="initialization failed"):
        NVMLTier().probe()

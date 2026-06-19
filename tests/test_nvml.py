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

from gpu_usage_audit.nvml import (
    NVMLNotAvailableError,
    NVMLTier,
    _decode,
    nvml_init_error_message,
)

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
    with pytest.raises(NVMLNotAvailableError, match="pynvml is not importable"):
        tier.probe()


# ── Collect 변환 로직 (mock pynvml) ──────────────────────────────


def _make_mock_pynvml(
    *,
    driver: str | bytes,
    gpus: list[dict[str, Any]],
) -> MagicMock:
    """가짜 pynvml 모듈. NVMLTier 가 호출하는 함수들만 구현.

    gpus 항목은 {"uuid", "util", "procs": [{"pid", "mem"}]} 형태.
    "graphics_procs", "compute_error", "graphics_error" 도 선택 지원.
    선택 키: "name", "mem_total", "mem_used", "temp", "power_mw" (없으면 기본값).
    mem=None 이면 usedGpuMemory 가 None — skip 검증용.
    """

    class _FakeNVMLError(Exception):
        def __init__(self, message: str = "NVML error", value: int | None = None) -> None:
            super().__init__(message)
            self.value = value

    nvml = MagicMock()
    nvml.NVMLError = _FakeNVMLError
    nvml.NVML_ERROR_LIBRARY_NOT_FOUND = 12
    nvml.NVML_ERROR_DRIVER_NOT_LOADED = 9
    nvml.NVML_ERROR_LIB_RM_VERSION_MISMATCH = 19
    nvml.NVML_TEMPERATURE_GPU = 0
    nvml.nvmlInit = MagicMock(return_value=None)
    nvml.nvmlShutdown = MagicMock(return_value=None)
    nvml.nvmlSystemGetDriverVersion = MagicMock(return_value=driver)
    nvml.nvmlDeviceGetCount = MagicMock(return_value=len(gpus))

    # GPU 핸들은 그저 index → dict 로 직접 매핑.
    nvml.nvmlDeviceGetHandleByIndex = MagicMock(side_effect=lambda i: gpus[i])
    nvml.nvmlDeviceGetUUID = MagicMock(side_effect=lambda h: h["uuid"])
    nvml.nvmlDeviceGetName = MagicMock(side_effect=lambda h: h.get("name", "NVIDIA Test GPU"))
    nvml.nvmlDeviceGetUtilizationRates = MagicMock(
        side_effect=lambda h: SimpleNamespace(gpu=h["util"], memory=0)
    )
    nvml.nvmlDeviceGetMemoryInfo = MagicMock(
        side_effect=lambda h: SimpleNamespace(
            total=h.get("mem_total", 48 * 1024 * 1024 * 1024),
            used=h.get("mem_used", 0),
            free=0,
        )
    )
    nvml.nvmlDeviceGetTemperature = MagicMock(side_effect=lambda h, sensor: h.get("temp", 50))
    nvml.nvmlDeviceGetPowerUsage = MagicMock(side_effect=lambda h: h.get("power_mw", 70000))

    def _process_infos(items: list[dict[str, Any]]) -> list[SimpleNamespace]:
        return [SimpleNamespace(pid=p["pid"], usedGpuMemory=p["mem"]) for p in items]

    def _compute_processes(h: dict[str, Any]) -> list[SimpleNamespace]:
        if h.get("compute_error"):
            raise nvml.NVMLError("compute denied")
        return _process_infos(h.get("procs", []))

    def _graphics_processes(h: dict[str, Any]) -> list[SimpleNamespace]:
        if h.get("graphics_error"):
            raise nvml.NVMLError("graphics denied")
        return _process_infos(h.get("graphics_procs", []))

    nvml.nvmlDeviceGetComputeRunningProcesses = MagicMock(side_effect=_compute_processes)
    nvml.nvmlDeviceGetGraphicsRunningProcesses = MagicMock(side_effect=_graphics_processes)
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
                    # usedGpuMemory=None → memory-unknown 으로 보존.
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
    assert [(p.gpu_uuid, p.pid, p.mem_used_mb, p.process_type) for p in snap.procs] == [
        ("GPU-aaaa", 1234, 71680, "compute"),
        ("GPU-bbbb", 5678, 8192, "compute"),
        ("GPU-bbbb", 9999, None, "compute"),
    ]


def test_collect_includes_graphics_processes_and_dedups_compute_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _make_mock_pynvml(
        driver="x",
        gpus=[
            {
                "uuid": "GPU-x",
                "util": 0,
                "procs": [
                    {"pid": 10, "mem": 1024 * 1024},
                    {"pid": 20, "mem": 2 * 1024 * 1024},
                ],
                "graphics_procs": [
                    {"pid": 20, "mem": 3 * 1024 * 1024},
                    {"pid": 30, "mem": None},
                ],
            }
        ],
    )
    monkeypatch.setitem(sys.modules, "pynvml", fake)

    with NVMLTier() as tier:
        tier.probe()
        snap = tier.collect(TS)

    assert [(p.pid, p.mem_used_mb, p.process_type) for p in snap.procs] == [
        (10, 1, "compute"),
        (20, 2, "compute"),
        (30, None, "graphics"),
    ]


@pytest.mark.parametrize(
    ("compute_error", "graphics_error", "want_pids", "want_compute_unavailable"),
    [
        (False, False, [10, 20], False),
        (False, True, [10], False),
        (True, False, [20], True),
        (True, True, [], True),
    ],
)
def test_collect_handles_compute_and_graphics_failures_independently(
    monkeypatch: pytest.MonkeyPatch,
    compute_error: bool,
    graphics_error: bool,
    want_pids: list[int],
    want_compute_unavailable: bool,
) -> None:
    fake = _make_mock_pynvml(
        driver="x",
        gpus=[
            {
                "uuid": "GPU-x",
                "util": 0,
                "procs": [{"pid": 10, "mem": 1024 * 1024}],
                "graphics_procs": [{"pid": 20, "mem": 2 * 1024 * 1024}],
                "compute_error": compute_error,
                "graphics_error": graphics_error,
            }
        ],
    )
    monkeypatch.setitem(sys.modules, "pynvml", fake)

    with NVMLTier() as tier:
        tier.probe()
        snap = tier.collect(TS)
        degraded = tier.last_process_list_unavailable

    assert [p.pid for p in snap.procs] == want_pids
    assert degraded is (compute_error or graphics_error)
    assert ("GPU-x" in snap.compute_processes_unavailable_uuids) is want_compute_unavailable


def test_collect_gathers_enriched_device_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _make_mock_pynvml(
        driver="560.35.05",
        gpus=[
            {
                "uuid": "GPU-aaaa",
                "util": 80,
                "name": b"NVIDIA RTX A6000",
                "mem_total": 48 * 1024 * 1024 * 1024,
                "mem_used": 18 * 1024 * 1024 * 1024,
                "temp": 54,
                "power_mw": 72000,
                "procs": [{"pid": 1234, "mem": 18 * 1024 * 1024 * 1024}],
            },
        ],
    )
    monkeypatch.setitem(sys.modules, "pynvml", fake)

    with NVMLTier() as tier:
        tier.probe()
        snap = tier.collect(TS)

    g = snap.gpus[0]
    assert (g.uuid, g.index, g.name) == ("GPU-aaaa", 0, "NVIDIA RTX A6000")
    assert (g.memory_total_mb, g.memory_used_mb) == (49152, 18432)
    assert (g.temperature_c, g.power_w) == (54, 72)
    # process 는 자기 카드의 index 를 들고 온다 (loginuid/name 은 caller 가 해석).
    assert (snap.procs[0].gpu_index, snap.procs[0].process_name) == (0, None)


def test_collect_tolerates_missing_temperature_and_power(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _make_mock_pynvml(
        driver="x",
        gpus=[{"uuid": "GPU-x", "util": 0, "procs": []}],
    )

    def _unsupported(*_args: Any) -> Any:
        raise fake.NVMLError("not supported")

    fake.nvmlDeviceGetTemperature = MagicMock(side_effect=_unsupported)
    fake.nvmlDeviceGetPowerUsage = MagicMock(side_effect=_unsupported)
    monkeypatch.setitem(sys.modules, "pynvml", fake)

    with NVMLTier() as tier:
        tier.probe()
        snap = tier.collect(TS)
    assert snap.gpus[0].temperature_c is None
    assert snap.gpus[0].power_w is None


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


def test_last_process_list_unavailable_tracks_per_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _make_mock_pynvml(
        driver="x",
        gpus=[{"uuid": "GPU-x", "util": 0, "procs": [{"pid": 1, "mem": 1024 * 1024}]}],
    )
    monkeypatch.setitem(sys.modules, "pynvml", fake)

    def _raise(_h: Any) -> Any:
        raise fake.NVMLError("permission denied")

    def _ok(_h: Any) -> Any:
        return [SimpleNamespace(pid=1, usedGpuMemory=1024 * 1024)]

    with NVMLTier() as tier:
        tier.probe()
        # 정상 collect → process list 가용.
        tier.collect(TS)
        clean = tier.last_process_list_unavailable

        # process list 가 실패하는 틱 → True (core GPU metric 은 유지).
        fake.nvmlDeviceGetComputeRunningProcesses = MagicMock(side_effect=_raise)
        snap = tier.collect(TS)
        degraded = tier.last_process_list_unavailable

        # 다시 정상으로 돌아오면 *틱마다 리셋* 된다.
        fake.nvmlDeviceGetComputeRunningProcesses = MagicMock(side_effect=_ok)
        tier.collect(TS)
        recovered = tier.last_process_list_unavailable

    assert clean is False
    assert degraded is True  # core metric 은 유지된 채 partial 신호만 켜진다.
    assert [g.uuid for g in snap.gpus] == ["GPU-x"]
    assert recovered is False


def test_probe_translates_nvml_error_to_friendly(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _make_mock_pynvml(driver="x", gpus=[])
    fake.nvmlInit = MagicMock(side_effect=fake.NVMLError("localized message", value=9))
    monkeypatch.setitem(sys.modules, "pynvml", fake)

    with pytest.raises(NVMLNotAvailableError, match="initialization failed"):
        NVMLTier().probe()


@pytest.mark.parametrize(
    ("code", "raw", "want"),
    [
        (12, "localized message", "libnvidia-ml.so.1 was not found"),
        (19, "localized message", "versions do not match"),
        (9, "localized message", "driver is not loaded"),
        (-1, "NVML Shared Library Not Found", "libnvidia-ml.so.1 was not found"),
        (-1, "other failure", "driver or NVML initialization failed"),
    ],
)
def test_nvml_init_error_message_classifies_common_failures(
    code: int,
    raw: str,
    want: str,
) -> None:
    fake = _make_mock_pynvml(driver="x", gpus=[])
    message = nvml_init_error_message(fake.NVMLError(raw, value=code), fake)
    assert want in message
    assert raw in message

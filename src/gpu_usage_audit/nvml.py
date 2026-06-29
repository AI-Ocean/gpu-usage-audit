"""실 NVIDIA NVML 위에서 GPU/프로세스 텔레메트리 수집.

`nvidia-ml-py` (= pynvml) 의존. bare-metal 1.0 에서는 기본 dependency 지만
GPU 없는 개발/CI/demo 환경도 계속 동작해야 하므로 import/init 은 늦게 한다.

운영 머신 (NVIDIA 드라이버 깔린 GPU 머신) 에서만 실 동작 검증 가능.
개발 머신에선 모킹으로 *변환 로직* 만 검증 (nvmlInit 자체는 실패).
"""

from __future__ import annotations

import contextlib
import logging
from datetime import datetime
from typing import Any

from .model import GPUSample, ProcessType, ProcSample, Snapshot, UtilSample

logger = logging.getLogger(__name__)


class NVMLNotAvailableError(RuntimeError):
    """pynvml 미설치 또는 NVML 초기화 실패. 사용자 facing 메시지로도 사용."""


def _load_pynvml() -> Any:
    """pynvml 을 늦게 import — 의존성 누락 케이스에 명확한 에러로.

    Any 반환: pynvml 은 type stub 이 없어 strict mypy 가 정확한 타입을
    추론 못 함. 호출 site 마다 attribute 접근만 하므로 Any 가 합리적.
    """
    try:
        import pynvml
    except ImportError as e:
        raise NVMLNotAvailableError(
            "pynvml is not importable. gpu-usage-audit includes nvidia-ml-py as a "
            "default dependency; reinstall with `uv tool install --force gpu-usage-audit`."
        ) from e
    return pynvml


def _decode(s: str | bytes) -> str:
    """일부 pynvml 함수는 bytes 반환 (라이브러리/드라이버 버전에 따라)."""
    return s.decode("utf-8") if isinstance(s, bytes) else s


class NVMLTier:
    """진짜 NVML 위에서 한 틱의 GPU/프로세스 텔레메트리.

    수명:
      probe() 첫 호출에 nvmlInit + driver version. close() 가 nvmlShutdown.
      `with NVMLTier() as t:` 컨텍스트 매니저로 두 메서드를 한 묶음으로.

    NVML 호출 정책:
      - utilization, compute processes, graphics processes 를 수집한다.
      - usedGpuMemory==None 은 "memory unknown" 으로 보존한다.
      - compute process list 실패와 graphics process list 실패는 독립 처리한다.
    """

    def __init__(self) -> None:
        self._nvml: Any | None = None  # pynvml ModuleType
        self._initialized = False
        self._process_list_warning_keys: set[tuple[str, str]] = set()
        # 가장 최근 collect() 에서 process list 를 읽지 못한 GPU UUID 들.
        # compute 실패는 usageState 생략에도 쓰이므로 graphics 실패와 분리한다.
        self._last_compute_process_list_unavailable_uuids: set[str] = set()
        self._last_graphics_process_list_unavailable_uuids: set[str] = set()

    def __enter__(self) -> NVMLTier:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def probe(self) -> str:
        nvml = self._nvml or _load_pynvml()
        try:
            nvml.nvmlInit()
        except nvml.NVMLError as e:
            raise NVMLNotAvailableError(
                f"NVML initialization failed: {nvml_init_error_message(e, nvml)}"
            ) from e
        self._nvml = nvml
        self._initialized = True
        return _decode(nvml.nvmlSystemGetDriverVersion())

    def collect(self, ts: datetime) -> Snapshot:
        del ts  # NVML 은 호출 시점의 *현재* 상태 — 인자 ts 미사용.
        nvml = self._nvml
        if nvml is None or not self._initialized:
            raise NVMLNotAvailableError("NVMLTier.collect called before probe()")

        gpus: list[GPUSample] = []
        procs: list[ProcSample] = []
        self._last_compute_process_list_unavailable_uuids = set()
        self._last_graphics_process_list_unavailable_uuids = set()
        count = nvml.nvmlDeviceGetCount()
        for i in range(count):
            h = nvml.nvmlDeviceGetHandleByIndex(i)
            uuid = _decode(nvml.nvmlDeviceGetUUID(h))
            util = nvml.nvmlDeviceGetUtilizationRates(h)
            mem = nvml.nvmlDeviceGetMemoryInfo(h)
            gpus.append(
                GPUSample(
                    uuid=uuid,
                    util_pct=int(util.gpu),
                    index=i,
                    name=_decode(nvml.nvmlDeviceGetName(h)),
                    memory_total_mb=int(mem.total) // (1024 * 1024),
                    memory_used_mb=int(mem.used) // (1024 * 1024),
                    temperature_c=self._read_temperature(nvml, h),
                    power_w=self._read_power_w(nvml, h),
                )
            )

            compute_running = self._read_running_processes(
                nvml,
                h,
                uuid,
                process_type="compute",
                getter_name="nvmlDeviceGetComputeRunningProcesses",
            )
            graphics_running = self._read_running_processes(
                nvml,
                h,
                uuid,
                process_type="graphics",
                getter_name="nvmlDeviceGetGraphicsRunningProcesses",
            )

            process_groups: tuple[tuple[ProcessType, list[Any]], ...] = (
                ("compute", compute_running),
                ("graphics", graphics_running),
            )
            procs_by_pid: dict[int, ProcSample] = {}
            for process_type, running in process_groups:
                for p in running:
                    pid = int(p.pid)
                    if process_type == "graphics" and pid in procs_by_pid:
                        continue
                    used = getattr(p, "usedGpuMemory", None)
                    mem_mb = None if used is None else int(used) // (1024 * 1024)
                    procs_by_pid[pid] = ProcSample(
                        gpu_uuid=uuid,
                        pid=pid,
                        mem_used_mb=mem_mb,
                        gpu_index=i,
                        process_type=process_type,
                    )
            procs.extend(procs_by_pid.values())
        return Snapshot(
            gpus=gpus,
            procs=procs,
            compute_processes_unavailable_uuids=set(
                self._last_compute_process_list_unavailable_uuids
            ),
        )

    def collect_util(self, ts: float) -> list[UtilSample]:
        """1초 라이브용 *가벼운* 표본 — util/mem 만. 프로세스 enumerate·온도·전력 생략.

        collect() 는 매 카드마다 compute+graphics 프로세스 목록을 walk 해서 비싸다.
        1초 루프엔 부적합 → 카드당 NVML 두 콜(util, mem)만. uuid 는 시계열 키.
        """
        nvml = self._nvml
        if nvml is None or not self._initialized:
            raise NVMLNotAvailableError("NVMLTier.collect_util called before probe()")
        out: list[UtilSample] = []
        for i in range(nvml.nvmlDeviceGetCount()):
            h = nvml.nvmlDeviceGetHandleByIndex(i)
            util = nvml.nvmlDeviceGetUtilizationRates(h)
            mem = nvml.nvmlDeviceGetMemoryInfo(h)
            out.append(
                UtilSample(
                    uuid=_decode(nvml.nvmlDeviceGetUUID(h)),
                    ts=ts,
                    util_pct=int(util.gpu),
                    mem_used_mb=int(mem.used) // (1024 * 1024),
                )
            )
        return out

    def _read_running_processes(
        self,
        nvml: Any,
        handle: Any,
        uuid: str,
        *,
        process_type: ProcessType,
        getter_name: str,
    ) -> list[Any]:
        try:
            getter = getattr(nvml, getter_name)
            return list(getter(handle))
        except (nvml.NVMLError, AttributeError) as e:
            unavailable = (
                self._last_compute_process_list_unavailable_uuids
                if process_type == "compute"
                else self._last_graphics_process_list_unavailable_uuids
            )
            unavailable.add(uuid)
            warning_key = (uuid, process_type)
            if warning_key not in self._process_list_warning_keys:
                logger.warning(
                    "NVML %s process list unavailable for %s; usage classification "
                    "may fall back: %s",
                    process_type,
                    uuid,
                    e,
                )
                self._process_list_warning_keys.add(warning_key)
            return []

    @property
    def last_process_list_unavailable(self) -> bool:
        """가장 최근 collect() 에서 한 카드라도 process list 를 못 읽었는지.

        True 면 core GPU metric 은 수집됐지만 일부 카드의 process 목록이
        권한/일시오류로 비었다는 뜻 — cloud push 는 `partial` 로 보낸다.
        """
        return bool(
            self._last_compute_process_list_unavailable_uuids
            or self._last_graphics_process_list_unavailable_uuids
        )

    @staticmethod
    def _read_temperature(nvml: Any, handle: Any) -> int | None:
        """GPU 온도(°C). 일부 카드/드라이버는 미지원 — 실패 시 None (optional metric)."""
        try:
            return int(nvml.nvmlDeviceGetTemperature(handle, nvml.NVML_TEMPERATURE_GPU))
        except nvml.NVMLError:
            return None

    @staticmethod
    def _read_power_w(nvml: Any, handle: Any) -> int | None:
        """순간 전력(W). NVML 은 milliwatt 반환. 미지원 시 None (optional metric)."""
        try:
            return int(nvml.nvmlDeviceGetPowerUsage(handle)) // 1000
        except nvml.NVMLError:
            return None

    def close(self) -> None:
        if not self._initialized or self._nvml is None:
            return
        # 종료 경로에서의 NVML 에러는 운영상 의미 없음 — silent.
        with contextlib.suppress(self._nvml.NVMLError):
            self._nvml.nvmlShutdown()
        self._initialized = False


def nvml_init_error_message(error: object, nvml: Any) -> str:
    detail = str(error).strip() or "unknown NVML error"
    reason = _nvml_error_reason(error, nvml, detail)
    return f"{reason}. Detail: {detail}"


def _nvml_error_reason(error: object, nvml: Any, detail: str) -> str:
    code = getattr(error, "value", None)
    if code == getattr(nvml, "NVML_ERROR_LIBRARY_NOT_FOUND", object()):
        reason = "libnvidia-ml.so.1 was not found"
    elif code == getattr(nvml, "NVML_ERROR_LIB_RM_VERSION_MISMATCH", object()):
        reason = "the NVIDIA driver and NVML library versions do not match"
    elif code == getattr(nvml, "NVML_ERROR_DRIVER_NOT_LOADED", object()):
        reason = "the NVIDIA driver is not loaded"
    else:
        reason = _fallback_nvml_error_reason(detail)
    return reason


def _fallback_nvml_error_reason(detail: str) -> str:
    lowered = detail.lower()
    if "shared library not found" in lowered or "libnvidia-ml" in lowered:
        return "libnvidia-ml.so.1 was not found"
    if "driver/library version mismatch" in lowered or "version mismatch" in lowered:
        return "the NVIDIA driver and NVML library versions do not match"
    if "driver not loaded" in lowered or "no driver" in lowered:
        return "the NVIDIA driver is not loaded"
    return "driver or NVML initialization failed"

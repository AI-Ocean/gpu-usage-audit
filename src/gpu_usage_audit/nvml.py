"""실 NVIDIA NVML 위에서 GPU/프로세스 텔레메트리 수집.

`nvidia-ml-py` (= pynvml) 의존. 옵션 의존성 `[nvml]` 로만 설치되므로,
import 단계에서 실패 시 `NVMLNotAvailableError` 를 띄울 수 있는
*늦은 바인딩* 패턴.

운영 머신 (NVIDIA 드라이버 깔린 GPU 머신) 에서만 실 동작 검증 가능.
개발 머신에선 모킹으로 *변환 로직* 만 검증 (nvmlInit 자체는 실패).
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Any

from .model import GPUSample, ProcSample, Snapshot


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
            "pynvml not installed. Install the [nvml] extra:\n"
            "  pip install 'gpu-usage-audit[nvml]'\n"
            "  uvx --with nvidia-ml-py gpu-usage-audit ..."
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
      - utilization, compute processes 만 수집 (encoder/graphics/MPS 제외).
        v0.2.0 의 핵심 funnel 메시지는 *compute* idle-held 라.
      - usedGpuMemory==None (NVML 의 권한 부족/N/A) 인 프로세스 → skip.
        그 프로세스가 정말로 메모리를 안 쓰는지 vs. 못 보는지 구별 불가
        한 채로 0 MB 적재하면 idle-held 분류가 망가짐.
    """

    def __init__(self) -> None:
        self._nvml: Any | None = None  # pynvml ModuleType
        self._initialized = False

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
                f"NVML initialization failed (driver missing or version mismatch?): {e}"
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
        count = nvml.nvmlDeviceGetCount()
        for i in range(count):
            h = nvml.nvmlDeviceGetHandleByIndex(i)
            uuid = _decode(nvml.nvmlDeviceGetUUID(h))
            util = nvml.nvmlDeviceGetUtilizationRates(h)
            gpus.append(GPUSample(uuid=uuid, util_pct=int(util.gpu)))

            # 일부 GPU/드라이버는 권한이 부족하면 이 호출 자체가 NVMLError —
            # 해당 카드의 process list 만 비우고 진행.
            try:
                running = nvml.nvmlDeviceGetComputeRunningProcesses(h)
            except nvml.NVMLError:
                running = []

            for p in running:
                # usedGpuMemory 가 None 이면 "측정 불가" 의미 — 0 으로 적재
                # 하면 분류가 truly-idle 로 가버려 사실 왜곡. skip.
                used = getattr(p, "usedGpuMemory", None)
                if used is None:
                    continue
                mem_mb = int(used) // (1024 * 1024)
                procs.append(
                    ProcSample(
                        gpu_uuid=uuid,
                        pid=int(p.pid),
                        mem_used_mb=mem_mb,
                    )
                )
        return Snapshot(gpus=gpus, procs=procs)

    def close(self) -> None:
        if not self._initialized or self._nvml is None:
            return
        # 종료 경로에서의 NVML 에러는 운영상 의미 없음 — silent.
        with contextlib.suppress(self._nvml.NVMLError):
            self._nvml.nvmlShutdown()
        self._initialized = False

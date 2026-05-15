"""gpu-usage-audit — surfaces idle-held NVIDIA GPU memory.

1.0 scope는 단일 로컬 베어메탈 NVIDIA 호스트의 NVML telemetry를 SQLite에
기록하고, active / idle-held / truly-idle retrospective report를 출력하는 것.
"""

# 런타임에서 버전 노출. pyproject.toml 의 [project.version] 과 동기 유지.
# importlib.metadata 로 자동 추출도 가능하지만, 단일 source of truth 를
# pyproject.toml 로 두기 위해 일단 metadata API 사용.
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("gpu-usage-audit")
except PackageNotFoundError:
    # 패키지 설치 안 된 상태 (예: 소스 트리에서 직접 import) — 개발 표식.
    __version__ = "0.0.0+local"

__all__ = ["__version__"]

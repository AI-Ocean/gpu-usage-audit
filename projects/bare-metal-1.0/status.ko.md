# Bare Metal 1.0 Status

갱신일: 2026-05-15

## 요약

Bare Metal 1.0은 단일 NVIDIA 베어메탈 호스트만 대상으로 하는 형태로 1.0.1까지
릴리스됐다. `v1.0.1` GitHub Release와 PyPI publish는 완료됐고, 사용자가 실제
NVIDIA host에서 telemetry 수집이 정상 동작하는 것도 확인했다.

현재 작업은 1.0.1 이후 코드 퀄리티 cleanup이다. 주요 초점은 background daemon
PID 안전성, report 의미 가시성, 내부 문서 정합성이다.

## 구현 상태

| 영역 | 상태 | 메모 |
| --- | --- | --- |
| Scope reset | 완료 | Kubernetes/Slurm/Docker/remote runtime 표면 제거. |
| `gua doctor` | 완료 | 현재 머신의 `/dev/nvidia*`, `nvidia-smi -L`, NVML, DB path만 진단. |
| Packaging UX | 완료 | `nvidia-ml-py`가 기본 dependency이고 `nvml` extra는 빈 compatibility alias. |
| `gua` command surface | 완료 | `doctor`, `daemon`, `start`, `status`, `stop`, `report`, `demo` 제공. |
| Background daemon UX | 완료 | `gua daemon`은 기본 백그라운드 실행, `--foreground`는 systemd/debug용. |
| `daemon`/`report` DB UX | 완료 | 기본 DB는 `/tmp/gua.db`; daemon은 기존 DB를 거부하고 report는 없는 DB를 거부. |
| README bare-metal 문서 | 완료 | install, runbook, systemd 예시, 운영 notes가 1.0.1 기준. |
| Release | 완료 | `v1.0.1` tag, GitHub Release, PyPI publish 완료. |
| NVIDIA host acceptance | 완료 | 실제 NVIDIA host에서 수집 정상 동작 확인. |

## 마지막 확인 결과

2026-05-15 1.0.1 상태 확인:

```sh
git status --short
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest
env GITHUB_REF_NAME=v1.0.1 uv run python scripts/check-tag-version.py
uv build --out-dir /tmp/gua-dist-1.0.1-status
bash scripts/smoke-dist-wheel.sh /tmp/gua-dist-1.0.1-status/gpu_usage_audit-1.0.1-py3-none-any.whl
```

결과:

- 작업트리 clean.
- `ruff check`: pass.
- `ruff format --check`: 26 files already formatted.
- `mypy`: no issues in 25 source files.
- `pytest`: 114 passed.
- tag-version check: `v1.0.1`과 `pyproject.toml` version 일치.
- `uv build`: sdist/wheel build 성공.
- wheel smoke: 성공.
- Release workflow: `v1.0.1` success.
- PyPI latest: `gpu-usage-audit 1.0.1`.

## 1.0.1에서 바뀐 점

- `gua`를 documented command surface로 정리했다.
- `gua daemon`은 collector를 백그라운드로 시작한다.
- `gua daemon --foreground`는 systemd와 debugging 용도로 유지한다.
- `gua start`, `gua status`, `gua stop`을 추가했다.
- README의 install/run/report 예시는 `gua` 기준으로 정리됐다.

## 현재 cleanup 리뷰 결과

- `/tmp/gua.pid` 숫자만 믿고 `gua stop`이 SIGTERM을 보내면 PID 재사용 시 다른
  프로세스를 건드릴 수 있다. pid가 실제 `python -m gpu_usage_audit daemon`
  프로세스인지 확인해야 한다.
- §2 report가 `idle-held`와 `truly-idle`을 모두 "idle/waste"로 합쳐 보여주면
  제품 메시지가 흐려진다. 사용자가 못 쓰는 용량과 실제 빈 용량을 분리해야 한다.
- §4 Top identities는 process row를 바로 세면 같은 사용자의 여러 프로세스가
  같은 GPU/tick에서 과대계상될 수 있다. identity/GPU/tick 단위로 먼저 접어야 한다.
- report는 "sample"의 의미, threshold, `--interval` 의존성을 출력 자체에서 더
  잘 설명해야 한다.
- NVML process list를 읽지 못하는 경우 low-util GPU가 `truly-idle`처럼 보일 수
  있으므로 최소한 경고가 필요하다.

## 로컬 `doctor` 상태

현재 개발 머신은 NVIDIA host가 아니므로 `uv run gua doctor`는 `unsupported`가
정상 결과다.

관찰된 blocker:

- `/dev/nvidia*` 없음.
- `nvidia-smi`가 PATH에 없음.
- NVML init 실패: `libnvidia-ml.so.1` 없음.
- `/tmp/gua.db`가 이미 있어 daemon은 기본 경로로 시작하지 않음.

이 결과는 로컬 환경 한계이며, 제품 regression으로 보지 않는다.

## 다음 작업

1. cleanup PR에서 PID 검증, report 가시성, 문서 정합성을 반영한다.
2. `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`, `uv run pytest`를
   다시 실행한다.
3. 필요하면 1.0.2 patch release 후보로 묶는다.

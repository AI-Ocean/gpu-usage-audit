# Bare Metal 1.0 Handoff

갱신일: 2026-05-15

## 이어받을 때 먼저 볼 것

- `projects/bare-metal-1.0/status.ko.md`: 현재 완료 상태, 1.0.1 검증 결과, 1.0.2 release prep 상태.
- `README.md`: 실제 사용자 문서와 release/install/runbook/report 표면.
- `src/gpu_usage_audit/__main__.py`: `gua` CLI, background daemon lifecycle, PID handling.
- `src/gpu_usage_audit/report.py`: report SQL 집계.
- `src/gpu_usage_audit/render.py`: report 사람이 읽는 출력.
- `.github/workflows/release.yml`: tag release, GitHub Release, PyPI publish 경로.

## 고정된 결정

- 1.0은 단일 로컬 베어메탈 NVIDIA 호스트만 본다.
- Kubernetes, Slurm, Docker/Podman fallback, remote node, cluster-wide report는 1.0 범위 밖이다.
- `nvidia-ml-py`는 기본 dependency다.
- `gpu-usage-audit[nvml]` extra는 compatibility를 위해 빈 alias로 남긴다.
- DB schema는 v1을 유지한다: `host`, `gpu_sample`, `proc_sample`.
- 기본 DB는 `/tmp/gua.db`다.
- `gua daemon`은 기본 백그라운드 실행이다.
- `gua daemon --foreground`는 systemd/debugging 용도다.
- `gua start`는 `gua daemon` alias다.
- `gua status`와 `gua stop`은 pid file 기반 background collector 관리용이다.
- `daemon`은 기존 DB 파일이 있으면 실패한다.
- `report`는 DB 파일이 없으면 실패한다.
- `daemon`과 `demo`는 host row의 `env_kind`를 항상 `"bare"`로 기록한다.
- auto-runtime proposal/project 문서는 삭제했다. Kubernetes/Slurm/Docker/Podman 확장을 다시
  시작하려면 새 proposal로 시작한다.

## 현재 상태

- PR A: implemented in PR #9.
- PR B: implemented in PR #10.
- Post-1.0 cleanup: completed in PR #11.
- Bare-metal 1.0 release: completed in PR #12 and tag `v1.0.0`.
- 1.0.1 command surface/background daemon release: completed in PR #13 and tag `v1.0.1`.
- GitHub Release `v1.0.1`: published.
- PyPI `gpu-usage-audit 1.0.1`: published.
- NVIDIA host acceptance: 사용자가 실제 host에서 수집 정상 동작을 확인했다.
- 1.0.2 release prep: 진행 중. #14 lifecycle/report cleanup을 patch release로 배포한다.
  package version은 `1.0.2`로 bump했고 local build/wheel smoke는 통과했다.

## 마지막 로컬 검증

```sh
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest
uv build --out-dir /tmp/gua-dist-1.0.2-prep
bash scripts/smoke-dist-wheel.sh /tmp/gua-dist-1.0.2-prep/gpu_usage_audit-1.0.2-py3-none-any.whl
env GITHUB_REF_NAME=v1.0.2 uv run python scripts/check-tag-version.py
```

결과는 `pytest` 124 passed, `mypy` 25 source files, `ruff format` 26 files 기준이다.

## 현재 cleanup PR 방향

- `/tmp/gua.pid`가 PID 재사용으로 다른 프로세스를 가리킬 수 있으므로 `status`/`stop` 전에
  해당 PID가 실제 managed `gpu_usage_audit daemon` 프로세스인지 확인한다.
- report §2는 low-util 전체를 "waste"로 합치지 말고 `idle-held`와 `truly-idle`을 분리한다.
- report §4는 process row가 아니라 identity/GPU/tick 단위로 먼저 접어서 사용자별 GPU-hours를 계산한다.
- report 출력 자체에 sample 의미, classification rule, `--interval` 의존성, heatmap 의미를 짧게 노출한다.
- NVML process list 조회 실패는 idle-held를 과소평가할 수 있으므로 warning으로 남긴다.
- 1.0.2 release prep에서는 package version, README release asset 예시, CHANGELOG를 `1.0.2`로 맞춘다.

## 주의할 점

- 현재 로컬 개발 머신은 NVIDIA host가 아니다. `gua doctor`가 unsupported를 내는 것은 정상이다.
- `/tmp/gua.db`가 이미 존재한다. 기본 경로 daemon 실행이 거부되는 것은 기대 동작이다.
- `report --interval`은 daemon 수집 interval과 같아야 GPU-hours가 맞다.
- SQLite WAL sidecar(`*.db-wal`, `*.db-shm`)는 마지막 connection이 닫히면 정리된다.
- 1.0.2를 자를 경우 `env GITHUB_REF_NAME=v1.0.2 uv run python scripts/check-tag-version.py`가
  통과해야 한다.

## 다음 세션 추천 순서

1. `git status --short`로 사용자 변경 여부를 먼저 확인한다.
2. cleanup PR의 CI 결과와 review comments를 확인한다.
3. 필요하면 report wording을 실제 운영자가 읽기 쉬운 형태로 한 번 더 다듬는다.
4. merge 후 patch release가 필요하면 version bump와 changelog를 별도 PR로 처리한다.

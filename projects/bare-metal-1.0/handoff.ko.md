# Bare Metal 1.0 Handoff

갱신일: 2026-05-15

## 이어받을 때 먼저 볼 것

- `projects/bare-metal-1.0/plan.ko.md`: 범위와 PR A-D 계획의 source of truth.
- `projects/bare-metal-1.0/status.ko.md`: 현재 완료/대기 상태와 마지막 검증 결과.
- `README.md`: 실제 사용자 문서와 release/install/runbook 표면.
- `pyproject.toml`: 현재 package version과 dependency 정책.
- `.github/workflows/release.yml`: tag release, GitHub Release, PyPI publish 경로.

## 고정된 결정

- 1.0은 단일 로컬 베어메탈 NVIDIA 호스트만 본다.
- Kubernetes, Slurm, Docker/Podman fallback, remote node, managed
  `gua start/status/stop/uninstall`은 1.0 사용자 표면에서 제외한다.
- `nvidia-ml-py`는 기본 dependency다.
- `gpu-usage-audit[nvml]` extra는 compatibility를 위해 빈 alias로 남긴다.
- DB schema는 v1을 유지한다: `host`, `gpu_sample`, `proc_sample`.
- 기본 DB는 `/tmp/gua.db`다.
- `daemon`은 기존 DB 파일이 있으면 실패한다.
- `report`는 DB 파일이 없으면 실패한다.
- `gua`의 사용자 표면은 `doctor`만 남긴다.
- auto-runtime proposal/project 문서는 삭제했다. Kubernetes/Slurm/Docker/Podman
  확장을 다시 시작하려면 새 proposal로 시작한다.

## 현재 상태

- PR A: implemented in PR #9.
- PR B: implemented in PR #10.
- Post-1.0 cleanup: 완료. auto-runtime 문서와 `RuntimePlan`/env detection
  잔재를 제거했다.
- PR C: implemented in release prep.
- PR D: 진행 중. 현재 버전은 `1.0.0`으로 bump했고, local build/wheel smoke는
  통과했다. NVIDIA host acceptance와 tag publish가 남았다.

마지막 로컬 검증은 모두 통과했다.

```sh
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest
uv build --out-dir /tmp/gua-dist-1.0.0-prep
bash scripts/smoke-dist-wheel.sh /tmp/gua-dist-1.0.0-prep/gpu_usage_audit-1.0.0-py3-none-any.whl
env GITHUB_REF_NAME=v1.0.0 uv run python scripts/check-tag-version.py
```

cleanup 후 결과는 `pytest` 107 passed, `mypy` 25 source files, `ruff format`
26 files 기준이다. release prep에서는 `/tmp/gua-dist-1.0.0-prep`로 build와
wheel smoke를 확인한다.

## 주의할 점

- 현재 로컬 개발 머신은 NVIDIA host가 아니다. `gua doctor`가 unsupported를 내는 것은
  정상이다.
- `/tmp/gua.db`가 이미 존재한다. 기본 경로 daemon 테스트는 이 파일 때문에 실패하는
  것이 기대 동작이다.
- 실제 1.0 acceptance는 NVIDIA 베어메탈 호스트에서만 닫을 수 있다.
- `daemon`과 `demo`는 host row의 `env_kind`를 항상 `"bare"`로 기록한다. 1.0은
  container/k8s runtime 감지를 하지 않는다.
- PR C를 닫기 전에 문서만 보고 끝내지 말고, 기존 DB 존재/부재 error UX가 README와
  CLI 출력에서 서로 같은 메시지를 주는지 확인한다.
- PR D에서 tag를 만들기 전에는 `env GITHUB_REF_NAME=v1.0.0 uv run python
  scripts/check-tag-version.py`가 통과해야 한다.

## 다음 세션 추천 순서

1. `git status --short`로 사용자 변경 여부를 먼저 확인한다.
2. `projects/bare-metal-1.0/status.ko.md`를 읽고 마지막 검증 이후 차이를 확인한다.
3. NVIDIA host acceptance를 실행한다.
4. release prep PR을 main에 머지한다.
5. `v1.0.0` tag를 push하기 전 아래를 다시 실행한다.

```sh
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest
uv build
bash scripts/smoke-dist-wheel.sh
env GITHUB_REF_NAME=v1.0.0 uv run python scripts/check-tag-version.py
```

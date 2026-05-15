# Bare Metal 1.0 Status

갱신일: 2026-05-15

## 요약

Bare Metal 1.0은 단일 NVIDIA 베어메탈 호스트만 대상으로 하는 방향으로 정리되어
있다. PR A/B 범위는 구현 완료 상태이고, 이번 cleanup에서 1.0 이후 확장을 위한
auto-runtime 문서와 코드 잔재를 제거했다. 다음으로는 PR C runbook hardening을
닫을지 확인한 뒤 PR D release prep으로 넘어가면 된다.

cleanup 시작 시 워크트리는 깨끗했다.

## 구현 상태

| 영역 | 상태 | 메모 |
| --- | --- | --- |
| Scope reset | 완료 | Kubernetes/Slurm/Docker/remote/managed runtime 표면 제거. |
| `gua doctor` | 완료 | 현재 머신의 `/dev/nvidia*`, `nvidia-smi -L`, NVML, DB path만 진단. |
| Packaging UX | 완료 | `nvidia-ml-py`가 기본 dependency이고 `nvml` extra는 빈 compatibility alias. |
| `daemon`/`report` DB UX | 구현됨 | 기본 DB는 `/tmp/gua.db`; daemon은 기존 DB를 거부하고 report는 없는 DB를 거부. |
| README bare-metal 문서 | 대부분 완료 | 2-shell flow, systemd 예시, 운영 notes가 들어가 있음. |
| Post-1.0 cleanup | 완료 | auto-runtime proposal/project 문서, k8s/docker env 감지, `RuntimePlan` 잔재 제거. |
| PR C closure | 미확정 | 계획서에는 아직 완료 표시가 없다. README와 CLI UX를 기준으로 닫을지 최종 확인 필요. |
| PR D release prep | 대기 | 현재 package version은 `0.4.1`; 1.0 릴리스 버전 bump와 릴리스 노트 정리가 남음. |
| NVIDIA host acceptance | 미검증 | 현재 로컬 머신에는 NVIDIA device/driver가 없어 실제 host 수집 loop는 확인하지 못함. |

## 검증 결과

2026-05-15 cleanup 후 로컬 검증:

```sh
git status --short
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest
uv build --out-dir /tmp/gua-dist-prune-20260515
bash scripts/smoke-dist-wheel.sh /tmp/gua-dist-prune-20260515/gpu_usage_audit-0.4.1-py3-none-any.whl
env GITHUB_REF_NAME=v0.4.1 uv run python scripts/check-tag-version.py
```

결과:

- `git status --short`: cleanup 변경분만 존재.
- `ruff check`: pass.
- `ruff format --check`: 26 files already formatted.
- `mypy`: no issues in 25 source files.
- `pytest`: 107 passed.
- `uv build`: sdist/wheel build 성공.
- wheel smoke: 성공.
- tag-version check: `v0.4.1`과 `pyproject.toml` version 일치.

## 이번 cleanup 변경

- `proposals/design-auto-runtime*.md` 삭제.
- `projects/auto-runtime-audit/plan*.md` 삭제.
- `src/gpu_usage_audit/env.py`와 `tests/test_env.py` 삭제.
- `daemon`/`demo`는 1.0 계약대로 host `env_kind`를 `"bare"`로 직접 기록.
- `RuntimePlan` 모델 제거. `gua doctor`는 내부 `DoctorPlan`으로 host/unsupported,
  reasons, blockers, warnings만 유지.
- `DoctorPlan` JSON에서 post-1.0 placeholder였던 `scheduler`, `telemetry`,
  `confidence`, `required_privileges`, `actions` 필드 제거.

## 로컬 `doctor` 상태

현재 개발 머신은 NVIDIA host가 아니므로 `uv run gua doctor --json`은
`unsupported`가 정상 결과다.

관찰된 blocker:

- `/dev/nvidia*` 없음.
- `nvidia-smi`가 PATH에 없음.
- NVML init 실패: `libnvidia-ml.so.1` 없음.
- `/tmp/gua.db`가 이미 있어 daemon은 기본 경로로 시작하지 않음.

이 결과는 로컬 환경 한계이며, 제품 regression으로 보지는 않는다. 실제 acceptance는
NVIDIA 베어메탈 호스트에서 다시 실행해야 한다.

## 다음 작업

1. PR C를 닫기 전에 README의 runbook 내용이 `plan.ko.md`의 PR C deliverable을
   모두 만족하는지 확인한다.
2. PR C가 이미 충분하면 `plan.ko.md`에 `Status: implemented`와 체크박스를 반영한다.
3. PR D에서 release target version을 확정한다. 1.0 GA라면 `pyproject.toml`을
   `1.0.0`으로 올리고 README release asset 예시도 맞춘다.
4. 필요하면 `CHANGELOG.md` 또는 수동 release notes를 추가한다. 현재 GitHub Actions는
   tag push 때 git log 기반 release notes를 자동 생성한다.
5. NVIDIA host에서 acceptance command를 실행한다.

```sh
uv tool install gpu-usage-audit
gua doctor
gpu-usage-audit daemon --interval 30s
gpu-usage-audit report --since 1h --interval 30s
```

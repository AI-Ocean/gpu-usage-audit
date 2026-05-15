# Bare Metal 1.0 Status

갱신일: 2026-05-15

## 요약

Bare Metal 1.0은 단일 NVIDIA 베어메탈 호스트만 대상으로 하는 방향으로 정리되어
있다. PR A/B 범위는 구현 완료 상태이고, 다음으로는 PR C runbook hardening을
닫을지 확인한 뒤 PR D release prep으로 넘어가면 된다.

점검 시작 시 워크트리는 깨끗했다. 현재 변경분은 이 `status.ko.md`와
`handoff.ko.md` 추가뿐이다.

## 구현 상태

| 영역 | 상태 | 메모 |
| --- | --- | --- |
| Scope reset | 완료 | Kubernetes/Slurm/Docker/remote/managed runtime 표면 제거. |
| `gua doctor` | 완료 | 현재 머신의 `/dev/nvidia*`, `nvidia-smi -L`, NVML, DB path만 진단. |
| Packaging UX | 완료 | `nvidia-ml-py`가 기본 dependency이고 `nvml` extra는 빈 compatibility alias. |
| `daemon`/`report` DB UX | 구현됨 | 기본 DB는 `/tmp/gua.db`; daemon은 기존 DB를 거부하고 report는 없는 DB를 거부. |
| README bare-metal 문서 | 대부분 완료 | 2-shell flow, systemd 예시, 운영 notes가 들어가 있음. |
| PR C closure | 미확정 | 계획서에는 아직 완료 표시가 없다. README와 CLI UX를 기준으로 닫을지 최종 확인 필요. |
| PR D release prep | 대기 | 현재 package version은 `0.4.1`; 1.0 릴리스 버전 bump와 릴리스 노트 정리가 남음. |
| NVIDIA host acceptance | 미검증 | 현재 로컬 머신에는 NVIDIA device/driver가 없어 실제 host 수집 loop는 확인하지 못함. |

## 검증 결과

2026-05-15 로컬 검증:

```sh
git status --short
uv run pytest
uv run ruff check
uv run ruff format --check
uv run mypy
uv build --out-dir /tmp/gua-dist-check-20260515
bash scripts/smoke-dist-wheel.sh /tmp/gua-dist-check-20260515/gpu_usage_audit-0.4.1-py3-none-any.whl
env GITHUB_REF_NAME=v0.4.1 uv run python scripts/check-tag-version.py
```

결과:

- `git status --short`: 점검 시작 시 변경 없음. 문서 작성 후에는
  `status.ko.md`, `handoff.ko.md`가 새 파일로 남아 있음.
- `pytest`: 118 passed.
- `ruff check`: pass.
- `ruff format --check`: 28 files already formatted.
- `mypy`: no issues in 27 source files.
- `uv build`: sdist/wheel build 성공.
- wheel smoke: 성공.
- tag-version check: `v0.4.1`과 `pyproject.toml` version 일치.

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

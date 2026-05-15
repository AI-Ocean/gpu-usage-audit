# Bare Metal 1.0 개발 계획

상태: 채택
작성일: 2026-05-14
범위: `gpu-usage-audit` 1.0 을 단일 베어메탈 호스트 audit 도구로 완성

## Scope Reset

1.0 제품은 **설치된 현재 머신**만 본다.

`gua` 또는 `gpu-usage-audit` 명령을 실행하는 머신이 곧 audit 대상이다. 이
버전에서는 Kubernetes cluster 전체, Slurm scheduler, Docker/Podman fallback,
remote node, DaemonSet 배포를 제품 표면에 올리지 않는다.

이 결정의 목적은 첫 안정 버전을 다음 한 문장으로 설명 가능하게 만드는 것이다.

```text
현재 베어메탈 NVIDIA 호스트에서 NVML telemetry를 SQLite에 적재하고,
retrospective report로 active / idle-held / truly-idle 상태를 보여준다.
```

## 제품 정의

`gpu-usage-audit` 1.0 은 다음 workflow를 안정적으로 지원한다.

```sh
uv tool install gpu-usage-audit

gua doctor
gua doctor --db /var/lib/gua/gua.db  # optional, daemon DB 경로를 바꿀 때

gpu-usage-audit daemon --interval 30s
gpu-usage-audit report --since 1h --interval 30s
```

`--db`를 생략하면 `daemon`과 `report`는 `/tmp/gua.db`를 사용한다. 새
collection run이 오래된 test DB에 조용히 append하지 않도록 `daemon`은 대상
DB 파일이 이미 있으면 실패한다.

## Target Environment

1.0의 주 환경은 다음 조건을 만족하는 단일 Linux host다.

```text
/dev/nvidia0..N 이 현재 host namespace에서 보임
nvidia-smi -L 이 GPU 목록을 출력함
Python NVML(pynvml) init 성공
NVML device count > 0
```

예상 진단 성공 예:

```text
Scope:
  machine: local

Host GPU:
  /dev/nvidia*: ok, 10 GPU device files
  nvidia-smi: ok, 10 GPUs
  NVML: ok, initialized, GPU count=10

Recommended commands:
  collect: gpu-usage-audit daemon --interval 30s
  report after collecting: gpu-usage-audit report --since 1h --interval 30s
```

## Non-goals For 1.0

아래 항목은 1.0에서 제거하거나 사용자 표면에서 숨긴다.

- Kubernetes cluster scan.
- Kubernetes RuntimeClass/node 조회.
- Kubernetes DaemonSet manifest 생성 또는 적용.
- GPU Operator staged driver 자동 처리.
- Slurm command/config signal과 Slurm allocation join.
- Docker/Podman NVIDIA fallback runtime.
- `gua start/status/stop/uninstall` 기반 managed runtime.
- scheduler allocation-aware classification.
- DB schema v2와 migration.
- remote node 또는 cluster-wide report.

이 항목들은 제품 방향이 다시 넓어질 때 별도 proposal로 되살린다. 1.0의
완성도 기준에는 포함하지 않는다.

## CLI Direction

### `gua doctor`

`gua doctor`는 현재 머신의 베어메탈 readiness만 점검한다.

확인 항목:

- OS/kernel/Python.
- `/dev/nvidia*` GPU/control/uvm device file.
- `nvidia-smi` 존재와 `nvidia-smi -L` 결과.
- NVML load/init/device count/driver version.
- 진단 대상 DB path 상태. 기본값은 `/tmp/gua.db`이고, `--db PATH`로
  daemon/report에 쓸 경로를 미리 점검할 수 있다.

출력 목표:

```text
gua doctor

Scope:
  machine: local

Host GPU:
  /dev/nvidia*: ok, 10 GPU device files
  nvidia-smi: ok, 10 GPUs
  NVML: ok, initialized, GPU count=10, driver 560.35.05

Database:
  default: /tmp/gua.db
  status: absent, ready for a new daemon run

Recommended commands:
  collect: gpu-usage-audit daemon --interval 30s
  report after collecting: gpu-usage-audit report --since 1h --interval 30s
```

실패 출력은 어느 층이 막혔는지 직접 말해야 한다.

예:

```text
Host GPU:
  /dev/nvidia*: ok, 10 GPU device files
  nvidia-smi: ok, 10 GPUs
  NVML: error, pynvml is not importable

Fix:
  - Reinstall the tool environment: uv tool install --force gpu-usage-audit
```

`pynvml`이 import되지만 NVML init이 실패하면 packaging 문제가 아니라 host
driver/NVML 문제로 안내한다.

예:

```text
Host GPU:
  /dev/nvidia*: ok, 10 GPU device files
  nvidia-smi: ok, 10 GPUs
  NVML: error, loadable but init failed: the NVIDIA driver and NVML library versions do not match. Detail: Driver/library version mismatch

Fix:
  - Install or repair the NVIDIA driver so libnvidia-ml.so.1 is available and matches the loaded kernel driver; verify with `nvidia-smi -L`.
```

DB 파일이 이미 있으면 `doctor`는 바로 실패할 daemon 명령을 추천하지 않고,
기존 데이터를 읽는 report 명령만 보여준다.

예:

```text
Database:
  default: /tmp/gua.db
  status: warning, present; daemon will refuse this path, report can read it

Notes:
  - /tmp/gua.db already exists; `gpu-usage-audit daemon` will refuse this path
    until it is removed or another --db path is provided.

Recommended commands:
  report existing data: gpu-usage-audit report --since 1h --interval 30s
```

### `gpu-usage-audit daemon`

1.0의 primary collector다.

요구사항:

- 기본 DB path는 `/tmp/gua.db`.
- DB 파일이 이미 있으면 실패한다.
- NVML init 실패 시 friendly error를 출력한다.
- SIGINT/SIGTERM으로 정상 종료한다.
- tick마다 GPU/process sample을 append한다.

### `gpu-usage-audit report`

1.0의 primary reader다.

요구사항:

- 기본 DB path는 `/tmp/gua.db`.
- DB 파일이 없으면 실패한다.
- daemon이 쓰는 동안에도 읽을 수 있어야 한다.
- 기존 5-section report를 유지한다.

### `gpu-usage-audit demo`

GPU 없는 환경의 형식 확인용으로 유지한다. 제품 핵심 workflow는 아니다.

## Packaging Direction

베어메탈 전용 제품으로 정리하면 NVML은 optional feature가 아니라 사실상 핵심
기능이다. PR B에서는 1번을 선택한다. GPU 없는 개발/문서/CI 환경에서는
`nvidia-ml-py` import는 가능하고, driver/NVML init만 실패해야 하며 `demo`와
tests는 계속 동작해야 한다. 기존 `gpu-usage-audit[nvml]` 설치 습관은 깨지지
않게 빈 compatibility extra로 남긴다.

1. `nvidia-ml-py`를 기본 dependency로 올린다.
   - 장점: `uv tool install gpu-usage-audit` 후 바로 host NVML 진단 가능.
   - 단점: GPU 없는 사용자의 기본 설치에도 NVML Python package가 들어간다.

2. core dependency는 0으로 유지하고 doctor가 설치 명령을 강하게 안내한다.
   - 장점: 현재 packaging 철학 유지.
   - 단점: 베어메탈 사용자가 추가 `--with nvidia-ml-py` 단계를 밟아야 한다.
   - PR B 결정: 채택하지 않음. 단, 기존 `gpu-usage-audit[nvml]` 설치 습관을
     깨지 않도록 empty extra alias는 유지한다.

## Data Model For 1.0

1.0에서는 현재 schema를 유지한다.

- `host`
- `gpu_sample`
- `proc_sample`

새 scheduler allocation table, node table, owner table, migration은 넣지 않는다.
1.0에서 중요한 것은 실제 host daemon/report loop가 베어메탈에서 안정적으로
동작하는 것이다.

## Implementation Plan

### PR A: Bare Metal Scope Reset

Status: implemented in PR #9.

Deliver:

- [x] auto-runtime doctor 구현 제거 또는 축소.
- [x] `gua doctor`를 local machine / host NVML readiness 전용으로 재작성.
- [x] k8s/slurm/docker signal 제거.
- [x] auto-runtime `RuntimePlan` 잔재를 제거하고 `gua doctor` 내부의
  `DoctorPlan`으로 축소.
- [x] README의 제품 설명을 single-host bare-metal 중심으로 재정렬.
- [x] `gua start/status/report/stop/uninstall` placeholder 사용자 표면 제거.
- [x] `gua doctor --db PATH`로 실제 daemon/report DB 경로를 점검.

Working state:

- `gua doctor`가 베어메탈 host에서 daemon/report 실행 가능 여부를 설명한다.
- cluster-wide signal은 출력하지 않는다.
- DB가 없으면 collect/report 명령을 보여주고, DB가 이미 있으면 report-only
  명령을 보여준다.

### PR B: Packaging And Install UX

Status: implemented in PR #10.

Deliver:

- [x] `nvidia-ml-py`를 기본 dependency로 올릴지 결정하고 반영.
- [x] install 문서를 `uv tool install gpu-usage-audit` 중심으로 단순화.
- [x] NVML 미설치/driver mismatch/fail case 메시지 정리.

Working state:

- 사용자는 베어메탈 host에서 설치 후 바로 `gua doctor`를 실행할 수 있다.
- 기존 `gpu-usage-audit[nvml]` 설치 명령은 warning 없이 통과하지만,
  `nvidia-ml-py`는 기본 dependency로 설치된다.
- NVML init 실패는 에러 코드 기반으로 driver not loaded / missing
  `libnvidia-ml.so.1` / driver-library mismatch를 구분한다.

### PR C: Bare Metal Runbook Hardening

Status: implemented in release prep.

Deliver:

- `/tmp/gua.db` 기본 flow 문서 강화.
- 기존 DB 존재/부재 error UX 확정.
- long-running daemon 운영 notes 정리.
- systemd 예시는 optional advanced section으로 유지하거나 제거.

Working state:

- 사용자는 2개 shell로 daemon/report를 안정적으로 시도할 수 있다.

### PR D: 1.0 Release Prep

Status: in progress.

Deliver:

- [x] version bump.
- [x] README status 갱신.
- [x] changelog/release notes.
- [x] build + wheel smoke.
- [ ] GitHub Release + PyPI publish.

Working state:

- `uv tool install gpu-usage-audit`로 베어메탈 1.0 workflow를 설치할 수 있다.

## Acceptance Criteria

베어메탈 NVIDIA host에서 다음이 가능해야 한다.

```sh
uv tool install gpu-usage-audit
gua doctor
gpu-usage-audit daemon --interval 30s
gpu-usage-audit report --since 1h --interval 30s
```

성공 기준:

- `gua doctor`가 current machine만 진단한다.
- `gua doctor`가 NVML readiness를 명확히 말한다.
- `daemon`이 `/tmp/gua.db`에 sample을 적재한다.
- `report`가 같은 DB에서 active / idle-held / truly-idle report를 출력한다.
- Kubernetes/Slurm/Docker/Podman 관련 메시지가 기본 사용자 표면에 나오지 않는다.
- DB schema 변경 없이 기존 tests가 통과한다.

## Deferred Work

아래는 1.0 GA 전 또는 이후 다시 검토할 수 있는 운영 품질 항목이다. Kubernetes,
Slurm, Docker/Podman, scheduler allocation, managed runtime 같은 1.0 이후
제품 확장은 현재 코드베이스와 프로젝트 문서에서 제거했다. 다시 진행하려면 새
proposal로 시작한다.

- `nvidia-ml-py` upper bound 정책 (`>=12.535,<13` 같은 known-good range 여부).
- `NVMLInfo.failure_kind` 같은 구조적 실패 타입 도입.
- unsupported text output에 `Blockers:` 섹션을 별도로 노출할지 결정.
- raw NVML detail의 redact 옵션 또는 JSON 필드 분리.

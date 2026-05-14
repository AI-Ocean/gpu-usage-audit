# Auto Runtime Audit 개발 계획

상태: 초안
범위: auto-runtime architecture 제안을 구현하기 위한 개발 계획

## 목표

`gpu-usage-audit`를 실제 GPU telemetry와 scheduler allocation context를
결합하는 retrospective audit 도구로 만든다.

제품은 다음 질문에 답해야 한다.

- 누가 GPU capacity를 할당받았는가?
- 할당받은 GPU를 실제로 사용했는가?
- scheduler allocation 없이 GPU를 사용한 주체는 누구인가?
- 어떤 GPU가 memory-held 상태였지만 compute-idle이었는가?

구현은 top-down으로 진행한다. 먼저 사용자에게 보일 report, runtime plan,
data model, fake end-to-end flow를 정의한다. 그 다음 실제 host, Kubernetes,
Slurm adapter를 붙인다.

## 기대 아키텍처

기대하는 module 경계:

```text
gpu_usage_audit/
  cli/              # gua doctor/start/status/report/stop
  doctor/           # environment check와 RuntimePlan 생성
  runtime/          # collector가 어디에서 실행되는가
  telemetry/        # 실제 GPU fact, 보통 NVML
  scheduler/        # allocation과 ownership context
  attribution/      # PID -> pod/job/user 매핑
  storage/          # SQLite schema, migration, export, rollup
  report/           # classification, aggregation, rendering
  packaging/        # systemd unit, k8s manifest, OCI image
```

핵심 분리:

```text
Runtime placement: collector process가 어디에서 실행되는가?
Telemetry source: 실제 GPU 상태를 어떻게 관측하는가?
Scheduler context: 누가 GPU capacity를 할당받았는가?
Attribution: 관측된 PID를 owner로 어떻게 되돌려 매핑하는가?
Report model: telemetry와 allocation을 어떻게 결합하는가?
```

Kubernetes와 Slurm은 scheduler context provider다. telemetry source가 아니다.
기본 telemetry source는 계속 NVML이다.

## 지원 영역

| 영역 | Runtime | Telemetry | Scheduler | 기대 기능 |
|---|---|---|---|---|
| Bare metal | host systemd 또는 foreground | NVML | none | active / idle-held / truly-idle |
| Bare metal + Slurm | host systemd | NVML | Slurm | job, user, account audit |
| Kubernetes / GPU Operator | DaemonSet | pod 내부 NVML | Kubernetes | pod와 namespace audit |
| Local Docker/Podman | local container | container 내부 NVML | none | host 직접 실행이 불가능할 때 fallback |
| Demo/test | foreground | fake | fake 또는 none | GPU 접근 없이 제품 의미 검증 |

## Delivery 원칙

- 모든 PR은 독립적으로 merge 가능해야 하며, merge 후 프로젝트는 동작 가능한
  상태여야 한다.
- 새 `gua` command surface를 도입하는 동안 기존 command는 compatibility
  alias로 유지할 수 있다.
- detection은 read-only여야 한다. package를 설치하거나 system/cluster 상태를
  변경하면 안 된다.
- `start`는 system 또는 cluster 상태를 변경하기 전에 concrete plan을 보여줘야
  한다.
- runtime placement와 scheduler context는 독립적으로 감지해야 한다.
- fake telemetry와 fake scheduler flow로 실제 cluster integration 전에 report
  semantics를 검증해야 한다.

## PR 계획

### PR 1: Proposal And Roadmap

현재 PR.

Deliver:

- Auto-runtime architecture proposal.
- 한국어 번역본.
- 이 PR 단위 개발 계획.

Working state:

- 문서 변경만 포함한다.
- runtime behavior 변경은 없다.

Merge 전 정리:

- runtime placement와 scheduler context가 독립적이라는 점을 명확히 한다.
- Kubernetes owner identity는 안정적인 UID를 기준으로 두고, namespace/name은
  display field로 둔다.
- GPU request 없이 `NVIDIA_VISIBLE_DEVICES=all`이 있는 경우 anomaly로 다루되,
  이 collector, DCGM, NVIDIA device/plugin component 같은 GPU management
  agent는 명시적으로 예외 처리한다.
- 의도하지 않은 Markdown trailing whitespace를 제거한다. 단, hard line break가
  의도된 경우는 예외다.

### PR 2: Command Surface Skeleton

Deliver:

- `gua` console entry point.
- Top-level commands:

```sh
gua doctor
gua start --dry-run
gua status
gua report
gua stop
gua uninstall
```

- 기존 `gpu-usage-audit daemon/report/demo` compatibility path.
- unsupported 또는 아직 설치되지 않은 mode에 대한 명확한 placeholder behavior.
- CLI smoke test.

Working state:

- 사용자는 새 command surface를 실행해볼 수 있다.
- 기존 문서화된 command는 계속 동작한다.
- `start/status/stop`은 아무것도 조용히 변경하지 않는다.

### PR 3: RuntimePlan And Doctor V1

Deliver:

- `RuntimePlan` model.
- `gua doctor` human-readable output.
- `gua doctor --json`.
- `gua start --dry-run`에서 recommended plan 출력.
- 다음 항목에 대한 read-only check:
  - OS/kernel/Python.
  - `/dev/nvidia*`.
  - NVML load/init/device count.
  - `kubectl` 존재와 auth.
  - Kubernetes runtime signal.
  - Slurm command/config signal.
  - Docker/Podman NVIDIA fallback signal.

Working state:

- 사용자는 아무것도 설치하지 않고 현재 machine에 어떤 runtime path가 추천되는지
  이해할 수 있다.

### PR 4: Data Model V2 And Migration

Deliver:

- Schema versioning과 migration.
- `node`.
- 확장된 `gpu_sample`.
- `gpu_process_sample`.
- `allocation_sample`.
- `owner_sample`.
- legacy DB read compatibility.

Working state:

- 기존 host daemon/report behavior가 새 schema에서도 계속 동작한다.
- scheduler allocation이 없어도 report는 기존 active / idle-held / truly-idle
  view를 출력한다.

### PR 5: Combined Classification And Fake Scheduler

Deliver:

- Allocation-aware classification:

```text
allocated-active
allocated-idle-held
allocated-unused
unallocated-active
unallocated-idle-held
truly-idle
unknown-active
unknown-idle-held
unknown-unused
```

- Fake scheduler adapter.
- allocated, unallocated, unknown allocation state를 모두 포함하는 demo data.
- combined class report section.
- classification과 report aggregation test.

Working state:

- 실제 GPU, Kubernetes, Slurm 없이도 최종 제품 의미를 검증할 수 있다.

### PR 6: Install State And Local Host Runtime

Deliver:

- Local install state file.
- Default DB path.
- Host foreground runtime adapter.
- `gua start --mode host --foreground`.
- `gua status`.
- `--db`가 생략되면 state를 사용하는 `gua report --since ...`.
- 가능한 foreground/state-aware flow에서 `gua stop`.

Working state:

- Single-host 사용자는 매 command마다 직접 `--db`를 넘기지 않고 새 `gua`
  workflow를 사용할 수 있다.

### PR 7: Systemd Host Runtime

Deliver:

- systemd unit template.
- `gua start --mode host`.
- `gua stop`.
- `gua uninstall`.
- `gua uninstall --delete-data`.
- `--dry-run`과 `--yes`.
- root/permission diagnostic.
- 기본 data 보존.

Working state:

- bare-metal host collection을 새 UX로 설치, 중지, 제거할 수 있다.

### PR 8: Kubernetes Manifest Dry Run

Deliver:

- 내장 Kubernetes manifest template.
- Namespace, ServiceAccount, RBAC, ConfigMap, DaemonSet rendering.
- GPU-capable node targeting logic.
- `hostPID: true` 기본값.
- `--no-host-pid` opt-out.
- plan output의 security와 RBAC 설명.

Working state:

- 사용자는 Kubernetes cluster에 무엇이 설치될지 apply 없이 정확히 검토할 수 있다.

### PR 9: Kubernetes Runtime Adapter

Deliver:

- 공식 OCI image path.
- `gua start --mode k8s`.
- `gua status --mode k8s`.
- `gua stop --mode k8s`.
- `kubectl apply/delete` integration.
- Collector pod discovery.
- Node별 hostPath SQLite DB.
- Node-level last-sample status.

Working state:

- Kubernetes GPU node에서 DaemonSet으로 collector를 실행할 수 있다.
- Scheduler attribution은 아직 limited일 수 있다.

### PR 10: Kubernetes Report Export

Deliver:

- `gua report --since ... --node NODE`.
- `gua report --since ... --all-nodes`.
- Collector pod fan-out.
- Windowed export.
- JSONL export format.
- Parallel collection.
- `pods/exec` RBAC diagnostic.

Working state:

- 사용자는 per-node collector database에서 cluster-level report를 만들 수 있다.

### PR 11: Kubernetes Scheduler Attribution

Deliver:

- Kubernetes API owner snapshot.
- Pod UID 기반 owner identity.
- PodResources API integration.
- Pod resource request/limit parsing.
- `/proc/<pid>/cgroup` PID-to-pod mapping.
- cgroup v1/v2 parser coverage.
- `NVIDIA_VISIBLE_DEVICES=all` anomaly detection.
- GPU management pod exception.

Working state:

- Kubernetes report에서 pod/namespace별 allocated-active, allocated-unused,
  unallocated-active, unallocated-idle-held를 볼 수 있다.

### PR 12: Slurm Doctor And Scheduler Adapter

Deliver:

- Doctor의 Slurm detection.
- `scontrol`, `squeue`, optional `sacct` integration.
- Node-level running job allocation snapshot.
- job/user/account owner model.
- requested GPU count.
- cgroup PID-to-job mapping.
- best-effort exact GPU-to-job mapping.

Working state:

- Slurm compute node에서 job, user, account별 GPU usage report가 동작한다.

### PR 13: Rollup And Retention

Deliver:

- Raw sample retention policy.
- 1-minute rollup table.
- Combined class rollup.
- Cleanup command.
- raw와 rollup window를 함께 읽는 report.

Working state:

- 장기 실행 collector가 core audit class를 잃지 않으면서 DB size를 통제한다.

### PR 14: Packaging And Release Polish

Deliver:

- host, Kubernetes, Slurm, demo path를 위한 README quickstart.
- Troubleshooting matrix.
- Wheel release verification.
- OCI image release workflow.
- Manifest path가 안정화되었다면 optional Helm chart.

Working state:

- 새 사용자가 문서만 보고 install, start, inspect, report, uninstall을 진행할 수
  있다.

## 권장 Merge 순서

핵심 foundation은 PR 2부터 PR 5까지다.

```text
CLI surface -> RuntimePlan/doctor -> schema V2 -> combined report semantics
```

그 다음 host, Kubernetes, Slurm은 안정된 contract 위에 붙는 adapter 작업이 된다.

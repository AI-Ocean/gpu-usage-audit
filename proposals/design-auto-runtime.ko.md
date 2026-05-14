# gpu-usage-audit 자동 런타임 설계

상태: 초안
작성일: 2026-05-12

## 개요

`gpu-usage-audit`는 사용자가 현재 머신이 베어메탈인지, Kubernetes인지,
컨테이너 런타임 호스트인지, Slurm compute node인지 몰라도 시작할 수 있는
도구가 되어야 한다.

목표 UX:

```sh
gua doctor
gua start

# 며칠 뒤
gua status
gua report --since 3d
gua stop
```

제품은 적절한 collector 실행 방식을 자동으로 감지해야 한다. 단, 그 결정을
숨기면 안 된다. 사용자는 배포 모델을 미리 알 필요가 없어야 하지만, `gua`는
무엇을 선택했고 왜 그렇게 판단했는지 명확히 보여줘야 한다.

예:

```text
Detected environment:
  host NVML: initialized, GPU count=0
  kubernetes: available
  k8s NVIDIA runtime: available
  slurm: not detected

Recommended plan:
  runtime: k8s-daemonset
  telemetry: nvml
  scheduler: k8s

Reason:
  GPUs are not visible from the host namespace, but they are visible inside
  Kubernetes containers with NVIDIA_VISIBLE_DEVICES=all.
```

이것이 제품의 주요 변화다. `daemon`은 저수준 collector로 남기고,
`gua start`가 launcher/orchestrator 역할을 맡는다.

## 동기와 차별점

이 프로젝트가 가져가야 할 영역은 raw GPU telemetry 그 자체가 아니다.
DCGM exporter, `nvidia-smi`, 여러 Grafana dashboard는 이미 utilization,
memory, temperature, process-level fact를 잘 보여준다. Slurm accounting,
Kubernetes metadata, cluster dashboard도 scheduler-side allocation과
ownership을 보여준다.

비어 있는 영역은 둘을 retrospective하게 join한 뷰다.

```text
누가 GPU를 할당받았고, 그 GPU가 실제로 유의미한 일을 했는가?
scheduler allocation 없이 GPU를 사용한 주체는 누구인가?
어떤 GPU가 memory-held 상태였지만 compute-idle이었는가?
어떤 GPU가 할당됐지만 의미 있는 GPU process가 전혀 없었는가?
```

이 combined view가 핵심 가치다. 따라서 `gpu-usage-audit`는 또 하나의 live
GPU monitor가 되면 안 된다. 실제 NVML 관측과 scheduler context를 결합하는
가벼운 retrospective audit 도구가 되어야 한다.

가장 중요한 headline class는 다음이다.

```text
allocated-idle-held     # scheduler가 할당했고, process가 memory를 잡았지만 compute는 차가움
allocated-unused        # scheduler가 할당했지만, NVML상 의미 있는 사용이 없음
unallocated-active      # scheduler allocation 없이 GPU가 사용됨
unallocated-idle-held   # scheduler allocation 없이 GPU memory가 잡힘
```

Kubernetes에서 `nvidia.com/gpu` request 없이 `NVIDIA_VISIBLE_DEVICES=all`이
있는 pod는 first-class anomaly다. 이 pod는 scheduler accounting에 잡히지
않는 GPU 접근 권한을 가질 수 있다. 이는 표준 GPU telemetry나 kube-state류
metadata만으로는 만들어지지 않는 신호다.

## 제품 목표

1. **첫 사용에 환경 지식이 필요 없어야 한다**
   - 사용자는 node가 베어메탈인지, k8s인지, Docker인지, Slurm인지 몰라도
     `gua doctor`나 `gua start`를 실행할 수 있어야 한다.

2. **마법처럼 숨기지 말고 투명해야 한다**
   - auto mode는 선택한 plan, 판단 이유, 필요한 권한, 저장 위치, cleanup
     명령을 출력해야 한다.
   - 고급 사용자는 `--mode host`, `--mode k8s`, `--mode slurm`,
     `--mode container`로 명시 override할 수 있어야 한다.

3. **Retrospective audit이 우선이다**
   - 핵심 가치는 "지난 N시간/일 동안 무엇이 있었는가?"다.
   - live dashboard, quota, scheduling decision, remediation은 첫 제품
     표면이 아니다.

4. **실제 GPU 사용과 scheduler allocation을 모두 측정한다**
   - NVML은 "GPU가 일을 하고 있는가, memory를 잡고 있는가?"에 답한다.
   - k8s/Slurm은 "이 GPU가 workload에 할당됐는가?"에 답한다.
   - report는 둘을 결합해야 한다.

5. **운영 부담이 낮아야 한다**
   - 기본 저장소는 SQLite로 유지한다.
   - 기본 경로에는 database service, web server, Prometheus, Grafana가
     필요하지 않아야 한다.

6. **실패 모드가 좋아야 한다**
   - `gua`가 실행될 수 없다면 driver, NVML, device visibility, container
     runtime, kubectl auth, Slurm config, permission 중 어느 층이 실패했는지
     말해야 한다.

## 비목표

- Slurm, Kubernetes, DCGM, Prometheus, Grafana, Open OnDemand, cluster
  dashboard를 대체하지 않는다.
- quota를 enforce하거나 job을 kill하지 않는다.
- workload scheduling을 하지 않는다.
- 최소 제품에서 central server를 요구하지 않는다.
- 모든 설치를 silent하게 만들지 않는다. system 또는 cluster 상태 변경은
  명시적이어야 한다.

## 지원 환경 분류

### 1. 베어메탈 host

전형적인 형태:

```text
/dev/nvidia0..N 이 host에서 보임
host NVML init 성공
host NVML device count > 0
scheduler가 없거나 scheduler context 비활성
```

Runtime:

```text
runtime: host-systemd or host-foreground
telemetry: nvml
scheduler: none
```

현재 프로젝트와 가장 가까운 형태다.

### 2. Kubernetes / GPU Operator

전형적인 형태:

```text
host에는 /dev/nvidiactl만 보일 수 있음
host NVML device count가 0일 수 있음
GPU device는 pod 안에 inject됨
runtimeClassName=nvidia가 있을 수 있음
NVIDIA_VISIBLE_DEVICES가 device 노출을 제어함
```

Runtime:

```text
runtime: k8s-daemonset
telemetry: nvml
scheduler: k8s
```

GPU가 container namespace 안에서만 보일 수 있으므로 collector는 Kubernetes
안에서 실행되어야 한다.

사용자가 Docker를 직접 build하거나 run할 필요는 없어야 한다. 제품 내부에서
공식 OCI image를 사용하는 것은 괜찮다.

### 3. Slurm compute node

전형적인 형태:

```text
host /dev/nvidia0..N 이 보임
Slurm이 GPU를 GRES로 관리함
job이 --gres=gpu:N 또는 --gpus=N 으로 GPU를 요청함
Slurm이 job step 안에 CUDA_VISIBLE_DEVICES를 설정함
cgroup이 visible device file을 제한할 수 있음
```

Runtime:

```text
runtime: host-systemd or host-foreground
telemetry: nvml
scheduler: slurm
```

Slurm 지원의 핵심은 NVML을 동작시키는 것이 아니다. NVML 사용 상태와 Slurm
allocation state를 결합하는 것이다.

### 4. 로컬 컨테이너 런타임

전형적인 형태:

```text
host command를 직접 실행할 수 없거나 직접 실행하면 안 됨
docker/podman이 NVIDIA container를 실행할 수 있음
docker run --gpus all ... 에서 GPU가 보임
```

Runtime:

```text
runtime: local-container
telemetry: nvml
scheduler: none
```

fallback으로는 유용하지만, 기본 UX가 되어서는 안 된다.

## 핵심 아키텍처

collector와 report 코드 전체에 환경 분기를 퍼뜨리면 안 된다. 제품을 세 축으로
분리한다.

```text
1. Collector Runtime
   collector process가 어디에서 실행되는가?

2. Telemetry Source
   실제 GPU 상태를 어떻게 읽는가?

3. Scheduler Context
   GPU가 누구에게 예약/할당되었는가?
```

구체적 조합:

| Environment | Runtime | Telemetry | Scheduler |
|---|---|---|---|
| Bare metal | host-systemd | nvml | none |
| Kubernetes / GPU Operator | k8s-daemonset | nvml | k8s |
| Slurm | host-systemd | nvml | slurm |
| Docker-only | local-container | nvml | none |
| Demo/test | foreground | fake | none/fake |

중요한 규칙:

```text
Kubernetes와 Slurm은 telemetry source가 아니다.
telemetry source는 여전히 NVML이다.
Kubernetes와 Slurm은 runtime placement와 allocation context를 제공한다.
```

## CLI 설계

### 기본 명령

```text
gua doctor
gua start
gua status
gua report
gua stop
gua uninstall
```

### 저수준 명령

아래 명령은 유지할 수 있지만 첫 사용 UX의 중심이 되어서는 안 된다.

```text
gua daemon run
gua daemon export
gua db inspect
```

현재 `gpu-usage-audit daemon`과 `gpu-usage-audit report`는 migration 기간에
compatibility alias로 남길 수 있다.

### `gua doctor`

읽기 전용 환경 진단 명령이다.

기본 출력은 사람이 읽기 쉬운 형태다. 자동화에는 `--json`을 사용한다.

예:

```sh
gua doctor
gua doctor --json
gua doctor --mode k8s
```

Doctor가 확인할 항목:

- OS, kernel, Python, uv/pipx 가용성
- `/dev/nvidia*`
- host NVML load/init/device count
- `/run/nvidia/driver` 아래 GPU Operator staged NVML
- staged NVML path를 host mode에 써야 하는지 여부
- `nvidia-smi` 존재 여부
- `kubectl` 가용성과 인증 상태
- k8s runtime class
- k8s GPU pod/DaemonSet
- 필요한 k8s resource를 만들 수 있는 권한
- Slurm command와 node GRES
- Docker/Podman NVIDIA runtime fallback

Doctor는 `RuntimePlan`을 만든다.

### `gua start`

기본 mode는 `auto`다.

```sh
gua start
gua start --mode auto
gua start --mode host
gua start --mode k8s
gua start --mode slurm
gua start --mode container
gua start --dry-run
gua start --yes
```

동작:

1. doctor를 실행한다.
2. runtime plan을 선택한다.
3. plan을 출력한다.
4. system이나 cluster 상태를 변경하는 작업이라면 TTY에서 확인을 받는다.
5. install state를 local에 저장한다.

예:

```text
Plan:
  mode: k8s-daemonset
  namespace: gpu-usage-audit
  image: ghcr.io/AI-Ocean/gpu-usage-audit:0.4.0
  db: hostPath /var/lib/gpu-usage-audit/gua.db
  nodes: GPU-capable nodes
  cleanup: gua stop --mode k8s

Continue? [y/N]
```

### `gua status`

설치/실행 중인 collector 상태를 보여준다.

```text
mode: k8s-daemonset
collectors:
  gpusystem: running, last sample 12s ago, GPUs visible=10
  ds02: running, last sample 10s ago, GPUs visible=4
storage:
  per-node SQLite under /var/lib/gpu-usage-audit/gua.db
```

### `gua report`

기본적으로 저장된 install state를 사용한다.

```sh
gua report --since 24h
gua report --since 3d --node gpusystem
gua report --since 3d --all-nodes
gua report --db /var/lib/gpu-usage-audit/gua.db --since 3d
```

k8s에서는 사용자가 DB 위치를 알 필요가 없어야 한다. CLI가 collector pod를
발견하고 `kubectl exec` 등을 통해 export stream을 받아 local에서 집계할 수
있다.

### `gua stop`과 `gua uninstall`

`stop`은 기본적으로 collector를 멈추되 data는 보존해야 한다.

`uninstall`은 설치된 resource를 제거하고, 선택적으로 data도 지울 수 있다.

```sh
gua stop
gua uninstall
gua uninstall --delete-data
```

## RuntimePlan 인터페이스

detector는 바로 실행하지 말고 구조화된 plan을 만들어야 한다.

개념 모델:

```python
class RuntimePlan:
    mode: Literal[
        "host-systemd",
        "host-foreground",
        "k8s-daemonset",
        "local-container",
        "unsupported",
    ]
    telemetry: Literal["nvml", "fake"]
    scheduler: Literal["none", "k8s", "slurm"]
    confidence: Literal["high", "medium", "low"]
    reasons: list[str]
    blockers: list[str]
    warnings: list[str]
    required_privileges: list[str]
    actions: list[PlannedAction]
```

Runtime adapter가 plan을 소비한다.

```text
HostRuntimeAdapter
K8sRuntimeAdapter
ContainerRuntimeAdapter
```

Scheduler adapter는 snapshot을 enrich한다.

```text
NoSchedulerAdapter
K8sSchedulerAdapter
SlurmSchedulerAdapter
```

Telemetry adapter는 hardware fact를 만든다.

```text
NVMLTelemetry
FakeTelemetry
```

## 감지 순서

Auto mode는 모든 GPU를 볼 수 있는 가장 덜 놀라운 runtime을 선호해야 한다.

제안 순서:

1. Host NVML
   - host NVML이 GPU를 보면 host runtime은 viable하다.
   - Slurm이 감지되면 scheduler context는 `slurm`이다.
   - 아니면 scheduler context는 `none`이다.
   - host NVML이 version mismatch로 실패했지만 `/run/nvidia/driver` 아래
     GPU Operator staged NVML이 있으면 plan에 host runtime remediation을
     기록한다.
     - pynvml import 전에 `LD_LIBRARY_PATH`를 prepend하여 re-exec하거나,
     - collector 시작 전에 library path를 설정하는 작은 launcher wrapper를
       사용한다.
     pynvml/libnvidia-ml이 이미 load된 뒤 `LD_LIBRARY_PATH`를 바꾸는 것은
     충분하지 않다.

2. Kubernetes
   - host NVML이 GPU를 보지 못하지만 k8s가 있고 NVIDIA runtime이 pod 안에
     GPU를 노출할 수 있으면 `k8s-daemonset`을 사용한다.
   - `node.status.capacity["nvidia.com/gpu"]`만 믿지 않는다. 일부 cluster는
     accounting이 unusual/custom이어도 pod 안에 GPU를 노출한다.

3. Local container runtime
   - Docker/Podman이 all GPU를 가진 NVIDIA container를 실행할 수 있으면
     `local-container`를 사용한다.

4. Unsupported
   - 가장 가까운 viable path를 설명한다.

중요: detection은 package를 설치하거나 cluster를 변경하면 안 된다.

## Kubernetes Runtime 설계

### 설치 형태

최소 설치:

```text
Namespace: gpu-usage-audit
DaemonSet: gpu-usage-audit
ServiceAccount: gpu-usage-audit
ConfigMap: collector config
hostPath DB: /var/lib/gpu-usage-audit/gua.db
```

DaemonSet 요구사항:

```yaml
runtimeClassName: nvidia
hostPID: true
env:
  - name: NVIDIA_VISIBLE_DEVICES
    value: all
  - name: NVIDIA_DRIVER_CAPABILITIES
    value: compute,utility
```

가능한 mount:

```text
/var/lib/gpu-usage-audit         read-write DB hostPath
/proc                            read-only host process metadata, if needed
/var/lib/kubelet/pod-resources   read-only pod resources socket, if available
```

`hostPID: true`는 node-wide process attribution에 중요하다. NVML은 GPU
process PID를 보고할 수 있지만, host PID visibility가 없으면 collector가
그 PID를 `/proc/<pid>/cgroup`으로 다시 매핑하지 못할 수 있다.

기본값은 `hostPID: true`가 되어야 하며 opt-out을 제공한다. 일부 cluster는
restricted Pod Security profile을 강제하므로
`gua start --mode k8s --no-host-pid`가 가능해야 한다. 단, plan은
process-to-pod attribution이 약해진다고 명확히 말해야 한다.

DaemonSet은 기본적으로 모든 node가 아니라 GPU-capable node만 대상으로 해야
한다. 선호 selector:

```text
nvidia.com/gpu.present=true
feature.node.kubernetes.io/pci-10de.present=true
```

GPU Feature Discovery / Node Feature Discovery label이 없다면 더 넓은
DaemonSet을 설치한 뒤 collector self-check로 fallback할 수 있다.

### Kubernetes Allocation Context

k8s adapter는 세 데이터 source를 결합해야 한다.

1. Kubernetes API
   - Pod, namespace, node name, owner reference, resource request/limit.

2. Kubelet PodResources API
   - 어떤 pod/container가 어떤 GPU device ID를 받았는지에 대한 가장 좋은
     source.

3. Host `/proc/<pid>/cgroup`
   - 관측된 GPU process PID를 pod/container로 매핑하는 가장 좋은 source.

이 구분이 중요한 이유는, 관측한 cluster에 다음 형태의 pod가 있었기 때문이다.

```text
NVIDIA_VISIBLE_DEVICES=all
no nvidia.com/gpu request
all GPUs visible inside the container
```

이 pod들은 scheduler accounting이 깨끗하게 표현하지 못하는 방식으로 GPU를
쓸 수 있다.

adapter는 다음을 명시적으로 감지해야 한다.

```text
NVIDIA_VISIBLE_DEVICES=all
NVIDIA_VISIBLE_DEVICES=<GPU UUID list>
no nvidia.com/gpu request or limit
```

이는 raw environment variable로만 저장하지 말고 scheduler-accounting
anomaly로 표면화해야 한다.

### Cgroup 호환성

Process attribution은 `/proc/<pid>/cgroup`에 의존하지만 cgroup v1과 unified
cgroup v2는 path 표현이 다르다. Kubernetes와 Slurm 배포 모두 cgroup v2로
이동하는 추세다.

parser는 k8s adapter와 Slurm adapter가 공유하는 module이어야 한다. 지원할
항목:

```text
cgroup v1 controller-specific lines
cgroup v2 unified `0::/path` lines
systemd slice escaping
containerd / CRI-O pod and container IDs
Slurm job_<id> and step_<id> paths
```

process-to-owner attribution 구현 전에 이 결정을 내려야 한다.

### Kubernetes Report 의미론

report는 scheduler allocation과 실제 GPU state를 모두 보여줘야 한다.

```text
allocated-active
allocated-idle-held
allocated-unused
unallocated-active
unallocated-idle-held
truly-idle
```

정의:

```text
allocated-unused = scheduler가 GPU를 할당했지만 의미 있는 NVML process/memory가 없음
unallocated-active = NVML상 사용이 있지만 scheduler allocation이 없거나 알 수 없음
unallocated-idle-held = scheduler allocation 없이 memory가 잡힘
truly-idle = allocation도 없고 의미 있는 NVML 사용도 없음
```

## Slurm Runtime 설계

Slurm은 일반적으로 GPU를 GRES로 관리한다.

중요한 Slurm 사실:

- GPU는 보통 `Name=gpu`인 GRES로 설정된다.
- job은 `--gres=gpu:N`, `--gpus=N` 또는 관련 flag로 GPU를 요청한다.
- Slurm은 job step에 `CUDA_VISIBLE_DEVICES`를 설정한다.
- Slurm은 cgroup으로 visible device file을 제한할 수 있다.
- Slurm은 `gres.conf`에서 NVML을 통해 NVIDIA GPU를 autodetect할 수 있다.

Slurm 지원은 다음으로 다뤄야 한다.

```text
runtime: host-systemd
telemetry: nvml
scheduler: slurm
```

collector는 user job 밖에서 compute node 위에 실행된다. 실제 GPU 사용은
NVML로 읽고, allocation context는 Slurm에서 읽는다.

### Slurm 감지 신호

```text
scontrol exists
sinfo exists
slurmd process or service exists
/etc/slurm/slurm.conf or $SLURM_CONF exists
scontrol show node <hostname> reports Gres or CfgTRES with gpu
```

### Slurm Allocation Context

초기 adapter source:

```text
scontrol show node <node>
squeue -h -w <node>
scontrol show job -d <jobid>
sacct, when available
/proc/<pid>/cgroup for job_<id> or step_<id>
```

MVP가 지원해야 할 것:

- 이 node에서 실행 중인 job.
- 각 job의 user.
- 각 job이 요청한 GPU 수.
- 가능하면 할당된 GPU device ID 또는 UUID.
- cgroup을 통한 GPU PID -> Slurm job ID 매핑.

Slurm이 exact GPU ID를 노출하지 않는 경우 첫 버전에서는 per-GPU allocation을
`allocated-unknown-gpu`로 표시해도 된다.

## Data Model V2

현재 schema는 hardware sample과 process sample을 담는다. 여전히 유용하지만,
scheduler allocation은 first-class storage가 필요하다.

제안 table:

### `node`

```text
node_id
hostname
first_seen
last_seen
runtime_mode       # host-systemd / k8s-daemonset / local-container
scheduler_kind     # none / k8s / slurm
driver_version
collector_version
```

### `gpu_sample`

```text
ts
node_id
gpu_uuid
gpu_index
parent_uuid          # nullable, set for MIG instances or virtual slices
mig_profile          # nullable, e.g. 1g.5gb
share_id             # nullable, for MIG/vGPU/time-slicing/MPS-style slices
bus_id
util_pct
mem_used_mb
mem_total_mb
```

### `gpu_process_sample`

```text
ts
node_id
gpu_uuid
pid
process_name
mem_used_mb
loginuid_user
owner_key          # nullable, references observed owner if resolved
```

### `allocation_sample`

```text
ts
node_id
scheduler_kind     # k8s / slurm
gpu_uuid           # nullable if exact GPU unknown
parent_uuid        # nullable, physical GPU for MIG/vGPU/shared allocations
owner_kind         # k8s_pod / slurm_job
owner_key          # stable ID: namespace/name or job ID
owner_name
namespace
user_name
account
requested_gpus
share_fraction     # nullable, for fractional/shared GPU allocation
allocation_state   # allocated / released / unknown
raw_ref
```

### `owner_sample`

정규화된 report에 유용한 optional table:

```text
ts
owner_kind
owner_key
owner_name
namespace
user_name
account
labels_json
```

### Migration

기존 DB는 legacy mode로 읽을 수 있다.

```text
scheduler_kind = none
allocation state = unknown
```

report는 기존 DB에서도 계속 동작해야 한다.

### Retention과 Rollup

Raw process sample은 빠르게 커질 수 있다. 바쁜 node는 tick마다 많은 row를
만들 수 있다.

```text
1 Hz * 10 GPUs * 50 GPU processes = 500 process rows/sec
```

SQLite는 유용한 short-term window를 감당할 수 있지만, 긴 retention에는 명시적
정책이 필요하다. 기본 저장소는 운영 모델을 단순하게 유지해야 한다.

```text
raw samples:       7-14 days by default
1-minute rollups:  90 days by default
5-minute rollups:  optional long-term retention
```

제안 rollup table:

```text
gpu_rollup_1m
owner_rollup_1m
allocation_rollup_1m
```

Rollup은 평균 utilization만 보존하면 안 되고 combined class를 보존해야 한다.
그렇지 않으면 `allocated-unused` 같은 핵심 신호가 downsampling 중 사라진다.

## Classification Model

기존 hardware classification은 유지한다.

```text
util >= 10                  -> active
util <  10 and mem > 100    -> idle-held
util <  10 and mem <= 100   -> truly-idle
```

scheduler allocation을 추가한다.

```text
allocation known and present -> allocated
allocation absent            -> unallocated
allocation unavailable       -> unknown
```

Combined class:

| Allocation | Hardware | Combined |
|---|---|---|
| allocated | active | allocated-active |
| allocated | idle-held | allocated-idle-held |
| allocated | truly-idle | allocated-unused |
| unallocated | active | unallocated-active |
| unallocated | idle-held | unallocated-idle-held |
| unallocated | truly-idle | truly-idle |
| unknown | active | active |
| unknown | idle-held | idle-held |
| unknown | truly-idle | truly-idle |

이 모델은 기존 report 의미를 유지하면서 k8s/Slurm 가치를 추가한다.

## Storage와 Reporting 전략

### Single Node

기본:

```text
/var/lib/gpu-usage-audit/gua.db
```

user-mode/foreground fallback:

```text
~/.local/share/gpu-usage-audit/gua.db
```

### Kubernetes

MVP:

- node마다 hostPath 기반 SQLite DB 하나.
- `gua report`가 collector pod를 발견한다.
- `gua report`가 각 collector pod 안에서
  `gua daemon export --format jsonl`을 실행하고 local에서 집계한다.

이 방식은 central database나 service를 피할 수 있지만 한계가 있다.

- `pods/exec` RBAC는 종종 제한된다.
- 많은 node를 sequential exec하면 느리다.
- 큰 export에는 streaming, compression, time-window filtering이 필요하다.

report 구현은 병렬 fan-out을 해야 하고 필요한 time window만 요청해야 한다.
또한 alternative export path를 지원해야 한다.

나중:

- 각 collector pod의 read-only HTTP export endpoint.
- `kubectl port-forward` 기반 report collection.
- cluster-internal aggregator Job.
- optional central PVC.
- optional Prometheus/exporter mode.
- optional object storage export.

### Slurm

MVP:

- compute node마다 SQLite DB 하나.
- 먼저 local node report를 지원한다.

나중:

- Slurm controller-side aggregator.
- `gua report --partition` 또는 `--nodes`.

## Packaging과 Installation

### 기본 CLI 설치

권장:

```sh
uv tool install gpu-usage-audit
```

또는:

```sh
pipx install gpu-usage-audit
```

첫 사용 마찰을 줄이기 위해 `nvidia-ml-py`를 optional extra가 아니라 기본
dependency로 둘지 검토한다. 작고, GPU audit 도구가 NVML binding 누락으로 첫
실행에서 실패하는 것은 좋지 않다.

### OCI Image

k8s runtime에는 필요하다.

```text
ghcr.io/AI-Ocean/gpu-usage-audit:<version>
ghcr.io/AI-Ocean/gpu-usage-audit:latest
```

사용자가 Docker를 직접 실행할 필요는 없다. image는 k8s runtime adapter가
사용하는 내부 구현 디테일이다.

### Kubernetes 설치

초기 구현은 Python package 안에 manifest template을 내장할 수 있다.

나중:

- GitHub Releases에 standalone YAML 게시.
- Helm chart 게시.

### One-Line Installer

나중에 가능한 UX:

```sh
curl -Ls https://github.com/AI-Ocean/gpu-usage-audit/releases/latest/download/install.sh | sh
```

이는 CLI만 설치해야 한다. systemd service나 k8s DaemonSet을 조용히 설치하면
안 된다.

## Security와 Permission

### Host Mode

필요:

- NVML 접근.
- `/proc/<pid>/loginuid`와 cgroup metadata read 권한.
- DB directory write 권한.
- systemd install에는 root 필요.

테스트용으로 non-root foreground mode를 지원해야 한다.

### Kubernetes Mode

필요:

- namespace, service account, configmap, daemonset, RBAC를 만들 수 있는 권한.
- target node의 모든 GPU에 접근할 수 있는 runtime 권한.
- pod와 node metadata read 권한.
- process attribution을 위한 hostPID와 read-only `/proc` 접근 가능성.
- SQLite DB를 위한 hostPath write 권한.
- exec 기반 export를 쓸 경우 `gua report`용 optional `pods/exec`.

install plan은 resource를 적용하기 전에 이 권한들을 출력해야 한다.

collector의 최소 RBAC는 다음에서 시작한다.

```text
get/list/watch pods
get/list/watch nodes
```

`pods/exec`는 report-side에만 필요하며 collector 자체에는 필요하지 않아야
한다.

### Slurm Mode

필요:

- Host NVML 접근.
- Slurm command/config/accounting read 접근.
- process cgroup read 접근.
- systemd install에는 보통 admin 권한 필요.

Slurm job user가 node-wide collector를 설치한다고 기대하면 안 된다.

## 구현 마일스톤

### M0: 집중 ADR

넓은 구현 전에 위험도가 높은 세부사항에 대해 짧은 architecture decision
record를 작성한다.

- GPU Operator staged NVML loading과 host-mode re-exec.
- MIG, vGPU, MPS, time-slicing 표현.
- cgroup v1/v2 parser와 owner attribution.
- k8s report export path: `pods/exec` vs HTTP endpoint vs aggregator.

### M1: Doctor와 RuntimePlan

아직 collection 동작은 바꾸지 않는다.

Deliver:

- `gua doctor`
- host NVML/device check
- k8s check
- Slurm check
- structured JSON output
- recommended plan

환경 가정을 설치 없이 검증하므로 가장 leverage가 높은 milestone이다.

### M2: Schema V2와 Combined Report Model

Deliver:

- migration-safe DB schema
- allocation table
- combined classes
- fake scheduler tests
- old DB compatibility
- retention and rollup policy

이것이 차별화 기능이다. 모든 runtime adapter가 같은 model을 target할 수
있도록 일찍 들어가야 한다.

### M3: CLI Surface와 State

Deliver:

- `gua start --dry-run`
- `gua status`
- local state file
- 기존 command compatibility alias

아직 k8s install은 하지 않는다.

### M4: Kubernetes Runtime Adapter

Deliver:

- official OCI image
- embedded DaemonSet manifest
- `gua start --mode k8s`
- `gua stop --mode k8s`
- parallel, windowed export 기반 collector pod report

관측한 GPU Operator 환경을 해결한다.

### M5: Kubernetes Scheduler Adapter

Deliver:

- pod/process attribution
- 가능한 경우 PodResources API integration
- namespace/pod/user별 report
- GPU request 없는 `NVIDIA_VISIBLE_DEVICES=all` pod 탐지
- unrequested GPU access anomaly headline

### M6: Host Runtime Adapter

Deliver:

- systemd unit install
- foreground mode
- host preflight
- GPU Operator staged NVML re-exec 또는 명확한 diagnostic

### M7: Slurm Scheduler Adapter

Deliver:

- Slurm detection
- job allocation snapshot
- cgroup 기반 process-to-job mapping
- best-effort exact GPU-to-job mapping
- job/user/account별 report

### M8: Documentation과 Release Polish

Deliver:

- quickstart
- architecture docs
- troubleshooting matrix
- wheel + OCI image release workflow
- optional Helm chart

## 현재 서버 해석

관측한 `gpusystem` 서버는 다음에 해당한다.

```text
runtime: k8s-daemonset
telemetry: nvml
scheduler: k8s
```

이유:

- Host에는 `/dev/nvidiactl`만 보인다.
- Host NVML은 device를 보지 못한다.
- Kubernetes workload container 안에서는 `/dev/nvidia0..9`가 보인다.
- 일부 pod는 `runtimeClassName=nvidia`와 `nvidia.com/gpu` request를 쓴다.
- 일부 pod는 GPU request 없이 `NVIDIA_VISIBLE_DEVICES=all`을 노출한다.

이 환경이 바로 runtime placement와 scheduler context를 분리해야 하는 이유다.

## Open Questions

제안 결정:

1. `nvidia-ml-py`는 기본 dependency가 되어야 한다.
2. k8s DaemonSet은 `hostPID: true`를 기본값으로 하고 `--no-host-pid` opt-out을
   제공한다.
3. k8s install은 기본적으로 GPU-capable node만 target해야 한다.
4. collector RBAC는 read-only로 시작한다: pods와 nodes. `pods/exec`는
   exec 기반 report transport에만 필요하다.
5. `gua report`는 local state가 node-scoped일 때 current node를 기본값으로
   하고, cluster report에는 `--all-nodes`를 제공한다.
6. Slurm MVP는 detection, node-level job allocation, cgroup PID-to-job mapping을
   포함해야 한다. Exact GPU-to-job mapping은 best effort다.
7. MIG field는 schema v2에 미리 들어가야 한다. report는 초기에는 MIG를 일반
   GPU-like device처럼 다뤄도 된다.
8. `gua`를 primary command로 둔다. `gpu-usage-audit`는 compatibility alias로
   유지한다.

아직 열려 있는 질문:

1. 첫 k8s report transport는 `pods/exec`, HTTP export, 또는 둘 다 중 무엇인가?
2. 바쁜 node에서 acceptable한 기본 raw retention window는 얼마인가?
3. rollup은 collector process에서 계산할 것인가, report/export 시점에 계산할
   것인가?
4. HAMi/vGPU/time-slicing의 fractional sharing을 scheduler 간 어떻게 정규화할
   것인가?

## 참고 자료

- NVIDIA DCGM Exporter deployment patterns:
  https://docs.nvidia.com/datacenter/dcgm/latest/gpu-telemetry/dcgm-exporter.html
- NVIDIA Container Toolkit GPU environment variables:
  https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/1.18.1/docker-specialized.html
- NVIDIA GPU Operator overview:
  https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/index.html
- NVIDIA GPU Operator CDI and GPU Management Containers:
  https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/cdi.html
- Kubernetes Device Plugins:
  https://kubernetes.io/docs/concepts/extend-kubernetes/compute-storage-net/device-plugins/
- Kubernetes kubelet files and Pod Resources API path:
  https://kubernetes.io/docs/reference/node/kubelet-files/
- Slurm GRES GPU scheduling:
  https://slurm.schedmd.com/gres.html
- Slurm `gres.conf`:
  https://slurm.schedmd.com/gres.conf.html
- Slurm cgroups:
  https://slurm.schedmd.com/cgroups.html
- Jeon et al., "Analysis of Large-Scale Multi-Tenant GPU Clusters for DNN
  Training Workloads", USENIX ATC 2019:
  https://www.usenix.org/conference/atc19/presentation/jeon
- Hu et al., "Lucid: A Non-intrusive, Scalable and Interpretable Scheduler for
  Deep Learning Training Jobs", ASPLOS 2023:
  https://doi.org/10.1145/3575693.3575705

# gpu-usage-audit auto-runtime design

Status: draft  
Date: 2026-05-12

## Summary

`gpu-usage-audit` should become a tool that a user can start without knowing
whether the machine is bare metal, Kubernetes, a container runtime host, or a
Slurm compute node.

Target UX:

```sh
gua doctor
gua start

# days later
gua status
gua report --since 3d
gua stop
```

The product should auto-detect the right collector runtime, but it must not hide
the decision. The user should not need to know the deployment model up front,
but `gua` should clearly report what it chose and why.

Example:

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

This is the main product shift: `daemon` remains a low-level collector, while
`gua start` becomes the launcher/orchestrator.

## Product Goals

1. **No environment knowledge required for first use**
   - The user can run `gua doctor` or `gua start` without knowing whether the
     node is bare metal, k8s, Docker, or Slurm.

2. **Transparent, not magical**
   - Auto mode must print the selected plan, reasons, required privileges,
     storage location, and cleanup command.
   - Advanced users can override with `--mode host`, `--mode k8s`,
     `--mode slurm`, or `--mode container`.

3. **Retrospective audit first**
   - The core value remains "what happened over the last N hours/days?"
   - Live dashboards, quotas, scheduling decisions, and remediation are not the
     first product surface.

4. **Measure both actual GPU use and scheduler allocation**
   - NVML answers: "is a GPU doing work or holding memory?"
   - k8s/Slurm answer: "was this GPU allocated to a workload?"
   - The report should combine both.

5. **Low operational footprint**
   - SQLite remains the default local storage.
   - No database service, web server, Prometheus, or Grafana required for the
     default path.

6. **Good failure modes**
   - If `gua` cannot run, it should say which layer failed: driver, NVML,
     device visibility, container runtime, kubectl auth, Slurm config, or
     permissions.

## Non-Goals

- Replacing Slurm, Kubernetes, DCGM, Prometheus, Grafana, Open OnDemand, or
  cluster dashboards.
- Enforcing quotas or killing jobs.
- Scheduling workloads.
- Requiring a central server in the minimum viable product.
- Making every install silent. Cluster or system changes should be explicit.

## Supported Environment Classes

### 1. Bare Metal Host

Typical shape:

```text
/dev/nvidia0..N visible on host
host NVML init succeeds
host NVML device count > 0
no scheduler detected, or scheduler context disabled
```

Runtime:

```text
runtime: host-systemd or host-foreground
telemetry: nvml
scheduler: none
```

This is closest to the current project.

### 2. Kubernetes / GPU Operator

Typical shape:

```text
host may only show /dev/nvidiactl
host NVML device count may be 0
GPU devices are injected into pods
runtimeClassName=nvidia may exist
NVIDIA_VISIBLE_DEVICES controls device exposure
```

Runtime:

```text
runtime: k8s-daemonset
telemetry: nvml
scheduler: k8s
```

The collector must run inside Kubernetes because the GPUs may only be visible in
container namespaces.

The user should not need to build or run Docker manually. The product can still
use an official OCI image internally.

### 3. Slurm Compute Node

Typical shape:

```text
host /dev/nvidia0..N visible
Slurm manages GPUs as GRES
jobs request GPUs with --gres=gpu:N or --gpus=N
Slurm sets CUDA_VISIBLE_DEVICES inside job steps
cgroups may restrict visible device files
```

Runtime:

```text
runtime: host-systemd or host-foreground
telemetry: nvml
scheduler: slurm
```

Slurm support is not mainly about making NVML work. It is about combining NVML
use with Slurm allocation state.

### 4. Local Container Runtime

Typical shape:

```text
host command cannot or should not run directly
docker/podman can run NVIDIA containers
docker run --gpus all ... sees GPUs
```

Runtime:

```text
runtime: local-container
telemetry: nvml
scheduler: none
```

This is useful as a fallback, but should not be the primary UX.

## Core Architecture

Do not spread environment branches throughout the collector and report code.
Separate the product into three axes.

```text
1. Collector Runtime
   Where does the collector process run?

2. Telemetry Source
   How does it read actual GPU state?

3. Scheduler Context
   Who has the GPU reserved or allocated?
```

Concrete combinations:

| Environment | Runtime | Telemetry | Scheduler |
|---|---|---|---|
| Bare metal | host-systemd | nvml | none |
| Kubernetes / GPU Operator | k8s-daemonset | nvml | k8s |
| Slurm | host-systemd | nvml | slurm |
| Docker-only | local-container | nvml | none |
| Demo/test | foreground | fake | none/fake |

The important rule:

```text
Kubernetes and Slurm are not telemetry sources.
NVML is still the telemetry source.
Kubernetes and Slurm provide runtime placement and allocation context.
```

## CLI Design

### Primary Commands

```text
gua doctor
gua start
gua status
gua report
gua stop
gua uninstall
```

### Low-Level Commands

These can remain available, but should not be the primary first-run UX.

```text
gua daemon run
gua daemon export
gua db inspect
```

The current `gpu-usage-audit daemon` and `gpu-usage-audit report` can remain as
compatibility aliases during migration.

### `gua doctor`

Read-only environment diagnosis.

Default output is human-readable. `--json` is required for automation.

Example:

```sh
gua doctor
gua doctor --json
gua doctor --mode k8s
```

Doctor checks:

- OS, kernel, Python, uv/pipx availability
- `/dev/nvidia*`
- host NVML load/init/device count
- GPU Operator staged NVML under `/run/nvidia/driver`
- `nvidia-smi` presence if available
- `kubectl` availability and auth
- k8s runtime classes
- k8s GPU pods/DaemonSets
- ability to create required k8s resources
- Slurm commands and node GRES
- Docker/Podman NVIDIA runtime fallback

Doctor produces a `RuntimePlan`.

### `gua start`

Default mode is `auto`.

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

Behavior:

1. Run doctor.
2. Select a runtime plan.
3. Print the plan.
4. If the action mutates system or cluster state, ask for confirmation when
   running in a TTY.
5. Persist install state locally.

Example:

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

Shows the installed/running collector state.

```text
mode: k8s-daemonset
collectors:
  gpusystem: running, last sample 12s ago, GPUs visible=10
  ds02: running, last sample 10s ago, GPUs visible=4
storage:
  per-node SQLite under /var/lib/gpu-usage-audit/gua.db
```

### `gua report`

Default should use the saved install state.

```sh
gua report --since 24h
gua report --since 3d --node gpusystem
gua report --since 3d --all-nodes
gua report --db /var/lib/gpu-usage-audit/gua.db --since 3d
```

For k8s, `gua report` should not require users to know where the DB is. It can
query collector pods through `kubectl exec` and stream an export format back to
the local CLI.

### `gua stop` and `gua uninstall`

`stop` should stop the collector but preserve data by default.

`uninstall` can remove installed resources and optionally data.

```sh
gua stop
gua uninstall
gua uninstall --delete-data
```

## RuntimePlan Interface

The detector should produce a structured plan, not directly perform actions.

Conceptual model:

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

Runtime adapters consume a plan:

```text
HostRuntimeAdapter
K8sRuntimeAdapter
ContainerRuntimeAdapter
```

Scheduler adapters enrich snapshots:

```text
NoSchedulerAdapter
K8sSchedulerAdapter
SlurmSchedulerAdapter
```

Telemetry adapters produce hardware facts:

```text
NVMLTelemetry
FakeTelemetry
```

## Detection Order

Auto mode should prefer the least surprising runtime that can see all GPUs.

Proposed order:

1. Host NVML
   - If host NVML sees GPUs, host runtime is viable.
   - If Slurm is detected, scheduler context becomes `slurm`.
   - Otherwise scheduler context is `none`.

2. Kubernetes
   - If host NVML cannot see GPUs, but k8s is available and NVIDIA runtime can
     expose GPUs in a pod, use `k8s-daemonset`.
   - Do not rely only on `node.status.capacity["nvidia.com/gpu"]`; some
     clusters expose GPUs to pods even when accounting is unusual or custom.

3. Local container runtime
   - If Docker/Podman can run an NVIDIA container with all GPUs, use
     `local-container`.

4. Unsupported
   - Explain the nearest viable path.

Important: detection should never install packages or mutate the cluster.

## Kubernetes Runtime Design

### Installation Shape

Minimum viable install:

```text
Namespace: gpu-usage-audit
DaemonSet: gpu-usage-audit
ServiceAccount: gpu-usage-audit
ConfigMap: collector config
hostPath DB: /var/lib/gpu-usage-audit/gua.db
```

DaemonSet requirements:

```yaml
runtimeClassName: nvidia
hostPID: true
env:
  - name: NVIDIA_VISIBLE_DEVICES
    value: all
  - name: NVIDIA_DRIVER_CAPABILITIES
    value: compute,utility
```

Likely mounts:

```text
/var/lib/gpu-usage-audit         read-write DB hostPath
/proc                            read-only host process metadata, if needed
/var/lib/kubelet/pod-resources   read-only pod resources socket, if available
```

`hostPID: true` is important for node-wide process attribution. NVML can report
PIDs for GPU processes, but without host PID visibility the collector may not be
able to map those PIDs back to `/proc/<pid>/cgroup`.

### Kubernetes Allocation Context

The k8s adapter should combine three data sources:

1. Kubernetes API
   - Pods, namespaces, node names, owner references, resource requests/limits.

2. Kubelet PodResources API
   - Best source for which pod/container received which GPU device IDs.

3. Host `/proc/<pid>/cgroup`
   - Best source for mapping an observed GPU process PID to a pod/container.

This distinction matters because the current observed cluster has pods with:

```text
NVIDIA_VISIBLE_DEVICES=all
no nvidia.com/gpu request
all GPUs visible inside the container
```

Those pods can use GPUs even though scheduler accounting may not represent the
use cleanly.

### Kubernetes Report Semantics

The report should show both scheduler allocation and actual GPU state:

```text
allocated-active
allocated-idle-held
allocated-unused
unallocated-active
unallocated-idle-held
truly-idle
```

Where:

```text
allocated-unused = scheduler allocated GPU, but no meaningful NVML process/mem
unallocated-active = NVML shows use, but scheduler allocation is absent/unknown
unallocated-idle-held = memory held without scheduler allocation
truly-idle = no allocation and no meaningful NVML use
```

## Slurm Runtime Design

Slurm generally manages GPUs through GRES.

Important Slurm facts:

- GPUs are configured as GRES, usually `Name=gpu`.
- Jobs request GPUs with `--gres=gpu:N`, `--gpus=N`, or related flags.
- Slurm sets `CUDA_VISIBLE_DEVICES` for job steps.
- Slurm can use cgroups to restrict visible device files.
- Slurm can autodetect NVIDIA GPUs with NVML in `gres.conf`.

Slurm support should be treated as:

```text
runtime: host-systemd
telemetry: nvml
scheduler: slurm
```

The collector runs on compute nodes outside user jobs. It reads NVML for actual
GPU use and Slurm for allocation context.

### Slurm Detection Signals

```text
scontrol exists
sinfo exists
slurmd process or service exists
/etc/slurm/slurm.conf or $SLURM_CONF exists
scontrol show node <hostname> reports Gres or CfgTRES with gpu
```

### Slurm Allocation Context

Initial adapter sources:

```text
scontrol show node <node>
squeue -h -w <node>
scontrol show job -d <jobid>
sacct, when available
/proc/<pid>/cgroup for job_<id> or step_<id>
```

MVP should support:

- Which jobs are running on this node.
- Which users own those jobs.
- How many GPUs each job requested.
- If available, which GPU device IDs or UUIDs are allocated.
- Mapping GPU PIDs back to Slurm job IDs via cgroup.

It is acceptable for the first version to mark per-GPU allocation as
`allocated-unknown-gpu` if Slurm does not expose exact GPU IDs in the available
commands.

## Data Model V2

The current schema captures hardware samples and process samples. That is still
useful, but scheduler allocation needs first-class storage.

Proposed tables:

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
owner_kind         # k8s_pod / slurm_job
owner_key          # stable ID: namespace/name or job ID
owner_name
namespace
user_name
account
requested_gpus
allocation_state   # allocated / released / unknown
raw_ref
```

### `owner_sample`

Optional but useful for normalized reporting:

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

The existing DB can be read as legacy mode:

```text
scheduler_kind = none
allocation state = unknown
```

Reports should continue to work on old DBs.

## Classification Model

Keep the existing hardware classification:

```text
util >= 10                  -> active
util <  10 and mem > 100    -> idle-held
util <  10 and mem <= 100   -> truly-idle
```

Add scheduler allocation:

```text
allocation known and present -> allocated
allocation absent            -> unallocated
allocation unavailable       -> unknown
```

Combined classes:

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

This lets the product keep the original report semantics while adding k8s/Slurm
value.

## Storage and Reporting Strategy

### Single Node

Default:

```text
/var/lib/gpu-usage-audit/gua.db
```

User-mode/foreground fallback:

```text
~/.local/share/gpu-usage-audit/gua.db
```

### Kubernetes

MVP:

- One SQLite DB per node via hostPath.
- `gua report` discovers collector pods.
- `gua report` runs `gua daemon export --format jsonl` inside each collector
  pod and aggregates locally.

This avoids a central database or service.

Later:

- Optional central PVC.
- Optional Prometheus/exporter mode.
- Optional object storage export.

### Slurm

MVP:

- One SQLite DB per compute node.
- Local node reports first.

Later:

- Slurm controller-side aggregator.
- `gua report --partition` or `--nodes`.

## Packaging and Installation

### Primary CLI Install

Recommended:

```sh
uv tool install gpu-usage-audit
```

or:

```sh
pipx install gpu-usage-audit
```

To reduce first-run friction, consider making `nvidia-ml-py` a default
dependency instead of an optional extra. It is small, and missing NVML bindings
should not be the reason a GPU audit tool fails on first use.

### OCI Image

Needed for k8s runtime.

```text
ghcr.io/AI-Ocean/gpu-usage-audit:<version>
ghcr.io/AI-Ocean/gpu-usage-audit:latest
```

The user does not need to run Docker manually. The image is an implementation
detail used by the k8s runtime adapter.

### Kubernetes Install

Initial implementation can embed a manifest template in the Python package.

Later:

- Publish standalone YAML in GitHub Releases.
- Publish Helm chart.

### One-Line Installer

Optional later UX:

```sh
curl -Ls https://github.com/AI-Ocean/gpu-usage-audit/releases/latest/download/install.sh | sh
```

This should install the CLI only. It should not silently install a systemd
service or k8s DaemonSet.

## Security and Permissions

### Host Mode

Needs:

- NVML access.
- Read access to `/proc/<pid>/loginuid` and cgroup metadata.
- Write access to DB directory.
- systemd install requires root.

Non-root foreground mode should be supported for testing.

### Kubernetes Mode

Needs:

- Ability to create namespace, service account, configmap, daemonset, and RBAC.
- Runtime access to all GPUs on the target node.
- Read access to pod metadata.
- Potential hostPID and read-only `/proc` access for process attribution.
- hostPath write access for SQLite DB.

The install plan must print these privileges before applying resources.

### Slurm Mode

Needs:

- Host NVML access.
- Read access to Slurm commands/config/accounting.
- Read access to process cgroups.
- systemd install usually requires admin privileges.

Slurm job users should not be expected to install node-wide collectors.

## Implementation Milestones

### M1: Doctor and RuntimePlan

No behavior changes to collection yet.

Deliver:

- `gua doctor`
- host NVML/device checks
- k8s checks
- Slurm checks
- structured JSON output
- recommended plan

This is the highest leverage milestone because it validates environment
assumptions without installing anything.

### M2: CLI Surface and State

Deliver:

- `gua start --dry-run`
- `gua status`
- local state file
- compatibility aliases for old commands

No k8s install yet.

### M3: Schema V2 and Combined Report Model

Deliver:

- migration-safe DB schema
- allocation table
- combined classes
- fake scheduler tests
- old DB compatibility

### M4: Kubernetes Runtime Adapter

Deliver:

- official OCI image
- embedded DaemonSet manifest
- `gua start --mode k8s`
- `gua stop --mode k8s`
- `gua report` from collector pods

This solves the observed GPU Operator environment.

### M5: Kubernetes Scheduler Adapter

Deliver:

- pod/process attribution
- PodResources API integration where available
- report by namespace/pod/user
- detection of `NVIDIA_VISIBLE_DEVICES=all` pods without GPU requests

### M6: Host Runtime Adapter

Deliver:

- systemd unit install
- foreground mode
- host preflight
- GPU Operator staged NVML re-exec or clear diagnostic

### M7: Slurm Scheduler Adapter

Deliver:

- Slurm detection
- job allocation snapshots
- process-to-job mapping through cgroups
- report by job/user/account

### M8: Documentation and Release Polish

Deliver:

- quickstart
- architecture docs
- troubleshooting matrix
- release workflow for wheel + OCI image
- optional Helm chart

## Current Server Interpretation

The observed `gpusystem` server fits:

```text
runtime: k8s-daemonset
telemetry: nvml
scheduler: k8s
```

Why:

- Host only shows `/dev/nvidiactl`.
- Host NVML cannot see devices.
- Kubernetes workload containers can see `/dev/nvidia0..9`.
- Some pods use `runtimeClassName=nvidia` and `nvidia.com/gpu` requests.
- Some pods expose `NVIDIA_VISIBLE_DEVICES=all` without GPU requests.

This environment is exactly why runtime placement and scheduler context must be
separate abstractions.

## Open Questions

1. Should `nvidia-ml-py` become a default dependency?
2. Should the k8s DaemonSet use `hostPID: true` by default?
3. Should k8s install default to all nodes or only detected GPU nodes?
4. What is the minimum RBAC for useful k8s attribution?
5. Should `gua report` default to one node or all nodes in k8s mode?
6. How much Slurm support is needed in the first release: detection only,
   allocation counts, or exact GPU-to-job mapping?
7. How should MIG be represented in the schema?
8. Should the project keep `gpu-usage-audit` as the command and add `gua`, or
   switch docs to `gua` as the primary command immediately?

## References

- NVIDIA DCGM Exporter deployment patterns:
  https://docs.nvidia.com/datacenter/dcgm/latest/gpu-telemetry/dcgm-exporter.html
- NVIDIA Container Toolkit GPU environment variables:
  https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/1.18.1/docker-specialized.html
- Slurm GRES GPU scheduling:
  https://slurm.schedmd.com/gres.html
- Slurm `gres.conf`:
  https://slurm.schedmd.com/gres.conf.html
- Slurm cgroups:
  https://slurm.schedmd.com/cgroups.html

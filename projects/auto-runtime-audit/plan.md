# Auto Runtime Audit Development Plan

Status: on hold
Scope: development plan for the auto-runtime architecture proposal

> 2026-05-14 scope reset: the 1.0 product is focused on diagnosing and
> collecting from **the currently installed bare-metal machine**, not
> auto-runtime or cluster-wide audit. The 1.0 plan of record is
> `projects/bare-metal-1.0/plan.ko.md`. This document remains as a deferred
> reference for a future expansion back into Kubernetes, Slurm, Docker/Podman,
> and scheduler allocation-aware reporting.

## Goal

Build `gpu-usage-audit` as a retrospective audit tool that joins actual GPU
telemetry with scheduler allocation context.

The product should answer:

- Who was allocated GPU capacity?
- Did they actually use it?
- Who used GPUs without scheduler allocation?
- Which GPUs were memory-held but compute-idle?

The implementation should be top-down. First define the user-facing report,
runtime plan, data model, and fake end-to-end flow. Then attach real host,
Kubernetes, and Slurm adapters.

## Architecture Shape

Expected module boundaries:

```text
gpu_usage_audit/
  cli/              # gua doctor/start/status/report/stop
  doctor/           # environment checks and RuntimePlan creation
  runtime/          # where the collector runs
  telemetry/        # actual GPU facts, usually NVML
  scheduler/        # allocation and ownership context
  attribution/      # PID -> pod/job/user mapping
  storage/          # SQLite schema, migration, export, rollup
  report/           # classification, aggregation, rendering
  packaging/        # systemd units, k8s manifests, OCI image
```

The core separation:

```text
Runtime placement: where does the collector process run?
Telemetry source: how do we observe actual GPU state?
Scheduler context: who was allocated GPU capacity?
Attribution: how do observed PIDs map back to owners?
Report model: how do telemetry and allocation combine?
```

Kubernetes and Slurm are scheduler context providers. They are not telemetry
sources. The default telemetry source remains NVML.

## Supported Areas

| Area | Runtime | Telemetry | Scheduler | Expected capability |
|---|---|---|---|---|
| Bare metal | host systemd or foreground | NVML | none | active / idle-held / truly-idle |
| Bare metal + Slurm | host systemd | NVML | Slurm | job, user, account audit |
| Kubernetes / GPU Operator | DaemonSet | NVML inside pod | Kubernetes | pod and namespace audit |
| Local Docker/Podman | local container | NVML inside container | none | fallback when host execution is unavailable |
| Demo/test | foreground | fake | fake or none | product semantics without GPU access |

## Delivery Principles

- Every PR must merge independently and leave the project in a working state.
- Existing commands may remain as compatibility aliases while the new `gua`
  command surface is introduced.
- Detection must be read-only. It must not install packages or mutate system or
  cluster state.
- `start` must show a concrete plan before changing system or cluster state.
- Runtime placement and scheduler context must be detected independently.
- Fake telemetry and fake scheduler flows should prove the report semantics
  before real cluster integrations are added.

## PR Plan

### PR 1: Proposal And Roadmap

Current PR.

Deliver:

- Auto-runtime architecture proposal.
- Korean translation.
- This PR-based development plan.

Working state:

- Documentation-only change.
- No runtime behavior changes.

Before merge:

- Clarify that runtime placement and scheduler context are independent.
- Use Kubernetes UID as the stable owner identity, with namespace/name as
  display fields.
- Treat `NVIDIA_VISIBLE_DEVICES=all` without GPU request as an anomaly, with
  explicit exceptions for GPU management agents such as this collector, DCGM,
  and NVIDIA device/plugin components.
- Remove unintended Markdown trailing whitespace unless a hard line break is
  deliberately required.

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

- Existing `gpu-usage-audit daemon/report/demo` compatibility path.
- Clear placeholder behavior for unsupported or not-yet-installed modes.
- CLI smoke tests.

Working state:

- Users can run the new command surface.
- Existing documented commands still work.
- `start/status/stop` do not silently mutate anything.

### PR 3: RuntimePlan And Doctor V1

Deliver:

- `RuntimePlan` model.
- `gua doctor` human-readable output.
- `gua doctor --json`.
- `gua start --dry-run` rendering the recommended plan.
- Read-only checks for:
  - OS/kernel/Python.
  - `/dev/nvidia*`.
  - NVML load/init/device count.
  - `kubectl` presence and auth.
  - Kubernetes runtime signals.
  - Slurm command/config signals.
  - Docker/Podman NVIDIA fallback signals.

Working state:

- Users can understand which runtime path is recommended on the current
  machine without installing anything.

### PR 4: Data Model V2 And Migration

Deliver:

- Schema versioning and migration.
- `node`.
- expanded `gpu_sample`.
- `gpu_process_sample`.
- `allocation_sample`.
- `owner_sample`.
- Legacy DB read compatibility.

Working state:

- Existing host daemon/report behavior continues on the new schema.
- Scheduler allocation may be absent, but reports still produce the legacy
  active / idle-held / truly-idle view.

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
- Demo data covering allocated, unallocated, and unknown allocation states.
- Report section for combined classes.
- Tests for classification and report aggregation.

Working state:

- The final product meaning is testable without real GPUs, Kubernetes, or Slurm.

### PR 6: Install State And Local Host Runtime

Deliver:

- Local install state file.
- Default DB path.
- Host foreground runtime adapter.
- `gua start --mode host --foreground`.
- `gua status`.
- `gua report --since ...` using state when `--db` is omitted.
- `gua stop` for foreground/state-aware flows where applicable.

Working state:

- Single-host users can use the new `gua` workflow without manually passing
  `--db` through every command.

### PR 7: Systemd Host Runtime

Deliver:

- systemd unit template.
- `gua start --mode host`.
- `gua stop`.
- `gua uninstall`.
- `gua uninstall --delete-data`.
- `--dry-run` and `--yes`.
- root/permission diagnostics.
- Data preservation by default.

Working state:

- Bare-metal host collection can be installed, stopped, and removed through the
  new UX.

### PR 8: Kubernetes Manifest Dry Run

Deliver:

- Embedded Kubernetes manifest templates.
- Namespace, ServiceAccount, RBAC, ConfigMap, and DaemonSet rendering.
- GPU-capable node targeting logic.
- `hostPID: true` default.
- `--no-host-pid` opt-out.
- Security and RBAC explanation in the plan output.

Working state:

- Users can inspect exactly what would be installed in a Kubernetes cluster
  without applying it.

### PR 9: Kubernetes Runtime Adapter

Deliver:

- Official OCI image path.
- `gua start --mode k8s`.
- `gua status --mode k8s`.
- `gua stop --mode k8s`.
- `kubectl apply/delete` integration.
- Collector pod discovery.
- Per-node hostPath SQLite DB.
- Node-level last-sample status.

Working state:

- Kubernetes GPU nodes can run collectors through a DaemonSet.
- Scheduler attribution may still be limited.

### PR 10: Kubernetes Report Export

Deliver:

- `gua report --since ... --node NODE`.
- `gua report --since ... --all-nodes`.
- Collector pod fan-out.
- Windowed export.
- JSONL export format.
- Parallel collection.
- `pods/exec` RBAC diagnostics.

Working state:

- Users can generate a cluster-level report from per-node collector databases.

### PR 11: Kubernetes Scheduler Attribution

Deliver:

- Kubernetes API owner snapshot.
- Pod UID based owner identity.
- PodResources API integration.
- Pod resource request/limit parsing.
- `/proc/<pid>/cgroup` PID-to-pod mapping.
- cgroup v1/v2 parser coverage.
- `NVIDIA_VISIBLE_DEVICES=all` anomaly detection.
- GPU management pod exceptions.

Working state:

- Kubernetes reports can show allocated-active, allocated-unused,
  unallocated-active, and unallocated-idle-held by pod/namespace.

### PR 12: Slurm Doctor And Scheduler Adapter

Deliver:

- Slurm detection in doctor.
- `scontrol`, `squeue`, and optional `sacct` integration.
- Node-level running job allocation snapshot.
- job/user/account owner model.
- Requested GPU count.
- cgroup PID-to-job mapping.
- Best-effort exact GPU-to-job mapping.

Working state:

- Slurm compute nodes can report GPU usage by job, user, and account.

### PR 13: Rollup And Retention

Deliver:

- Raw sample retention policy.
- 1-minute rollup tables.
- Combined class rollup.
- Cleanup command.
- Report support for raw plus rollup windows.

Working state:

- Long-running collectors keep DB size under control without losing the core
  audit classes.

### PR 14: Packaging And Release Polish

Deliver:

- README quickstart for host, Kubernetes, Slurm, and demo paths.
- Troubleshooting matrix.
- Wheel release verification.
- OCI image release workflow.
- Optional Helm chart, if the manifest path has stabilized.

Working state:

- A new user can install, start, inspect, report, and uninstall using the docs.

## Recommended Merge Order

The critical foundation is PR 2 through PR 5:

```text
CLI surface -> RuntimePlan/doctor -> schema V2 -> combined report semantics
```

After that, host, Kubernetes, and Slurm become adapter work against stable
contracts.

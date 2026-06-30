# Changelog

## 1.6.1 - 2026-06-29

- `gua top` polish: show the GPU index, draw a `▁` baseline so 0% reads as a
  floor (not a gap), and limit the process table to compute processes —
  graphics noise like Xorg/gnome-shell no longer clutters the view.
- Docs recommend `uv tool install gpu-usage-audit@latest` for upgrades, since
  `uv tool upgrade` can miss a just-published release due to index caching.

## 1.6.0 - 2026-06-29

- New `gua top`: a live local GPU view (1s utilization sparkline + per-GPU
  process table) right in the terminal, no board or web UI required. Runs
  against a real GPU or with `--fake`.
- `gua daemon --cloud` now also streams 1s utilization to GUA Board over a
  WebSocket, so the board's graphs scroll live; periodic snapshots still go
  over HTTP. The board buffers util in memory only and stores no per-second
  history.
- Adds a `websockets` runtime dependency for the cloud live stream. It is
  imported lazily, so the fully local CLI (`top`, `daemon`, `report`, `demo`)
  still works if `websockets` is absent.

## 1.5.0 - 2026-06-29

- Maintenance release — no user-facing behavior change. Internal CLI
  cleanup: `build_parser` now reuses the shared `_add_daemon_args` /
  `_add_report_args` / `_add_demo_args` helpers, and `main` / `gua_main`
  share a single `_dispatch`. Removed the superseded `bare-metal-1.0`
  planning docs.

## 1.4.0 - 2026-06-19

- Cloud sync now classifies each GPU from NVML compute and graphics process
  state and emits per-GPU `usageState` (`active`, `idle_held`, or `idle`) for
  GUA Board availability decisions.
- Process payloads now tag `compute` versus `graphics` ownership and preserve
  unknown NVML `usedGpuMemory` as `memoryUsedMb: null`, so display-only
  graphics processes do not make a GPU look occupied by compute.
- Local SQLite history upgrades in place with nullable process memory, process
  type, and GPU usage state columns; fake sync snapshots cover active,
  idle-held, and graphics-only idle GPUs.

## 1.3.0 - 2026-06-18

- Cloud sync now emits a real `collectionStatus` instead of always reporting
  `ok`. When core GPU metrics are collected but one or more cards' per-process
  list is unavailable (permissions or transient NVML errors), `gua sync-once`
  and `gua daemon --cloud` push `partial` with a `process_list_unavailable`
  error while still sending the GPU data. When NVML initialization fails
  entirely, `gua sync-once` now pushes an `error` heartbeat
  (`nvml_init_failed`, empty GPU inventory) so a host that lost its driver
  still surfaces a non-ok freshness signal on the board, then exits non-zero.

## 1.1.0 - 2026-06-17

- Added optional GUA Board cloud sync. `gua enroll` claims a one-time
  enrollment token from a GUA Board workspace and stores a host-scoped,
  write-only agent token in `~/.gua/cloud.json` (mode 0600). `gua sync-once`
  collects one snapshot, writes it to the local history database first, then
  pushes the latest state to GUA Board; a failed push never blocks or rolls
  back the local write. Cloud sync is entirely optional — local collection,
  storage, and `gua report` are unchanged when no host is enrolled, and no new
  runtime dependency is added (the client uses the standard library).
- Enriched NVML collection with per-GPU name, total/used memory, temperature,
  power, and physical index, plus per-process name (from `/proc/<pid>/comm`;
  full command lines are never collected). The local SQLite schema gained
  these columns plus a normalized `gpu_device` table. The migration is
  additive (nullable columns), so existing `~/.gua/gua.db` databases upgrade
  in place and `gua report` output is unaffected.

## 1.0.3 - 2026-05-27

- Changed default `gua` state paths to `~/.gua/gua.db`, `~/.gua/gua.pid`,
  and `~/.gua/gua.log`; the default database now acts as an appendable local
  history database.
- Record daemon run intervals in SQLite and attach samples to a run, so
  `gua report` uses recorded intervals by default. `--interval` is now an
  override and a fallback for legacy rows without interval metadata.

## 1.0.2 - 2026-05-15

- Hardened `gua status` and `gua stop` so stale PID files do not act on
  unrelated live processes.
- Clarified report output by explaining sample units, classification rules,
  interval-dependent GPU-hours, and heatmap density.
- Split §2 from generic "Waste" into idle-held capacity and truly-idle
  capacity. The equivalent-GPU figures now use GPUs present in the report
  window instead of the entire database.
- Made §4 Top identities aggregate by identity/GPU/tick before converting to
  GPU-hours, so reports may show lower per-user GPU-hours when one user has
  multiple processes on the same GPU at the same tick.
- Warn when NVML process-list visibility is unavailable for a GPU.

## 1.0.1 - 2026-05-15

- Made `gua` the documented command surface for daemon, report, demo, and doctor output.
- Made `gua daemon` start the collector in the background by default, with
  `gua daemon --foreground` available for systemd and debugging.
- Added `gua start`, `gua status`, and `gua stop` for background collector management.

## 1.0.0 - 2026-05-15

Bare-metal 1.0 narrows `gpu-usage-audit` to one clear workflow: inspect the
current NVIDIA Linux host, collect NVML telemetry into SQLite, and render a
retrospective active / idle-held / truly-idle report.

- Reset the product surface to a single local bare-metal host.
- Added `gua doctor` for read-only local NVIDIA/NVML/database readiness checks.
- Made `nvidia-ml-py` a default dependency while keeping the `nvml` extra as a
  compatibility alias.
- Defaulted `daemon` and `report` to `/tmp/gua.db`.
- Made `daemon` refuse an existing database and `report` refuse a missing one.
- Kept the schema at v1: `host`, `gpu_sample`, `proc_sample`.
- Removed post-1.0 auto-runtime planning artifacts and runtime-detection code.
- Preserved `demo` for GPU-less output checks with fake telemetry.

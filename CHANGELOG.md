# Changelog

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

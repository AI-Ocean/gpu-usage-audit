# Changelog

## Unreleased

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

# Changelog

All notable changes to this project will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- `OpenDB` now sets `journal_mode=WAL` and `busy_timeout=5000` before
  applying the schema, so the daemon and `report` can run against the
  same database file without `SQLITE_BUSY` on short reads.

### Added
- Indexes `idx_gpu_sample_uuid_ts` and `idx_proc_sample_uuid_ts` on
  `(gpu_uuid, ts)` — report queries slice by card over a time window.

### Planned for v0.2.0
- Real NVML tier implementation (build tag `nvml_real`) so the daemon
  collects actual GPU telemetry on NVIDIA hosts, not only the bundled
  fake source.

## [0.1.0] — 2026-05-11

### Added
- `daemon` subcommand — periodic GPU/process sampling into SQLite with
  anti-drift scheduling, signal-driven shutdown, and single-transaction
  per tick.
- `report` subcommand — five-section retrospective report from any
  accumulated database file:
  - §1 Headline: active / idle-held / truly-idle proportions with a
    glyph-differentiated three-bar.
  - §2 Waste: idle GPU-hours and equivalent unused GPU count.
  - §3 Per-GPU: idle-held breakdown by card.
  - §4 Top identities: by-user GPU-hours and idle-held share.
  - §5 Heatmap: day-of-week × hour activity grid.
- `FakeTier` — deterministic time-varying fake telemetry source so the
  daemon is exercisable on any host (no NVIDIA driver required).
- Identity resolution via `/proc/<pid>/loginuid` with `UserLookupFunc`
  abstraction; pluggable table-based lookup for tests.
- Host environment auto-detection (`bare` / `docker` / `k8s`) from
  `/proc/1/cgroup`.
- Three-table schema (`host`, `gpu_sample`, `proc_sample`) — minimal
  surface aligned to the idle-held question.
- Apache 2.0 license, Makefile (`build` / `run` / `clean`),
  `--version` injected at link time.

### Notes
- v0.1.0 is the *first cut* derived from the gpu-usage-audit-v2 design
  exercise. Telemetry is fake by default; real NVML support arrives
  in v0.2.0.
- The legacy `gpu-usage-audit` (v0.1.x) project is being archived in
  favour of this rewrite.

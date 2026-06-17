# gpu-usage-audit

Single-host NVIDIA GPU usage audit for finding **idle-held** GPUs: cards that look idle by utilization, but are still held by a process through GPU memory.

[![PyPI](https://img.shields.io/pypi/v/gpu-usage-audit.svg)](https://pypi.org/project/gpu-usage-audit/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://pypi.org/project/gpu-usage-audit/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![GitHub Release](https://img.shields.io/github/v/release/AI-Ocean/gpu-usage-audit)](https://github.com/AI-Ocean/gpu-usage-audit/releases)

[English](README.md) · [한국어](README.ko.md) · [Releases](https://github.com/AI-Ocean/gpu-usage-audit/releases) · [Issues](https://github.com/AI-Ocean/gpu-usage-audit/issues)

---

## About

gpu-usage-audit records local NVIDIA/NVML telemetry into SQLite and renders a retrospective report that separates GPU card-ticks into:

- `active`: utilization is doing real work
- `idle-held`: utilization is low, but a process still holds GPU memory
- `truly-idle`: no meaningful GPU process memory is present

The second category is the point. A notebook can sit at 1% SM utilization while keeping an 8 GB tensor allocated. Conventional dashboards usually flatten that into “idle”; this tool shows that the card is effectively unavailable.

## Features

- Single-host, bare-metal NVIDIA GPU audit
- `gua doctor` readiness check for `/dev/nvidia*`, `nvidia-smi`, NVML, and DB path
- Background collector with `gua daemon`, `gua status`, and `gua stop`
- SQLite history database at `~/.gua/gua.db` by default
- Report sections for headline split, idle capacity, per-GPU state, top identities, and time-of-day heatmap
- Daemon interval metadata stored per run, so reports compute GPU-hours correctly across mixed 30s / 10s runs
- GPU-less `gua demo` command with deterministic fake telemetry
- No cluster runtime dependency; no Kubernetes, Slurm, Docker, or remote-node scan in the 1.0 scope

## Installation

The recommended install path is PyPI via [uv](https://docs.astral.sh/uv/):

```sh
uv tool install gpu-usage-audit
```

Update or remove it with:

```sh
uv tool upgrade gpu-usage-audit
uv tool uninstall gpu-usage-audit
```

Manual wheel downloads are available from GitHub Releases:

```sh
BASE="https://github.com/AI-Ocean/gpu-usage-audit/releases/download/v1.0.3"
WHEEL="gpu_usage_audit-1.0.3-py3-none-any.whl"

curl -fsSLO "$BASE/$WHEEL"
curl -fsSLO "$BASE/SHA256SUMS"
sha256sum -c SHA256SUMS --ignore-missing

uvx --from "./$WHEEL" gua doctor
```

## Quick Start

On an NVIDIA GPU host:

```sh
gua doctor
gua daemon --interval 30s
gua status
gua report --since 1h
gua stop
```

`gua doctor` is read-only. It does not need `sudo`; run it as the same user that will run the daemon.

Default local state lives under `~/.gua/`:

| Path | Purpose |
| --- | --- |
| `~/.gua/gua.db` | SQLite history database |
| `~/.gua/gua.pid` | background daemon PID file |
| `~/.gua/gua.log` | daemon stdout/stderr log |

The default DB is an appendable local history database. Later daemon runs append to it. If you pass a custom `--db PATH`, daemon still refuses an existing file to avoid mixing ad hoc runs by accident.

## Report Preview

```text
$ gua report --since 1h
gua — lab-a100 (bare, driver 560.35.05)  Window: 1:00:00

§1 Headline
  basis: one sample = one GPU card at one daemon tick
  rules: active >=10% util; idle-held <10% util with >100 MB process memory
  active       █   15.7%
  idle-held    ▒   45.1%
  truly-idle   ░   39.2%
  (51 samples)

§2 Idle capacity
  converted from card-ticks to GPU-hours using recorded daemon interval
  idle-held: ~0.31 GPU-hours, ~1.53 GPUs equivalently unavailable
  truly-idle: ~0.12 GPU-hours, ~1.00 GPUs equivalently free

§3 Per-GPU
§4 Top identities
§5 Time-of-day heatmap (UTC)
```

Reports can run while the daemon is writing; SQLite WAL mode handles concurrent reads. Reports also work after the daemon has stopped, as long as the DB file exists.

## Commands

| Command | Description |
| --- | --- |
| `gua doctor` | Check local NVIDIA/NVML readiness and DB path status |
| `gua daemon` | Start background collection on the local NVIDIA host |
| `gua start` | Alias for `gua daemon` |
| `gua status` | Show whether the managed background collector is running |
| `gua stop` | Stop the managed background collector |
| `gua report` | Render the retrospective report from SQLite |
| `gua demo` | Generate a fake local report without a GPU |
| `gua enroll` | Connect this host to a GUA Board workspace (optional cloud sync) |
| `gua sync-once` | Collect one snapshot and push the latest state to GUA Board |
| `gua version` | Print version |

## Important Options

```sh
gua daemon [--db PATH] [--interval D] [--pid-file PATH] [--log-file PATH]
gua daemon --foreground [--db PATH] [--interval D]
gua report [--db PATH] [--since D] [--interval D] [--width N]
gua demo [--db PATH] [--ticks N] [--interval D]
```

- `--interval` on `daemon` controls sampling cadence. Default: `30s`.
- `--interval` on `report` is optional. New DB rows use the interval recorded by each daemon run. Use report `--interval D` only as an override or for legacy rows without interval metadata.
- `--since` accepts `ms`, `s`, `m`, `h`, and `d`, with no upper bound.
- `--foreground` is intended for systemd and debugging.

## Demo Without a GPU

```sh
gua demo
```

The demo records deterministic fake telemetry and immediately prints the report shape.

## Systemd Example

```ini
[Unit]
Description=gua daemon
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/gua daemon --foreground --db /var/lib/gua/gua.db --interval 30s
Restart=on-failure
User=gua

[Install]
WantedBy=multi-user.target
```

Then run:

```sh
systemctl enable --now gpu-usage-audit
```

## Cloud Sync (GUA Board, optional)

`gpu-usage-audit` runs fully local by default. If you also use GUA Board (a separate service that shows the latest GPU availability across several servers in one place), you can optionally connect a host:

```sh
# 1. In the GUA Board web UI, register a server and copy the one-time enrollment token.
# 2. On the GPU host:
gua enroll --server-url https://board.example.com --enrollment-token <TOKEN>
# 3. Push the current snapshot (run on a timer or after `gua daemon`):
gua sync-once
```

How it works and what it does not do:

- `enroll` exchanges the one-time token for a host-scoped, write-only agent token, stored in `~/.gua/cloud.json` with mode `0600`. The token can only write this host's observations — it cannot read reservations, users, or other hosts.
- `sync-once` collects one snapshot, **writes it to the local database first**, then pushes only the latest state. A failed push never blocks or rolls back the local write.
- Only the latest snapshot is sent. Historical ticks are kept locally and are never replayed to the server.
- Process telemetry is limited to PID, Linux user, process name (`/proc/<pid>/comm`), and GPU memory — never full command lines.
- Cloud sync adds no new runtime dependency (the client uses the Python standard library).

Override the config or database path with `--config PATH` / `--db PATH`, and use `gua sync-once --fake` to exercise the flow without a GPU.

## Classification Rules

Each daemon tick records per-card utilization and per-process GPU memory. The report classifies each GPU card at each tick with these rules:

```text
util >= 10                  -> active
util <  10 AND mem >  100   -> idle-held
util <  10 AND mem <= 100   -> truly-idle
```

The 100 MB threshold absorbs runtime baselines such as importing PyTorch or TensorFlow.

## Development

```sh
git clone https://github.com/AI-Ocean/gpu-usage-audit
cd gpu-usage-audit
uv sync
uv run python -m pytest
uv run ruff check
uv run ruff format --check
uv run python -m mypy
uv run gua demo
```

CI runs ruff, format check, mypy, pytest, build, and wheel smoke tests. Tag pushes (`v*`) build release assets and publish to PyPI through Trusted Publishing.

## Non-goals

This is a single-host retrospective tool. Live dashboards, multi-host aggregation, quotas, Kubernetes cluster scans, Slurm joins, Docker/Podman runtime fallback, and pod-name resolution are outside the bare-metal 1.0 scope.

The Go v0.1.0 implementation remains available at tag `v0.1.0` and branch [`go-archive`](https://github.com/AI-Ocean/gpu-usage-audit/tree/go-archive).

## License

Apache License 2.0. See [LICENSE](LICENSE).

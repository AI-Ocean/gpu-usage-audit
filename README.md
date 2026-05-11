# gpu-usage-audit

A single-host diagnostic daemon that records NVIDIA GPU utilization to
SQLite and produces a retrospective report separating *active* use from
*allocated-but-idle* ("idle-held") and *truly idle* (no process at all).

Conventional dashboards collapse the latter two. **Surfacing
idle-held as its own number is the entire point.** Someone left a
Jupyter notebook open with an 8 GB tensor on the GPU and went to
lunch — `nvidia-smi` will show 1% utilization, but the card is
*unusable* by anyone else. This tool measures that.

> **Status:** v0.2.0 — daemon (`--tier fake` **or** `--tier nvml`) and
> report work end-to-end. The Go v0.1.0 implementation remains
> downloadable at tag `v0.1.0` / branch
> [`go-archive`](https://github.com/AI-Ocean/gpu-usage-audit/tree/go-archive).

## What you get

```
$ gpu-usage-audit report --db /var/lib/gua/gua.db --since 1h
gpu-usage-audit — lab-a100 (bare, driver 560.35.05)  Window: 1:00:00

§1 Headline
  █████████▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒░░░░░░░░░░░░░░░░░░░░░░░░
  active       █   15.7%
  idle-held    ▒   45.1%       ← this is the number conventional tools miss
  truly-idle   ░   39.2%
  (51 samples)

§2 Waste
  ~0.43 GPU-hours idle, ~2.53 GPUs equivalently unused

§3 Per-GPU
  GPU-0     active  47.1%  idle-held  35.3%  truly-idle  17.6%
  GPU-1     active   0.0%  idle-held 100.0%  truly-idle   0.0%
  GPU-2     active   0.0%  idle-held   0.0%  truly-idle 100.0%

§4 Top identities
  identity              gpu-hours   idle-held
  alice                      0.42       42.9%
  bob                        0.28      100.0%

§5 Time-of-day heatmap (UTC)
        0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3
  Mon               .
```

The 3-bar collapses every card × every tick over the window into the
active / idle-held / truly-idle split. **`idle-held` rows are the
embarrassing category**: a process is holding GPU memory but the SM
utilization is below 10%.

## Quick demo (no GPU required)

The bundled `FakeTier` produces a deterministic 5-tick GPU workload —
active learning → idle-held memory → cleanup — so you can see the
report shape on any Linux host before you point it at real hardware.

```sh
WHEEL="https://github.com/AI-Ocean/gpu-usage-audit/releases/download/v0.2.0/gpu_usage_audit-0.2.0-py3-none-any.whl"

# Start a short-interval daemon (fake telemetry):
uvx --from "$WHEEL" gpu-usage-audit daemon --db /tmp/gua.db --interval 1s &

# Wait a few seconds, then run the report from another shell:
sleep 5
uvx --from "$WHEEL" gpu-usage-audit report --db /tmp/gua.db --since 1m --interval 1s
```

## Real NVIDIA GPU telemetry

On an NVIDIA host, install the `[nvml]` extra and pass `--tier nvml`:

```sh
# One-shot via uvx (recommended)
uvx --from "$WHEEL" --with nvidia-ml-py \
    gpu-usage-audit daemon --db /tmp/gua.db --tier nvml --interval 30s

# Or a persistent install
pip install 'gpu-usage-audit[nvml] @ <WHEEL>'
gpu-usage-audit daemon --db /tmp/gua.db --tier nvml --interval 30s
```

> `--tier nvml` requires the NVIDIA driver and `libnvidia-ml.so.1`
> reachable from `LD_LIBRARY_PATH`. On a driver-less host the daemon
> exits with `NVML Shared Library Not Found`.

## Usage

`gpu-usage-audit` is two commands sharing one SQLite file:

| Command  | What it does                                                |
| -------- | ----------------------------------------------------------- |
| `daemon` | Long-running background process. Samples GPU/process state on every tick and **appends** to the database. Stop with Ctrl+C (SIGINT) or `systemctl stop`. |
| `report` | One-shot read against the accumulated database. Safe to run **while the daemon is still writing** — SQLite WAL mode handles the concurrency. |

### `daemon`

```
gpu-usage-audit daemon --db PATH [--interval D] [--tier {fake,nvml}]
```

- `--db PATH` — SQLite file to write to. Created if missing. WAL mode
  is enabled automatically.
- `--interval D` (default `30s`) — how often to sample. Accepts `30s`,
  `1m`, `200ms`, etc. Shorter intervals give finer time resolution but
  more rows; `30s` is a good default for real workloads.
- `--tier fake|nvml` (default `fake`) — telemetry source. Use `nvml`
  on real NVIDIA hosts; `fake` runs on any Linux box for the demo.

Each tick prints a one-line summary to stdout:

```
Tick 0  ts=12:34:56.789  GPU-0=active      GPU-1=idle-held   GPU-2=truly-idle
```

On shutdown it prints the cumulative row count.

### `report`

```
gpu-usage-audit report --db PATH [--since D] [--interval D] [--width N]
```

- `--db PATH` — same SQLite file the daemon writes to.
- `--since D` (default `1h`) — the report window. `--since 24h` gives
  yesterday's slice, `--since 7d` gives the week, etc.
- `--interval D` (default `30s`) — **must match what the daemon used**.
  This is how §2 (Waste) and §4 (Top identities) convert tick counts
  to GPU-hours. Mismatched intervals → wrong GPU-hours.
- `--width N` (default `60`) — width of the §1 three-bar in characters.

### Operational notes

- **Same `--interval` on both sides.** If you ran the daemon with
  `--interval 30s`, run `report --interval 30s` too.
- **Let it run for a while.** §1/§3 are meaningful after one tick;
  §4 (Top identities) needs hours; §5 (Heatmap) needs days.
- **WAL leaves sidecar files** (`gua.db-wal`, `gua.db-shm`). They are
  cleaned up automatically when the last connection closes.
- **DB size**: ~50 MB per host per 30 days at 12 GPUs (extrapolated
  from Go v0.1.0; not yet re-measured for the Python rewrite).

### Running as a systemd service

For a long-running deployment, drop a unit file in
`/etc/systemd/system/gpu-usage-audit.service`:

```ini
[Unit]
Description=gpu-usage-audit daemon
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/gpu-usage-audit daemon --db /var/lib/gua/gua.db --tier nvml --interval 30s
Restart=on-failure
User=gua

[Install]
WantedBy=multi-user.target
```

Then `systemctl enable --now gpu-usage-audit`.

## How the classification works

Each tick of the daemon records:

- per-card: `util_pct` (SM utilization)
- per-process: `mem_used_mb` per `(card, pid)`

The report aggregates per card × per tick:

```
util >= 10                  → active        (compute is happening)
util <  10 AND mem >  100   → idle-held     (memory is held, SM is cold)
util <  10 AND mem <= 100   → truly-idle    (the card is genuinely free)
```

The 100 MB threshold absorbs the PyTorch/TF runtime baseline so
importing torch doesn't count as "holding the GPU".

## Development

Requires [uv](https://docs.astral.sh/uv/) (uv pins the Python version
automatically; `requires-python = ">=3.12"`).

```sh
git clone https://github.com/AI-Ocean/gpu-usage-audit
cd gpu-usage-audit
uv sync                          # create .venv, install dev deps
uv run pytest                    # run the test suite
uv run ruff check                # lint
uv run mypy                      # type-check (strict)
uv run gpu-usage-audit version   # exercise the CLI entry point
```

CI runs ruff + mypy + pytest on every push and PR. Tag pushes (`v*`)
build sdist + wheel and create a GitHub Release with auto-generated
notes.

## Non-goals

This is a **single-host retrospective** tool. Live dashboards, multi-host
aggregation, quotas, and pod-name resolution are out of scope — those
belong above the host layer. If this tool surfaces enough idle-held to
make scheduling worth solving, see [ocean-all](https://github.com/AI-Ocean).

## License

Apache License 2.0 — see [LICENSE](LICENSE).

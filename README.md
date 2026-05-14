# gpu-usage-audit

A single-host diagnostic daemon that records NVIDIA GPU utilization to
SQLite and produces a retrospective report separating *active* use from
*allocated-but-idle* ("idle-held") and *truly idle* (no process at all).

Conventional dashboards collapse the latter two. **Surfacing
idle-held as its own number is the entire point.** Someone left a
Jupyter notebook open with an 8 GB tensor on the GPU and went to
lunch — `nvidia-smi` will show 1% utilization, but the card is
*unusable* by anyone else. This tool measures that.

> **Status:** main includes read-only `gua doctor` runtime diagnostics and
> `gua start --dry-run` plan output. Managed start/status/report/stop/uninstall
> flows are still placeholders. `daemon` still runs on a real NVIDIA host,
> `demo` runs anywhere (no GPU required), and `report` reads either. The Go
> v0.1.0 implementation remains downloadable at tag `v0.1.0` / branch
> [`go-archive`](https://github.com/AI-Ocean/gpu-usage-audit/tree/go-archive).

## Install

The recommended install path is PyPI via uv. The package has no core
runtime dependencies.

Requires [uv](https://docs.astral.sh/uv/). In normal online environments,
uv creates the isolated tool environment and manages the needed Python
runtime. If Python downloads are disabled by local policy, install Python
3.12+ first.

```sh
uv tool install gpu-usage-audit

gua doctor
gua start --dry-run
gpu-usage-audit demo
```

`gua doctor` and `gua start --dry-run` are intentionally read-only: they
inspect the local environment, print a recommended runtime plan, and make
no system, service, cluster, or database changes. Use
`gpu-usage-audit daemon/report/demo` for the existing compatibility
workflow.

Use `gua doctor --json` for the same report in a machine-readable form.
The JSON includes local host and cluster diagnostic details such as paths
and command stderr, so review it before sharing it outside your team.
`gua doctor` does not need `sudo`; running it as root can change which
Kubernetes config `kubectl` sees.

Available `gua` subcommands: `doctor`, `start`, `status`, `report`,
`stop`, and `uninstall`.

Update or remove the installed tool with uv:

```sh
uv tool upgrade gpu-usage-audit
uv tool uninstall gpu-usage-audit
```

`uv tool uninstall gpu-usage-audit` removes the installed Python tool and
its `gua` / `gpu-usage-audit` commands. `gua uninstall` is different: it
is reserved for future runtime cleanup and is a no-op placeholder.

GitHub Release assets are also available for manual download:

```sh
BASE="https://github.com/AI-Ocean/gpu-usage-audit/releases/download/v0.4.1"
WHEEL="gpu_usage_audit-0.4.1-py3-none-any.whl"

curl -fsSLO "$BASE/$WHEEL"
curl -fsSLO "$BASE/SHA256SUMS"
sha256sum -c SHA256SUMS --ignore-missing

uvx --from "./$WHEEL" gua doctor
```

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

## Demo (no GPU required)

The `demo` subcommand records 30 ticks of fake telemetry and prints the
report — all in one process, no second shell needed.

```sh
gpu-usage-audit demo
```

The bundled `FakeTier` produces a deterministic 5-tick workload —
active learning → idle-held memory → cleanup — so the output is the
same every run. Adjust the shape with `--ticks N` and `--interval D`.

## Real NVIDIA GPU host

On an NVIDIA host, install the `[nvml]` extra and run `daemon`:

```sh
# Add the NVML Python package to the tool environment.
uv tool install --force --with nvidia-ml-py gpu-usage-audit

gpu-usage-audit daemon --db /tmp/gua.db --interval 30s
```

Run the report from another shell:

```sh
gpu-usage-audit report --db /tmp/gua.db --since 1h --interval 30s
```

If `--db` is omitted, both `daemon` and `report` use `/tmp/gua.db`.
`daemon` refuses to start when that database file already exists, so a
new collection run does not silently append to an old test database.

> The daemon requires the NVIDIA driver and `libnvidia-ml.so.1`. On a
> driver-less host it exits with `NVML Shared Library Not Found`. For a
> driverless box, use `demo` instead.

## Usage

`gpu-usage-audit` has three commands sharing one SQLite file:

| Command  | What it does                                                |
| -------- | ----------------------------------------------------------- |
| `daemon` | Long-running background process. Samples real NVML telemetry on every tick and writes to a new database. Stop with Ctrl+C (SIGINT) or `systemctl stop`. NVIDIA host required. |
| `report` | One-shot read against the accumulated database. Safe to run **while the daemon is still writing** — SQLite WAL mode handles the concurrency. |
| `demo`   | Self-contained showcase. Records N fake ticks and immediately prints the report. No GPU, no second shell, no operational meaning — just to see the output shape. |

### `daemon`

```
gpu-usage-audit daemon [--db PATH] [--interval D]
```

- `--db PATH` (default `/tmp/gua.db`) — SQLite file to create and write
  to. The daemon exits with an error if the file already exists. WAL mode
  is enabled automatically.
- `--interval D` (default `30s`) — how often to sample. Accepts `30s`,
  `1m`, `200ms`, etc.

Each tick prints a one-line summary to stdout; on shutdown the cumulative
row count is printed.

### `report`

```
gpu-usage-audit report [--db PATH] [--since D] [--interval D] [--width N]
```

- `--db PATH` (default `/tmp/gua.db`) — same SQLite file the daemon writes
  to. The report exits with an error if the file does not exist.
- `--since D` (default `1h`) — the report window. **No upper bound** —
  `--since 365d` is accepted. The effective window is min(`--since`, age
  of oldest sample), so passing a huge `--since` is the same as "all
  data". Units: `ms`, `s`, `m`, `h`, `d` (no `w`; use `7d`).
- `--interval D` (default `30s`) — **must match what the daemon used**.
  This is how §2 (Waste) and §4 (Top identities) convert tick counts
  to GPU-hours. Mismatched intervals → wrong GPU-hours.
- `--width N` (default `60`) — width of the §1 three-bar in characters.

### `demo`

```
gpu-usage-audit demo [--db PATH] [--ticks N] [--interval D]
```

- `--db PATH` (optional) — if omitted, a fresh temporary database is
  created and its path is printed to stderr.
- `--ticks N` (default `30`) — how many fake ticks to record before
  printing the report.
- `--interval D` (default `1s`) — tick spacing.

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
ExecStart=/usr/local/bin/gpu-usage-audit daemon --db /var/lib/gua/gua.db --interval 30s
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
uv run gpu-usage-audit demo      # see the report shape locally
```

CI runs ruff + format check + mypy + pytest, then builds and smoke-tests
the wheel on every push and PR. Tag pushes (`v*`) rerun the same checks,
build sdist + wheel, smoke-test the wheel, and create a GitHub Release
with auto-generated notes. Release tags also publish the wheel and sdist
to PyPI through Trusted Publishing.

## Non-goals

This is a **single-host retrospective** tool. Live dashboards, multi-host
aggregation, quotas, and pod-name resolution are out of scope — those
belong above the host layer. If this tool surfaces enough idle-held to
make scheduling worth solving, see [ocean-all](https://github.com/AI-Ocean).

## License

Apache License 2.0 — see [LICENSE](LICENSE).

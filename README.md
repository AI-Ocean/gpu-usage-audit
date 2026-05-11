# gpu-usage-audit

A single-host diagnostic daemon that records NVIDIA GPU utilization to
SQLite and produces a retrospective report separating *active* use from
*allocated-but-idle* ("idle-held") and *truly idle* (no process at all).

Conventional dashboards collapse the latter two. **Surfacing
idle-held as its own number is the entire point.** Someone left a
Jupyter notebook open with an 8 GB tensor on the GPU and went to
lunch — `nvidia-smi` will show 1% utilization, but the card is
*unusable* by anyone else. This tool measures that.

Published by [AIOcean](https://github.com/AI-Ocean) as the awareness
funnel for the **ocean-all** GPU resource management platform. The
daemon itself is fully offline and never touches the network.

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

Grab the wheel from the
[latest release](https://github.com/AI-Ocean/gpu-usage-audit/releases/latest)
and:

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

Run the report from another shell:

```sh
gpu-usage-audit report --db /tmp/gua.db --since 1h --interval 30s
```

> `--tier nvml` requires the NVIDIA driver and `libnvidia-ml.so.1`
> reachable from `LD_LIBRARY_PATH`. On a driver-less host the daemon
> exits with `NVML Shared Library Not Found` and a hint to install
> the extra.

## Future install path

Once PyPI publish is wired, the same will work without the URL:

```sh
uvx gpu-usage-audit daemon --db /tmp/gua.db --interval 30s
pip install 'gpu-usage-audit[nvml]'
```

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

## Resource budget

Not yet measured for the Python rewrite. The Go v0.1.0 cost (for
reference) was < 0.5 % of one CPU at a 30 s tick cadence, < 30 MB
RSS, and ~50 MB / host / 30 days at 12 GPUs. The Python daemon does
the same work with the same schema — expect the same disk footprint;
CPU/RSS to be re-measured once it runs on a real GPU host.

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

CI (`.github/workflows/ci.yml`) runs ruff + mypy + pytest on every push
and PR. Tag pushes (`v*`) build sdist + wheel and create a GitHub
Release with auto-generated notes.

## Non-goals

Deliberately out of scope:

- Multi-host aggregation / `--combine` / push-to-cloud
- Kubernetes pod-name resolution (cgroup-level identity only)
- Quotas, scheduling, or kill-idle enforcement
- Web dashboard or live-monitoring view (use `nvtop`, DCGM, Grafana)

Those belong to a platform layer above the host. **That's where
[ocean-all](https://github.com/AI-Ocean) comes in** — if this tool
shows you that a meaningful slice of your fleet is idle-held, the
next step is shared-pool scheduling, which is a problem this tool
intentionally does not try to solve.

## License

Apache License 2.0 — see [LICENSE](LICENSE).

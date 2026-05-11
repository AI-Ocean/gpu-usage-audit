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

> **Status:** v0.2.0 — `daemon` (fake **or** real NVIDIA NVML) and
> `report` work end-to-end. The Go v0.1.0 implementation remains
> downloadable at tag `v0.1.0` / branch
> [`go-archive`](https://github.com/AI-Ocean/gpu-usage-audit/tree/go-archive).

## What you get (target shape — being ported from Go v0.1.0)

```
$ gpu-usage-audit report --db /var/lib/gua/gua.db --since 1h
gpu-usage-audit — lab-a100 (bare, driver 560.35.05)  Window: 1h0m0s

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

The 3-bar collapses every card × every tick over the window into
the active / idle-held / truly-idle split. **`idle-held` rows are
the embarrassing category**: a process is holding GPU memory but
the SM utilization is below 10%.

## Install

Until PyPI publish is enabled, install the wheel from the
[v0.2.0 release](https://github.com/AI-Ocean/gpu-usage-audit/releases/tag/v0.2.0):

```sh
# Recommended: uvx (no global install, isolated environment)
uvx --from https://github.com/AI-Ocean/gpu-usage-audit/releases/download/v0.2.0/gpu_usage_audit-0.2.0-py3-none-any.whl \
    gpu-usage-audit version

# Or with pip
pip install https://github.com/AI-Ocean/gpu-usage-audit/releases/download/v0.2.0/gpu_usage_audit-0.2.0-py3-none-any.whl
```

Once PyPI publish is wired, the intended UX is:

```sh
uvx gpu-usage-audit daemon --db /tmp/gua.db --interval 30s
pip install gpu-usage-audit
```

### Real NVIDIA GPU telemetry

The default `--tier fake` works on any host. For real telemetry on
NVIDIA GPU machines, install the `[nvml]` extra (depends on
`nvidia-ml-py`):

```sh
# pip
pip install 'gpu-usage-audit[nvml]'

# uvx — bring nvidia-ml-py along for a one-shot run
uvx --with nvidia-ml-py gpu-usage-audit daemon --db /tmp/gua.db --tier nvml --interval 30s
```

Then run the report from another shell:

```sh
gpu-usage-audit report --db /tmp/gua.db --since 1h --interval 30s
```

If you want the Go v0.1.0 binary instead, see the
[v0.1.0 release](https://github.com/AI-Ocean/gpu-usage-audit/releases/tag/v0.1.0).

## Development

Requires [uv](https://docs.astral.sh/uv/) (uv pins the Python version
automatically; `requires-python = ">=3.12"`).

```sh
git clone https://github.com/AI-Ocean/gpu-usage-audit
cd gpu-usage-audit
uv sync                          # create .venv, install dev deps
uv run pytest                    # run the test suite
uv run gpu-usage-audit version   # exercise the CLI entry point
```

## How the classification works (carried over from v0.1.0)

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

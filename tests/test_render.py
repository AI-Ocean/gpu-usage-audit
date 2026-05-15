"""§1~§5 render 함수 출력 *형태* 검증.

문자열 픽셀 단위 동등이 아니라 *핵심 라벨/숫자/글리프* 가 출력에
포함되는지 검사. 사소한 공백 변경에 깨지지 않도록.
"""

from __future__ import annotations

import io
from datetime import timedelta

from gpu_usage_audit.model import HostRow
from gpu_usage_audit.render import (
    GLYPH_ACTIVE,
    GLYPH_IDLE_HELD,
    GLYPH_TRULY_IDLE,
    render_headline,
    render_heatmap,
    render_per_gpu,
    render_top_identities,
    render_waste,
)
from gpu_usage_audit.report import Headline, HeatmapCell, PerGPU, TopIdentity, Waste


def _render(fn, *args, **kwargs) -> str:  # type: ignore[no-untyped-def]
    buf = io.StringIO()
    fn(buf, *args, **kwargs)
    return buf.getvalue()


# ── §1 ──────────────────────────────────────────────────────────


def test_render_headline_with_host_and_samples() -> None:
    out = _render(
        render_headline,
        HostRow(hostname="lab-a100", env_kind="bare", driver_version="560.35.05"),
        Headline(active=0.25, idle_held=0.5, truly_idle=0.25, samples=8),
        timedelta(hours=1),
        width=60,
    )
    assert "gua — lab-a100 (bare, driver 560.35.05)" in out
    assert "Window: 1:00:00" in out
    assert "§1 Headline" in out
    assert "(8 samples)" in out
    # 3-bar 가 60 칸이고 active 글리프가 정확히 15개 (round(0.25*60)).
    bar_line = next(
        line for line in out.splitlines() if GLYPH_ACTIVE in line and "active" not in line
    )
    assert bar_line.count(GLYPH_ACTIVE) == 15
    assert bar_line.count(GLYPH_IDLE_HELD) == 30
    assert bar_line.count(GLYPH_TRULY_IDLE) == 15


def test_render_headline_empty_window() -> None:
    out = _render(
        render_headline,
        HostRow(hostname="h", env_kind="bare"),
        Headline(),
        timedelta(hours=1),
        width=60,
    )
    assert "(no samples in window)" in out
    # 빈 윈도우면 글리프 라인 없음.
    assert GLYPH_ACTIVE not in out


def test_render_headline_no_host_row() -> None:
    out = _render(render_headline, HostRow(), Headline(), timedelta(hours=1), width=60)
    assert "no host row" in out


# ── §2 ──────────────────────────────────────────────────────────


def test_render_waste() -> None:
    out = _render(render_waste, Waste(idle_gpu_hours=0.43, equiv_unused=2.53, samples=51))
    assert "§2 Waste" in out
    assert "0.43" in out
    assert "2.53" in out


def test_render_waste_empty() -> None:
    out = _render(render_waste, Waste())
    assert "(no samples in window)" in out


# ── §3 ──────────────────────────────────────────────────────────


def test_render_per_gpu() -> None:
    out = _render(
        render_per_gpu,
        [
            PerGPU(uuid="GPU-0", active=0.5, idle_held=0.0, truly_idle=0.5, samples=4),
            PerGPU(uuid="GPU-1", active=0.0, idle_held=1.0, truly_idle=0.0, samples=4),
        ],
    )
    assert "§3 Per-GPU" in out
    assert "GPU-0" in out and "GPU-1" in out
    assert "100.0%" in out  # GPU-1 의 idle-held 100%


def test_render_per_gpu_empty() -> None:
    assert "(no GPU cards in window)" in _render(render_per_gpu, [])


# ── §4 ──────────────────────────────────────────────────────────


def test_render_top_identities() -> None:
    out = _render(
        render_top_identities,
        [
            TopIdentity(identity="bob", gpu_hours=0.42, idle_held=1.0),
            TopIdentity(identity="alice", gpu_hours=0.28, idle_held=0.0),
        ],
    )
    assert "§4 Top identities" in out
    assert "bob" in out
    assert "alice" in out
    assert "100.0%" in out and "0.0%" in out


def test_render_top_identities_empty() -> None:
    assert "(no processes in window)" in _render(render_top_identities, [])


# ── §5 ──────────────────────────────────────────────────────────


def test_render_heatmap() -> None:
    out = _render(
        render_heatmap,
        [
            HeatmapCell(dow=1, hour=0, active=0.5, samples=4),
            HeatmapCell(dow=2, hour=1, active=0.5, samples=2),
        ],
    )
    assert "§5 Time-of-day heatmap" in out
    # 요일 라벨이 7개 모두 등장.
    for label in ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"):
        assert label in out


def test_render_heatmap_empty() -> None:
    assert "(no samples in window)" in _render(render_heatmap, [])

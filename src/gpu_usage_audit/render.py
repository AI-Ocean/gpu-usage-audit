"""§1~§5 report 렌더링.

색 의존성 회피 — TTY/isatty 토글 없이 *글자 자체* (█/▒/░) 로 세 분류를
시각적으로 구분. 파일 redirect 시에도 출력이 깨끗.

모든 함수가 TextIO 를 받음 — 테스트에서 io.StringIO 로 격리해 검증
가능. Go 의 io.Writer 와 동등 의도.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TextIO

from .model import HostRow
from .report import Headline, HeatmapCell, IdleCapacity, PerGPU, TopIdentity

# 카테고리별로 *다른 글자* 를 써서 색깔 없이도 시각적 구분이 되게.
GLYPH_ACTIVE = "█"  # 가장 진한 블록
GLYPH_IDLE_HELD = "▒"  # 중간 음영
GLYPH_TRULY_IDLE = "░"  # 가장 옅은 블록

# active 비율 [0,1] 을 10단계 ASCII 농도 문자에 매핑.
# 빈 셀(데이터 없음) 과 0% 활성 셀을 구별하기 위해 빈 셀은 별도로 ' .' 처리.
HEATMAP_DENSITY = " .:-=+*#%@"

DOW_LABELS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")


def render_headline(
    w: TextIO,
    host: HostRow,
    h: Headline,
    since: timedelta,
    width: int,
) -> None:
    """호스트 헤더 + §1 결과를 한 줄짜리 3-bar 와 비율로 출력.

    비례 분할: active/idle-held 를 반올림으로 잡고 나머지를 truly-idle 에
    줘서 합이 항상 width — 마지막 칸이 비어 보이지 않게.
    """
    if not host.hostname:
        print(f"gua  (no host row — daemon hasn't run yet?)  Window: {since}\n", file=w)
    else:
        ctx = host.env_kind
        if host.driver_version:
            ctx = f"{host.env_kind}, driver {host.driver_version}"
        print(f"gua — {host.hostname} ({ctx})  Window: {since}\n", file=w)

    print("§1 Headline", file=w)
    print("  basis: one sample = one GPU card at one daemon tick", file=w)
    print(
        "  rules: active >=10% util; idle-held <10% util with >100 MB process memory",
        file=w,
    )
    if h.samples == 0:
        print("  (no samples in window)", file=w)
        return

    w_a = round(h.active * width)
    w_b = round(h.idle_held * width)
    w_c = max(0, width - w_a - w_b)
    bar = GLYPH_ACTIVE * w_a + GLYPH_IDLE_HELD * w_b + GLYPH_TRULY_IDLE * w_c
    print(f"  {bar}", file=w)
    print(f"  active       {GLYPH_ACTIVE}  {h.active * 100:5.1f}%", file=w)
    print(f"  idle-held    {GLYPH_IDLE_HELD}  {h.idle_held * 100:5.1f}%", file=w)
    print(f"  truly-idle   {GLYPH_TRULY_IDLE}  {h.truly_idle * 100:5.1f}%", file=w)
    print(f"  ({h.samples} samples)", file=w)


def render_idle_capacity(w: TextIO, idle_capacity: IdleCapacity) -> None:
    print(file=w)
    print("§2 Idle capacity", file=w)
    print(
        f"  converted from card-ticks to GPU-hours using {idle_capacity.interval_source}",
        file=w,
    )
    if idle_capacity.samples == 0:
        print("  (no samples in window)", file=w)
        return
    print(
        f"  idle-held: ~{idle_capacity.idle_held_gpu_hours:.2f} GPU-hours, "
        f"~{idle_capacity.idle_held_equiv_gpus:.2f} GPUs equivalently unavailable",
        file=w,
    )
    print(
        f"  truly-idle: ~{idle_capacity.truly_idle_gpu_hours:.2f} GPU-hours, "
        f"~{idle_capacity.truly_idle_equiv_gpus:.2f} GPUs equivalently free",
        file=w,
    )


def render_per_gpu(w: TextIO, rows: list[PerGPU]) -> None:
    print(file=w)
    print("§3 Per-GPU", file=w)
    print("  per-card share of samples in the same three states", file=w)
    if not rows:
        print("  (no GPU cards in window)", file=w)
        return
    for r in rows:
        print(
            f"  {r.uuid:<8}  active {r.active * 100:5.1f}%  "
            f"idle-held {r.idle_held * 100:5.1f}%  "
            f"truly-idle {r.truly_idle * 100:5.1f}%  ({r.samples} samples)",
            file=w,
        )


def render_top_identities(w: TextIO, rows: list[TopIdentity]) -> None:
    print(file=w)
    print("§4 Top identities", file=w)
    print("  one identity counts once per GPU/tick after its processes are summed", file=w)
    if not rows:
        print("  (no processes in window)", file=w)
        return
    print(f"  {'identity':<20} {'gpu-hours':>10}  {'idle-held':>10}  {'samples':>8}", file=w)
    for r in rows:
        print(
            f"  {r.identity:<20} {r.gpu_hours:>10.2f}  {r.idle_held * 100:>9.1f}%  {r.samples:>8}",
            file=w,
        )


def render_heatmap(w: TextIO, cells: list[HeatmapCell]) -> None:
    print(file=w)
    print("§5 Time-of-day heatmap (UTC)", file=w)
    print("  darker means higher active share; blank means no samples", file=w)
    if not cells:
        print("  (no samples in window)", file=w)
        return

    # 7×24 그리드. seen 마스크로 데이터 *있음/없음* 구별 — 빈 셀은 ' .'.
    grid: list[list[float]] = [[0.0] * 24 for _ in range(7)]
    seen: list[list[bool]] = [[False] * 24 for _ in range(7)]
    for c in cells:
        if 0 <= c.dow <= 6 and 0 <= c.hour <= 23:
            grid[c.dow][c.hour] = c.active
            seen[c.dow][c.hour] = True

    # 시간 헤더 (마지막 자릿수만).
    header = "        " + " ".join(str(h % 10) for h in range(24))
    print(header, file=w)
    for dow, label in enumerate(DOW_LABELS):
        cells_str: list[str] = []
        for hour in range(24):
            if not seen[dow][hour]:
                cells_str.append(" ")
                continue
            # 10단계 매핑: density[0]=' ', density[9]='@'. 0% 활성이면
            # density[0] 이 아니라 *데이터는 있지만 활성 0* 임을 살리려면
            # 최소 density[1]='.' 부터 시작. round 후 max(1, ...).
            idx = max(1, round(grid[dow][hour] * (len(HEATMAP_DENSITY) - 1)))
            cells_str.append(HEATMAP_DENSITY[idx])
        print(f"  {label}   " + " ".join(cells_str), file=w)

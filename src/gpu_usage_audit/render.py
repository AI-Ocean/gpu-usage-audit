"""§1~§5 report 렌더링.

색 의존성 회피 — TTY/isatty 토글 없이 *글자 자체* (█/▒/░) 로 세 분류를
시각적으로 구분. 파일 redirect 시에도 출력이 깨끗.

모든 함수가 TextIO 를 받음 — 테스트에서 io.StringIO 로 격리해 검증
가능. Go 의 io.Writer 와 동등 의도.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TextIO

from .model import HostRow
from .report import ActionReport, Headline, HeatmapCell, IdleCapacity, PerGPU, Session, TopIdentity

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


# ── Action report ───────────────────────────────────────────────
#
# "통계" 대신 "조치 리스트" — 잡고 안 쓰는 카드(누가·언제~언제·얼마나)와
# 즉시 배정 가능한 빈 카드만 보여준다. GPU-hours/히트맵 같은 추상치는 뺌.
RULE = "─" * 66


def _fmt_since(td: timedelta) -> str:
    s = int(td.total_seconds())
    if s and s % 86400 == 0:
        return f"{s // 86400}일"
    if s and s % 3600 == 0:
        return f"{s // 3600}시간"
    if s % 60 == 0:
        return f"{s // 60}분"
    return str(td)


def _fmt_ago(delta: timedelta) -> str:
    s = int(delta.total_seconds())
    if s < 90:
        return f"{s}초 전"
    if s < 5400:
        return f"{s // 60}분 전"
    return f"{s // 3600}시간 전"


def _gpu_label(index: int | None, uuid: str) -> str:
    # NVML uuid "GPU-<8hex>-..." → 첫 세그먼트가 관례적 축약형.
    parts = uuid.split("-")
    short = parts[1] if len(parts) > 1 and parts[0] == "GPU" else uuid[:8]
    if index is None:
        return f"GPU {short}"
    core = "" if short == str(index) else f" {short}"
    return f"GPU#{index}{core}"


def _render_session(w: TextIO, s: Session, host: str, now: datetime) -> None:
    label = _gpu_label(s.gpu_index, s.gpu_uuid)
    pad = " " * (len(label) + 3)
    shared = " · 공유카드(util은 카드전체)" if s.shared else ""
    since_end = now - s.end
    if since_end.total_seconds() < 300:
        span = f"{s.start:%m-%d %H:%M} ~ 계속 (마지막 {_fmt_ago(since_end)})"
    else:
        span = f"{s.start:%m-%d %H:%M} ~ {s.end:%m-%d %H:%M}"
    print(
        f"  {label}   {s.owner:<14}  {s.duration_h:.1f}h 점유 · "
        f"util 평균 {s.avg_util:.0f}%{shared} · 최대 {s.peak_mem_mb:,}MB",
        file=w,
    )
    login_note = "" if s.has_login else "  (로그인 세션 밖: 시스템·컨테이너 가능)"
    print(f"{pad}{s.process_name} (pid {s.pid}){login_note}   {span}", file=w)
    idx = s.gpu_index if s.gpu_index is not None else "?"
    if s.has_login:
        action = f"확인·회수:  ssh {host} 'nvidia-smi -i {idx}'  →  kill {s.pid}"
    else:
        action = f"확인:  ssh {host} 'nvidia-smi -i {idx}'  (사용자 작업인지 판단)"
    print(f"{pad}↳ {action}", file=w)
    print(file=w)


def render_action_report(w: TextIO, rep: ActionReport) -> None:
    host = rep.host.hostname or "unknown"
    ctx = rep.host.env_kind or "?"
    meta = f"NVML {rep.interval_label} 샘플 · {rep.total_samples:,} 관측"
    print(f"GPU 낭비 진단 · {host} ({ctx}) · 최근 {_fmt_since(rep.since)}    {meta}", file=w)
    print(file=w)

    if rep.total_samples == 0:
        print("  (관측 구간에 데이터 없음 — 데몬이 아직 안 돌았거나 --since 범위 밖)", file=w)
        return

    print(
        f"{rep.gpu_count}장 중 평균 {rep.idle_equiv:.1f}장이 놀았습니다."
        f"   잡고도 안 쓴 시간 ≈ {rep.held_gpu_hours:.0f} GPU-시간.",
        file=w,
    )
    print(file=w)

    print(f"■ 조치 필요 — 잡고 안 쓰는 카드 ({len(rep.actions)}건)", file=w)
    print(file=w)
    if not rep.actions:
        print("  없음 — 점유된 카드가 모두 실사용 중이거나 비어 있습니다.", file=w)
        print(file=w)
    for s in rep.actions:
        _render_session(w, s, host, rep.now)

    print(f"■ 즉시 가용 — 관측 내내 빈 카드 ({len(rep.free_cards)}장)", file=w)
    print(file=w)
    if rep.free_cards:
        labels = "  ".join(f"GPU#{i}" if i is not None else uuid[:8] for i, uuid in rep.free_cards)
        print(f"  {labels}   →  지금 바로 배정 가능", file=w)
    else:
        print("  없음.", file=w)
    print(file=w)

    print(RULE, file=w)
    print(
        "util = NVML GPU 단위(카드 공유 시 프로세스 귀속 불가) · "
        "소유 = 로그인 사용자 우선, 없으면 프로세스 실 UID",
        file=w,
    )

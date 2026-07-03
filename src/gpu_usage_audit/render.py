"""§1~§5 report 렌더링.

색 의존성 회피 — TTY/isatty 토글 없이 *글자 자체* (█/▒/░) 로 세 분류를
시각적으로 구분. 파일 redirect 시에도 출력이 깨끗.

모든 함수가 TextIO 를 받음 — 테스트에서 io.StringIO 로 격리해 검증
가능. Go 의 io.Writer 와 동등 의도.
"""

from __future__ import annotations

import unicodedata
from datetime import datetime, timedelta
from typing import TextIO

from . import __version__
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
# 마스트헤드 + 요약 + 두 개의 표(조치 필요 / 전체 GPU) + 방법론 푸터.
# 색·외부 의존 없이 box-drawing 표를 직접 그린다. Hangul 등 wide 문자는
# 셀 폭을 2로 세어 정렬을 맞춘다(east_asian_width W/F).
WIDTH = 74
RULE = "─" * WIDTH
_PAST_LIMIT = 8  # 낭비 이력 표에 보여줄 최대 행 (초과분은 "… 외 N건")
_STATE_KO = {"in_use": "사용중", "idle_held": "유휴점유", "empty": "비어있음"}


def _dw(s: str) -> int:
    """터미널 표시 폭 — CJK(wide/fullwidth) 는 2칸."""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def _pad(s: str, width: int, align: str) -> str:
    gap = width - _dw(s)
    if gap <= 0:
        return s
    if align == "r":
        return " " * gap + s
    if align == "c":
        left = gap // 2
        return " " * left + s + " " * (gap - left)
    return s + " " * gap


def _table(
    w: TextIO,
    headers: list[str],
    rows: list[list[str]],
    aligns: list[str],
    indent: str = "  ",
) -> None:
    """box-drawing 표. 숫자열(align 'r')은 헤더를 가운데로(표 관례)."""
    n = len(headers)
    widths = [_dw(headers[i]) for i in range(n)]
    for r in rows:
        for i in range(n):
            widths[i] = max(widths[i], _dw(r[i]))

    def sep(left: str, mid: str, right: str) -> str:
        return indent + left + mid.join("─" * (widths[i] + 2) for i in range(n)) + right

    def line(cells: list[str], al: list[str]) -> str:
        parts = [" " + _pad(cells[i], widths[i], al[i]) + " " for i in range(n)]
        return indent + "│" + "│".join(parts) + "│"

    head_al = ["c" if a == "r" else "l" for a in aligns]
    print(sep("┌", "┬", "┐"), file=w)
    print(line(headers, head_al), file=w)
    print(sep("├", "┼", "┤"), file=w)
    for r in rows:
        print(line(r, aligns), file=w)
    print(sep("└", "┴", "┘"), file=w)


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


def _gpu_num(index: int | None, uuid: str) -> str:
    if index is not None:
        return f"#{index}"
    parts = uuid.split("-")
    return (parts[1] if len(parts) > 1 and parts[0] == "GPU" else uuid)[:6]


def _gpu_label(index: int | None, uuid: str) -> str:
    """상세줄용 — 번호 + uuid 축약."""
    parts = uuid.split("-")
    short = parts[1] if len(parts) > 1 and parts[0] == "GPU" else uuid[:8]
    if index is None:
        return f"GPU {short}"
    return f"GPU#{index} {short}" if short != str(index) else f"GPU#{index}"


def _span(s: Session, now: datetime) -> str:
    since_end = now - s.end
    if since_end.total_seconds() < 300:
        return f"{s.start:%m-%d %H:%M} → 계속 ({_fmt_ago(since_end)})"
    return f"{s.start:%m-%d %H:%M} → {s.end:%m-%d %H:%M}"


def render_action_report(w: TextIO, rep: ActionReport) -> None:
    host = rep.host.hostname or "unknown"
    driver = rep.host.driver_version
    ctx = f"{rep.host.env_kind or '?'}, driver {driver}" if driver else (rep.host.env_kind or "?")
    start = rep.now - rep.since

    # 마스트헤드
    print("═" * WIDTH, file=w)
    print("  GPU 가동·낭비 진단 리포트", file=w)
    print(f"  호스트  {host} ({ctx})", file=w)
    print(
        f"  구간    {start:%Y-%m-%d %H:%M} ~ {rep.now:%m-%d %H:%M} UTC ({_fmt_since(rep.since)})"
        f"   ·   NVML {rep.interval_label} 샘플 · {rep.total_samples:,} 관측",
        file=w,
    )
    print("═" * WIDTH, file=w)
    print(file=w)

    if rep.total_samples == 0:
        print("  관측 구간에 데이터가 없습니다 — 데몬이 아직 안 돌았거나 --since 범위 밖.", file=w)
        return

    # 요약
    print("요약", file=w)
    print(
        f"  실가동 {rep.active * 100:.0f}%   ·   유휴점유 {rep.idle_held * 100:.0f}%"
        f"   ·   완전유휴 {rep.truly_idle * 100:.0f}%",
        file=w,
    )
    print(
        f"  {rep.gpu_count}장 중 평균 {rep.idle_equiv:.1f}장이 놀았습니다. "
        f"잡고도 안 쓴 시간 ≈ {rep.held_gpu_hours:.0f} GPU-시간.",
        file=w,
    )
    print(
        f"  현재 점유 {len(rep.current_actions)}건   ·   "
        f"기간 중 낭비 {len(rep.past_waste)}건   ·   현재 가용 {len(rep.free_cards)}장",
        file=w,
    )
    print(file=w)

    # 조치 필요 — 지금 잡고 안 쓰는 카드 (현재 점유 중, kill 가능)
    print(f"■ 조치 필요 — 지금 잡고 안 쓰는 카드 ({len(rep.current_actions)}건)", file=w)
    print(file=w)
    if not rep.current_actions:
        print("  현재 잡고 안 쓰는 카드 없음.", file=w)
    else:
        _table(
            w,
            ["GPU", "소유자", "프로세스 (PID)", "점유", "평균util", "최대mem"],
            [
                [
                    _gpu_num(s.gpu_index, s.gpu_uuid),
                    s.owner,
                    f"{s.process_name} ({s.pid})",
                    f"{s.duration_h:.1f}h",
                    f"{s.avg_util:.0f}%",
                    f"{s.peak_mem_mb:,}MB",
                ]
                for s in rep.current_actions
            ],
            ["l", "l", "l", "r", "r", "r"],
        )
        print(file=w)
        for s in rep.current_actions:
            note = "" if s.has_login else "  ·  로그인 세션 밖(시스템·컨테이너 가능)"
            idx = s.gpu_index if s.gpu_index is not None else "?"
            print(f"  {_gpu_label(s.gpu_index, s.gpu_uuid)}  ·  {_span(s, rep.now)}{note}", file=w)
            if s.has_login:
                print(
                    f"    ↳ 확인 후 회수:  ssh {host} 'nvidia-smi -i {idx}'  →  kill {s.pid}",
                    file=w,
                )
            else:
                print(
                    f"    ↳ 확인:  ssh {host} 'nvidia-smi -i {idx}'  "
                    f"(사용자 작업인지 판단 후 kill {s.pid})",
                    file=w,
                )
    print(file=w)

    # 기간 중 낭비 이력 — 종료된 유휴점유 (회고·귀속, kill 대상 아님)
    if rep.past_waste:
        shown = rep.past_waste[:_PAST_LIMIT]
        extra = len(rep.past_waste) - len(shown)
        print(f"■ 기간 중 낭비 이력 — 이미 종료됨 ({len(rep.past_waste)}건)", file=w)
        print(file=w)
        _table(
            w,
            ["GPU", "소유자", "프로세스", "점유 기간", "지속", "평균util"],
            [
                [
                    _gpu_num(s.gpu_index, s.gpu_uuid),
                    s.owner,
                    s.process_name,
                    f"{s.start:%m-%d %H:%M}→{s.end:%m-%d %H:%M}",
                    f"{s.duration_h:.1f}h",
                    f"{s.avg_util:.0f}%",
                ]
                for s in shown
            ],
            ["l", "l", "l", "l", "r", "r"],
        )
        if extra:
            print(f"  … 외 {extra}건", file=w)
        print(file=w)

    # 전체 GPU 상태 — 현재(최신 tick) 스냅샷
    print(f"■ 전체 GPU 상태 — 현재 ({rep.gpu_count}장)", file=w)
    print(file=w)
    _table(
        w,
        ["GPU", "상태", "util", "메모리", "소유자", "프로세스"],
        [
            [
                _gpu_num(g.gpu_index, g.gpu_uuid),
                _STATE_KO.get(g.state, g.state),
                f"{g.util}%",
                f"{g.mem_mb:,}MB",
                g.owner or "—",
                g.process_name or "—",
            ]
            for g in rep.gpus
        ],
        ["l", "l", "r", "r", "l", "l"],
    )
    print(file=w)

    if rep.free_cards:
        labels = " ".join(_gpu_num(i, uuid) for i, uuid in rep.free_cards)
        print(f"현재 가용 ({len(rep.free_cards)}장):  {labels}   →  바로 배정 가능", file=w)
        print(file=w)

    # 방법론 푸터
    print(RULE, file=w)
    print("  조치필요=현재 점유 중 · 낭비이력=기간 중 발생했다 종료 · 전체 상태=최신 tick", file=w)
    print("  상태   실가동 util≥10% · 유휴점유 util<10%+메모리>100MB · 완전유휴 그 외", file=w)
    print(
        "  util   NVML GPU 단위 값 · graphics(Xorg 등) 제외 · 소유=로그인 우선, 없으면 실 UID",
        file=w,
    )
    print(f"  생성   {rep.now:%Y-%m-%d %H:%M} UTC · gua {__version__}", file=w)

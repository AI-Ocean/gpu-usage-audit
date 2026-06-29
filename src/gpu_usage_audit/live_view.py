"""`gua top` — 로컬 라이브 GPU 뷰 (work-spec 0032-2). 보드/웹 불필요.

nvtop 식: GPU별 util 스파크라인 + 현재 util/mem + 프로세스 표. 보드 ws 스트림과
*같은 1초 수집 코어*(`tier.collect_util`)를 쓰되, 여긴 로컬 터미널에 렌더한다 →
오픈소스 standalone 가치(예약·웹 없이도 모니터링).

util(1초)과 프로세스(스냅샷, 느림)는 cadence 가 달라 따로 수집한다. 렌더 함수는
순수(buffer+snapshot → 문자열) — 테스트는 그걸 친다. 루프(run_top)는 out/clock 주입.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TextIO

from .live_buffer import LiveUtilBuffer
from .model import ProcSample, Snapshot
from .tier import Tier

# 0(빈칸)~8(꽉찬블록) 9단계. util 0-100 을 이 인덱스로 매핑.
_LEVELS = " ▁▂▃▄▅▆▇█"
_CLEAR = "\033[H\033[2J"  # 커서 홈 + 화면 지움 (프레임마다 재출력)


def sparkline(values: list[int], width: int) -> str:
    """util%(0-100) 리스트의 최근 `width`개를 유니코드 블록 스파크라인으로."""
    recent = values[-width:]
    cells = []
    for v in recent:
        idx = max(0, min(8, round(max(0, min(100, v)) / 100 * 8)))
        cells.append(_LEVELS[idx])
    return "".join(cells).rjust(width)


def _procs_for(snapshot: Snapshot | None, uuid: str) -> list[ProcSample]:
    if snapshot is None:
        return []
    procs = [p for p in snapshot.procs if p.gpu_uuid == uuid and (p.mem_used_mb or 0) > 0]
    return sorted(procs, key=lambda p: p.mem_used_mb or 0, reverse=True)


def render_top(
    buffer: LiveUtilBuffer,
    snapshot: Snapshot | None,
    *,
    spark_width: int = 40,
) -> str:
    """buffer(util 히스토리) + snapshot(이름·총메모리·프로세스) → 터미널 문자열.

    GPU 순서는 snapshot 의 index 순(있으면), 없으면 버퍼 등장 순.
    """
    names: dict[str, str] = {}
    totals: dict[str, int] = {}
    order: list[str] = []
    if snapshot is not None:
        for g in snapshot.gpus:
            names[g.uuid] = g.name or g.uuid
            totals[g.uuid] = g.memory_total_mb or 0
            order.append(g.uuid)
    for uuid in buffer.uuids():
        if uuid not in order:
            order.append(uuid)

    lines: list[str] = ["GPU 라이브 (util 1초)  —  q/Ctrl-C 로 종료", ""]
    for uuid in order:
        samples = buffer.read(uuid)
        latest = samples[-1] if samples else None
        util = latest.util_pct if latest else 0
        used = latest.mem_used_mb if latest else 0
        total = totals.get(uuid, 0)
        name = names.get(uuid, uuid)
        mem = f"{used}/{total}MB" if total else f"{used}MB"
        lines.append(f"{name}  util {util:3d}%  mem {mem}")
        lines.append(f"  [{sparkline([s.util_pct for s in samples], spark_width)}]")
        for p in _procs_for(snapshot, uuid):
            user = p.loginuid_user or "?"
            pname = p.process_name or "?"
            lines.append(f"    {user:<10} pid {p.pid:<7} {pname:<16} {p.mem_used_mb}MB")
        lines.append("")
    return "\n".join(lines)


def run_top(
    tier: Tier,
    *,
    stop: threading.Event,
    out: TextIO,
    interval: float = 1.0,
    snapshot_interval: float = 3.0,
    spark_width: int = 40,
    clock: Callable[[], float] = time.time,
    max_frames: int | None = None,
) -> int:
    """stop 까지 매 interval 프레임 렌더. util 은 매 프레임, 프로세스는 느린 cadence.

    max_frames: None 이면 무한, 정수면 그만큼 그리고 멈춤 — 테스트용.
    """
    buffer = LiveUtilBuffer()
    snapshot: Snapshot | None = None
    next_snapshot = 0.0
    frames = 0
    while not stop.is_set():
        if max_frames is not None and frames >= max_frames:
            break
        ts = clock()
        buffer.append_all(tier.collect_util(ts))
        if ts >= next_snapshot:
            snapshot = tier.collect(datetime.now(UTC))
            next_snapshot = ts + snapshot_interval
        out.write(_CLEAR)
        out.write(render_top(buffer, snapshot, spark_width=spark_width))
        out.write("\n")
        frames += 1
        if stop.wait(interval):
            break
    return frames

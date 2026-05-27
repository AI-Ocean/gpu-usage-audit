"""데몬 루프 — anti-drift 스케줄링 + 시그널 기반 종료.

Go v0.1.0 의 runDaemon 동등. signal.signal 로 SIGINT/SIGTERM 을
threading.Event 에 묶고, sleep 은 event.wait(timeout) 으로 *즉시*
응답 가능하게. Go 의 ctx.Done 채널 + select 패턴과 동일 의도.

데몬 함수 자체는 stop event 와 lookup 을 인자로 받는다 (DI). 시그널
핸들러 설치는 *CLI 측 책임* — 테스트가 데몬을 시그널 없이 격리해
구동할 수 있게.
"""

from __future__ import annotations

import logging
import signal
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TextIO

from .db import start_daemon_run, write_snapshot
from .model import HostMeta
from .summarize import summarize
from .tier import Tier

logger = logging.getLogger(__name__)

UserLookup = Callable[[int], str | None]


def _noop_lookup(_pid: int) -> str | None:
    """기본 lookup — 모든 PID 를 미해결로 반환. 테스트/스캐폴드에 유용."""
    return None


def install_signal_handlers(stop: threading.Event) -> None:
    """SIGINT, SIGTERM 을 받으면 stop 을 set 한다. 호출 1회 (보통 CLI startup).

    Python 의 signal 모듈은 *메인 스레드* 에서만 핸들러 등록 가능 —
    데몬 함수가 별도 스레드에서 돈다면 다른 메커니즘 필요. 우리 데몬은
    foreground process 라 메인 스레드.
    """

    def handler(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def _tick(
    tier: Tier,
    db: sqlite3.Connection,
    host: HostMeta,
    lookup: UserLookup,
    ts: datetime,
    n: int,
    out: TextIO,
    run_id: int,
) -> None:
    """한 틱: tier.collect → loginuid 해석 → 적재 → 한 줄 로그."""
    snap = tier.collect(ts)
    # ProcSample 이 mutable slots — *제자리* 갱신.
    for p in snap.procs:
        if p.loginuid_user is None:
            p.loginuid_user = lookup(p.pid)
    write_snapshot(db, ts, host, snap, run_id=run_id)

    classes = "  ".join(f"{c.uuid}={c.klass.value:<10}" for c in summarize(snap))
    ts_short = ts.strftime("%H:%M:%S.") + f"{ts.microsecond // 1000:03d}"
    print(f"Tick {n}  ts={ts_short}  {classes}", file=out)


def run_daemon(
    *,
    tier: Tier,
    db: sqlite3.Connection,
    host: HostMeta,
    interval: timedelta,
    lookup: UserLookup = _noop_lookup,
    stop: threading.Event | None = None,
    max_ticks: int | None = None,
    out: TextIO | None = None,
) -> int:
    """ctx 캔슬까지 interval 간격으로 한 틱씩 반복. 적재한 틱 총 수 반환.

    anti-drift 스케줄: 다음 타겟을 next + interval 로 잡되, 틱이 너무
    오래 걸려서 이미 지났으면 *catch-up 하지 않고* now 로 점프. NVML/DB
    가 잠시 느려진 뒤 back-to-back 으로 펌프질하지 않게 — 그러면 단기
    idle 비율 계산이 망가지고 DB 가 부풀어 오른다.

    Args:
        max_ticks: None 이면 무한. 정수면 그만큼 돌고 정상 종료 — 테스트용.
    """
    stop = stop or threading.Event()
    out = out or sys.stdout
    interval_s = interval.total_seconds()

    logger.info(
        "daemon started (host=%s, env=%s, driver=%s, interval=%s)",
        host.hostname,
        host.env_kind,
        host.driver_version,
        interval,
    )

    next_at = time.monotonic()
    n = 0
    run_id: int | None = None
    while not stop.is_set():
        if max_ticks is not None and n >= max_ticks:
            break

        if run_id is None:
            run_id = start_daemon_run(db, datetime.now(UTC), interval)

        try:
            _tick(tier, db, host, lookup, datetime.now(UTC), n, out, run_id)
        except Exception:
            logger.exception("tick %d failed; continuing", n)
        n += 1

        next_at += interval_s
        now = time.monotonic()
        if now > next_at:
            logger.warning(
                "tick overran schedule by %.3fs; jumping without catch-up",
                now - next_at,
            )
            next_at = now

        delay = next_at - time.monotonic()
        if delay > 0 and stop.wait(delay):
            logger.info("shutdown signal received during sleep (%d ticks)", n)
            return n

    if stop.is_set():
        logger.info("shutdown signal received, daemon stopping cleanly (%d ticks)", n)
    return n

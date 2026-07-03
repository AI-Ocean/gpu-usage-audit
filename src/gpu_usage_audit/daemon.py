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
from .model import HostMeta, ProcSample, Snapshot
from .summarize import summarize
from .tier import Tier
from .usage_state import UsageStateTracker

logger = logging.getLogger(__name__)

UserLookup = Callable[[int], str | None]
# 틱 후크: local write *이후* 의 부가 작업(예: cloud push). 데몬 모듈은 cloud 를
# 모르고, CLI 가 콜백을 주입한다 — 결합도 분리.
OnTick = Callable[[Snapshot, datetime], None]


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


def resolve_proc_identities(
    procs: list[ProcSample],
    user_lookup: UserLookup,
    name_lookup: UserLookup,
    owner_lookup: UserLookup = _noop_lookup,
) -> None:
    """미해결(None) loginuid_user / owner_user / process_name 을 제자리에서 채운다.

    이미 채워진 항목(FakeTier 가 미리 박은 값)은 건드리지 않는다. daemon
    틱과 cloud sync-once 가 같은 해석 규칙을 공유하도록 한곳에 모음.
    owner_lookup 은 best-effort — 미주입(테스트/데모)이면 owner_user 는 None.
    """
    for p in procs:
        if p.loginuid_user is None:
            p.loginuid_user = user_lookup(p.pid)
        if p.owner_user is None:
            p.owner_user = owner_lookup(p.pid)
        if p.process_name is None:
            p.process_name = name_lookup(p.pid)


def _tick(
    tier: Tier,
    db: sqlite3.Connection,
    host: HostMeta,
    lookup: UserLookup,
    name_lookup: UserLookup,
    owner_lookup: UserLookup,
    ts: datetime,
    n: int,
    out: TextIO,
    run_id: int,
    on_tick: OnTick | None = None,
    usage_tracker: UsageStateTracker | None = None,
) -> None:
    """한 틱: tier.collect → loginuid/process_name 해석 → 적재 → 한 줄 로그.

    on_tick 이 있으면 local write *이후* 호출한다(예: cloud push). 후크 실패는
    이미 커밋된 local write 와 다음 틱을 막지 않는다 — 로그만 남기고 계속.
    """
    snap = tier.collect(ts)
    # ProcSample/GPUSample 이 mutable slots — *제자리* 갱신.
    resolve_proc_identities(snap.procs, lookup, name_lookup, owner_lookup)
    if usage_tracker is not None:
        usage_tracker.apply(snap)
    write_snapshot(db, ts, host, snap, run_id=run_id)

    classes = "  ".join(f"{c.uuid}={c.klass.value:<10}" for c in summarize(snap))
    ts_short = ts.strftime("%H:%M:%S.") + f"{ts.microsecond // 1000:03d}"
    print(f"Tick {n}  ts={ts_short}  {classes}", file=out)

    if on_tick is not None:
        try:
            on_tick(snap, ts)
        except Exception:
            logger.exception("tick %d on_tick hook failed; continuing", n)


def run_daemon(
    *,
    tier: Tier,
    db: sqlite3.Connection,
    host: HostMeta,
    interval: timedelta,
    lookup: UserLookup = _noop_lookup,
    name_lookup: UserLookup = _noop_lookup,
    owner_lookup: UserLookup = _noop_lookup,
    stop: threading.Event | None = None,
    max_ticks: int | None = None,
    out: TextIO | None = None,
    on_tick: OnTick | None = None,
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
    usage_tracker = UsageStateTracker()
    while not stop.is_set():
        if max_ticks is not None and n >= max_ticks:
            break

        if run_id is None:
            run_id = start_daemon_run(db, datetime.now(UTC), interval)

        try:
            _tick(
                tier,
                db,
                host,
                lookup,
                name_lookup,
                owner_lookup,
                datetime.now(UTC),
                n,
                out,
                run_id,
                on_tick,
                usage_tracker,
            )
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

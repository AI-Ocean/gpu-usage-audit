"""데몬 루프 테스트. anti-drift 와 시그널 처리의 의미를 짧은 시나리오로 확인."""

from __future__ import annotations

import io
import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gpu_usage_audit.daemon import run_daemon
from gpu_usage_audit.db import open_db
from gpu_usage_audit.model import HostMeta
from gpu_usage_audit.tier import FakeTier

INTERVAL = timedelta(milliseconds=20)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = open_db(tmp_path / "test.db")
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def host() -> HostMeta:
    return HostMeta(
        hostname="testhost",
        env_kind="bare",
        driver_version="0.0-fake",
        first_seen=datetime(2026, 5, 11, 0, 0, 0, tzinfo=UTC),
    )


def test_run_daemon_runs_max_ticks_and_loads_rows(db: sqlite3.Connection, host: HostMeta) -> None:
    out = io.StringIO()
    n = run_daemon(
        tier=FakeTier(),
        db=db,
        host=host,
        interval=INTERVAL,
        max_ticks=3,
        out=out,
    )
    assert n == 3

    # FakeTier 는 GPU 3개를 매 틱 반환 → 3 ticks * 3 GPUs = 9 gpu_sample 행.
    assert db.execute("SELECT COUNT(*) FROM gpu_sample").fetchone()[0] == 9
    # 한 daemon run 과 host upsert 한 행.
    assert db.execute("SELECT COUNT(*) FROM daemon_run").fetchone()[0] == 1
    assert db.execute("SELECT interval_seconds FROM daemon_run").fetchone()[0] == 0.02
    assert db.execute("SELECT COUNT(DISTINCT run_id) FROM gpu_sample").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM host").fetchone()[0] == 1

    # 콘솔 출력: 3 틱 모두 "Tick N" 줄.
    lines = out.getvalue().strip().splitlines()
    assert len(lines) == 3
    assert all(line.startswith(f"Tick {i}") for i, line in enumerate(lines))


def test_run_daemon_honors_stop_event_immediately(db: sqlite3.Connection, host: HostMeta) -> None:
    # 시작 *전* 에 set 해두면 0 틱.
    stop = threading.Event()
    stop.set()
    n = run_daemon(
        tier=FakeTier(),
        db=db,
        host=host,
        interval=INTERVAL,
        stop=stop,
        out=io.StringIO(),
    )
    assert n == 0
    assert db.execute("SELECT COUNT(*) FROM gpu_sample").fetchone()[0] == 0


def test_run_daemon_lookup_resolves_loginuid(db: sqlite3.Connection, host: HostMeta) -> None:
    # FakeTier 가 PID 1234→alice, 5678→bob 을 *미리* 박아둠. 데몬은 None 인
    # 항목만 lookup 으로 채우는 동선이라, 미리 박힌 두 항목은 *lookup 호출
    # 자체를 안 거침*. PID 9999 만 lookup 으로 들어가는데 table 에 없어 NULL.
    lookup_calls: list[int] = []

    def tracking_lookup(pid: int) -> str | None:
        lookup_calls.append(pid)
        return {9999: "carol"}.get(pid)  # 9999 는 lookup 에서 채움.

    n = run_daemon(
        tier=FakeTier(),
        db=db,
        host=host,
        interval=INTERVAL,
        lookup=tracking_lookup,
        max_ticks=1,
        out=io.StringIO(),
    )
    assert n == 1

    rows = dict(db.execute("SELECT pid, loginuid_user FROM proc_sample").fetchall())
    assert rows.get(1234) == "alice"  # FakeTier 가 박은 값.
    assert rows.get(5678) == "bob"  # FakeTier 가 박은 값.
    assert rows.get(9999) == "carol"  # lookup 으로 채움.

    # 미리 박힌 PID 에는 lookup 호출이 가지 않아야 — 부조화 방지의 핵심.
    assert 1234 not in lookup_calls
    assert 5678 not in lookup_calls
    assert 9999 in lookup_calls


def test_run_daemon_invokes_on_tick_after_local_write(
    db: sqlite3.Connection, host: HostMeta
) -> None:
    # on_tick 은 매 틱 local write *이후* 호출된다(cloud push 가 얹히는 자리).
    calls: list[int] = []

    def on_tick(snap, _ts) -> None:
        # 콜백 시점엔 이미 이번 틱이 DB 에 기록돼 있어야 한다.
        calls.append(db.execute("SELECT COUNT(*) FROM gpu_sample").fetchone()[0])
        assert len(snap.gpus) == 3  # FakeTier 의 스냅샷이 그대로 전달된다.

    n = run_daemon(
        tier=FakeTier(),
        db=db,
        host=host,
        interval=INTERVAL,
        max_ticks=3,
        out=io.StringIO(),
        on_tick=on_tick,
    )
    assert n == 3
    # 매 틱 호출되고, 호출 시점의 누적 행 수는 3, 6, 9 (틱당 3 GPU).
    assert calls == [3, 6, 9]


def test_run_daemon_continues_when_on_tick_raises(db: sqlite3.Connection, host: HostMeta) -> None:
    # on_tick(예: cloud push) 실패는 local write 와 다음 틱을 막지 않는다.
    attempts: list[int] = []

    def boom(_snap, _ts) -> None:
        attempts.append(1)
        raise RuntimeError("cloud push failed")

    n = run_daemon(
        tier=FakeTier(),
        db=db,
        host=host,
        interval=INTERVAL,
        max_ticks=3,
        out=io.StringIO(),
        on_tick=boom,
    )
    assert n == 3
    assert len(attempts) == 3  # 매 틱 호출(예외에도 멈추지 않음).
    # local write 는 전부 보존: 3 틱 * 3 GPU = 9 행.
    assert db.execute("SELECT COUNT(*) FROM gpu_sample").fetchone()[0] == 9

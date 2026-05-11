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
    # host upsert 한 행.
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

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
    # FakeTier 가 GPU-0 의 pid 1234 / GPU-1 의 5678, 9999 를 만듦.
    table = {1234: "alice", 5678: "bob"}  # 9999 는 의도적으로 누락
    n = run_daemon(
        tier=FakeTier(),
        db=db,
        host=host,
        interval=INTERVAL,
        lookup=table.get,
        max_ticks=1,
        out=io.StringIO(),
    )
    assert n == 1

    rows = dict(db.execute("SELECT pid, loginuid_user FROM proc_sample").fetchall())
    assert rows.get(1234) == "alice"
    assert rows.get(5678) == "bob"
    assert rows.get(9999) is None  # 미해결 → NULL

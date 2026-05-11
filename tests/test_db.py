"""DB 계층 (open_db + upsert_host + write_snapshot) 테스트.

Go v0.1.0 의 WAL/인덱스 검증 + 트랜잭션 동등성 검증을 옮겨옴.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gpu_usage_audit.db import open_db, write_snapshot
from gpu_usage_audit.model import GPUSample, HostMeta, ProcSample, Snapshot


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """tmp 디렉토리에 새 DB. 매 테스트 격리. teardown 에서 close."""
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
        driver_version="999.99-test",
        first_seen=datetime(2026, 5, 11, 0, 0, 0, tzinfo=UTC),
    )


def test_open_db_enables_wal_and_creates_indexes(db: sqlite3.Connection) -> None:
    mode = db.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"

    idx = {
        row[0]
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='index' ORDER BY name")
    }
    assert {"idx_gpu_sample_uuid_ts", "idx_proc_sample_uuid_ts"} <= idx


def test_open_db_creates_three_tables(db: sqlite3.Connection) -> None:
    tables = {
        row[0]
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    }
    assert {"host", "gpu_sample", "proc_sample"} <= tables


def test_write_snapshot_inserts_rows(db: sqlite3.Connection, host: HostMeta) -> None:
    ts = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)
    snap = Snapshot(
        gpus=[GPUSample(uuid="GPU-0", util_pct=80), GPUSample(uuid="GPU-1", util_pct=2)],
        procs=[
            ProcSample(gpu_uuid="GPU-0", pid=100, mem_used_mb=70000, loginuid_user="alice"),
            ProcSample(gpu_uuid="GPU-1", pid=200, mem_used_mb=8000, loginuid_user=None),
        ],
    )
    write_snapshot(db, ts, host, snap)

    assert db.execute("SELECT COUNT(*) FROM gpu_sample").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM proc_sample").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM host").fetchone()[0] == 1

    # None → SQL NULL 매핑 확인.
    null_count = db.execute(
        "SELECT COUNT(*) FROM proc_sample WHERE loginuid_user IS NULL"
    ).fetchone()[0]
    assert null_count == 1


def test_host_upsert_single_row(db: sqlite3.Connection, host: HostMeta) -> None:
    # 두 틱 적재 → host 는 *한 행* 만 (last_seen 만 갱신).
    ts1 = datetime(2026, 5, 11, 0, 0, 0, tzinfo=UTC)
    ts2 = datetime(2026, 5, 11, 0, 1, 0, tzinfo=UTC)
    snap = Snapshot(gpus=[GPUSample(uuid="GPU-0", util_pct=0)])
    write_snapshot(db, ts1, host, snap)
    write_snapshot(db, ts2, host, snap)

    rows = db.execute("SELECT hostname, first_seen, last_seen FROM host").fetchall()
    assert len(rows) == 1
    hostname, first_seen, last_seen = rows[0]
    assert hostname == "testhost"
    # first_seen 은 immutable, last_seen 은 ts2 까지 진행.
    assert first_seen == host.first_seen.isoformat()
    assert last_seen == ts2.isoformat()


def test_write_snapshot_atomicity_on_empty_lists(db: sqlite3.Connection, host: HostMeta) -> None:
    # GPU 0개, proc 0개 Snapshot 도 *host 만* 적재되며 에러 없음
    # (데몬 startup 직후 NVML 응답 비어있는 케이스 방어).
    ts = datetime(2026, 5, 11, 0, 0, 0, tzinfo=UTC)
    write_snapshot(db, ts, host, Snapshot())

    assert db.execute("SELECT COUNT(*) FROM gpu_sample").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM proc_sample").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM host").fetchone()[0] == 1

"""DB 계층 (open_db + upsert_host + write_snapshot) 테스트.

Go v0.1.0 의 WAL/인덱스 검증 + 트랜잭션 동등성 검증을 옮겨옴.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gpu_usage_audit.db import open_db, start_daemon_run, upsert_gpu_device, write_snapshot
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


def test_open_db_creates_runtime_tables(db: sqlite3.Connection) -> None:
    tables = {
        row[0]
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    }
    assert {"host", "daemon_run", "gpu_sample", "proc_sample"} <= tables


def test_start_daemon_run_and_write_snapshot_store_run_id(
    db: sqlite3.Connection,
    host: HostMeta,
) -> None:
    run_id = start_daemon_run(
        db,
        datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC),
        timedelta(seconds=17),
    )
    write_snapshot(
        db,
        datetime(2026, 5, 11, 12, 0, 1, tzinfo=UTC),
        host,
        Snapshot(
            gpus=[GPUSample(uuid="GPU-0", util_pct=2)],
            procs=[ProcSample(gpu_uuid="GPU-0", pid=100, mem_used_mb=70000)],
        ),
        run_id=run_id,
    )

    assert db.execute("SELECT interval_seconds FROM daemon_run").fetchone()[0] == 17.0
    assert db.execute("SELECT run_id FROM gpu_sample").fetchone()[0] == run_id
    assert db.execute("SELECT run_id FROM proc_sample").fetchone()[0] == run_id


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


def test_write_snapshot_stores_usage_state_and_nullable_process_memory(
    db: sqlite3.Connection,
    host: HostMeta,
) -> None:
    ts = datetime(2026, 6, 19, 4, 0, 0, tzinfo=UTC)
    snap = Snapshot(
        gpus=[GPUSample(uuid="GPU-0", util_pct=0, usage_state="idle_held")],
        procs=[
            ProcSample(
                gpu_uuid="GPU-0",
                pid=100,
                mem_used_mb=None,
                process_type="compute",
            ),
            ProcSample(
                gpu_uuid="GPU-0",
                pid=200,
                mem_used_mb=128,
                process_type="graphics",
            ),
        ],
    )

    write_snapshot(db, ts, host, snap)

    assert db.execute("SELECT usage_state FROM gpu_sample").fetchone()[0] == "idle_held"
    rows = db.execute(
        "SELECT pid, mem_used_mb, process_type FROM proc_sample ORDER BY pid"
    ).fetchall()
    assert rows == [(100, None, "compute"), (200, 128, "graphics")]


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


def _enriched_gpu(uuid: str = "GPU-0", *, index: int = 0, used: int = 18000) -> GPUSample:
    return GPUSample(
        uuid=uuid,
        util_pct=3,
        index=index,
        name="NVIDIA RTX A6000",
        memory_total_mb=49140,
        memory_used_mb=used,
        temperature_c=54,
        power_w=72,
    )


def test_write_snapshot_stores_enriched_metric_columns(
    db: sqlite3.Connection,
    host: HostMeta,
) -> None:
    ts = datetime(2026, 6, 17, 4, 0, 0, tzinfo=UTC)
    snap = Snapshot(
        gpus=[_enriched_gpu()],
        procs=[
            ProcSample(
                gpu_uuid="GPU-0",
                pid=100,
                mem_used_mb=17800,
                loginuid_user="lee",
                gpu_index=0,
                process_name="python",
            )
        ],
    )
    write_snapshot(db, ts, host, snap)

    gpu_row = db.execute(
        "SELECT gpu_index, memory_used_mb, temperature_c, power_w FROM gpu_sample"
    ).fetchone()
    assert gpu_row == (0, 18000, 54, 72)
    proc_row = db.execute(
        "SELECT gpu_index, process_name, process_type FROM proc_sample"
    ).fetchone()
    assert proc_row == (0, "python", "compute")


def test_write_snapshot_upserts_gpu_device_identity(
    db: sqlite3.Connection,
    host: HostMeta,
) -> None:
    ts1 = datetime(2026, 6, 17, 4, 0, 0, tzinfo=UTC)
    ts2 = datetime(2026, 6, 17, 4, 0, 30, tzinfo=UTC)
    write_snapshot(db, ts1, host, Snapshot(gpus=[_enriched_gpu(used=10000)]))
    write_snapshot(db, ts2, host, Snapshot(gpus=[_enriched_gpu(used=20000)]))

    rows = db.execute(
        "SELECT gpu_uuid, name, memory_total_mb, first_seen, last_seen FROM gpu_device"
    ).fetchall()
    assert len(rows) == 1
    uuid, name, total, first_seen, last_seen = rows[0]
    assert (uuid, name, total) == ("GPU-0", "NVIDIA RTX A6000", 49140)
    # first_seen 은 immutable, last_seen 은 두 번째 틱까지 진행.
    assert first_seen == ts1.isoformat()
    assert last_seen == ts2.isoformat()
    # gpu_sample 은 두 틱 모두 append.
    assert db.execute("SELECT COUNT(*) FROM gpu_sample").fetchone()[0] == 2


def test_upsert_gpu_device_skips_unenriched_sample(
    db: sqlite3.Connection,
) -> None:
    seen = datetime(2026, 6, 17, 4, 0, 0, tzinfo=UTC)
    # name/memory_total 없는 sample 은 device 행을 만들지 않는다.
    with db:
        upsert_gpu_device(db, GPUSample(uuid="GPU-0", util_pct=0), seen)
    assert db.execute("SELECT COUNT(*) FROM gpu_device").fetchone()[0] == 0


def test_migrate_schema_upgrades_legacy_db_in_place(tmp_path: Path) -> None:
    # v1.0 모양의 DB 를 직접 만들고, open_db 가 새 컬럼/테이블을 무손상으로 추가하는지.
    legacy = tmp_path / "legacy.db"
    raw = sqlite3.connect(legacy)
    raw.executescript(
        """
        CREATE TABLE host (hostname TEXT, env_kind TEXT, driver_version TEXT,
            first_seen DATETIME, last_seen DATETIME);
        CREATE TABLE daemon_run (id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at DATETIME, interval_seconds REAL);
        CREATE TABLE gpu_sample (ts DATETIME, gpu_uuid TEXT, util_pct INTEGER);
        CREATE TABLE proc_sample (ts DATETIME, gpu_uuid TEXT, pid INTEGER,
            mem_used_mb INTEGER NOT NULL, loginuid_user TEXT);
        INSERT INTO gpu_sample(ts, gpu_uuid, util_pct) VALUES('2026-01-01T00:00:00+00:00','GPU-0',5);
        """
    )
    raw.commit()
    raw.close()

    conn = open_db(legacy)
    try:
        gpu_cols = {row[1] for row in conn.execute("PRAGMA table_info(gpu_sample)")}
        assert {"gpu_index", "memory_used_mb", "temperature_c", "power_w", "run_id"} <= gpu_cols
        proc_cols = {row[1] for row in conn.execute("PRAGMA table_info(proc_sample)")}
        assert {"gpu_index", "process_name", "process_type", "run_id"} <= proc_cols
        proc_info = {row[1]: row for row in conn.execute("PRAGMA table_info(proc_sample)")}
        assert proc_info["mem_used_mb"][3] == 0
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "gpu_device" in tables
        # 기존 데이터 보존 + nullable memory insert 가능.
        assert conn.execute("SELECT util_pct FROM gpu_sample").fetchone()[0] == 5
        write_snapshot(
            conn,
            datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
            HostMeta("h", "bare", "d", datetime(2026, 1, 1, tzinfo=UTC)),
            Snapshot(
                gpus=[GPUSample(uuid="GPU-0", util_pct=0)],
                procs=[ProcSample(gpu_uuid="GPU-0", pid=10, mem_used_mb=None)],
            ),
        )
        row = conn.execute(
            "SELECT mem_used_mb, process_type FROM proc_sample WHERE pid=10"
        ).fetchone()
        assert row == (None, "compute")
    finally:
        conn.close()


def test_write_snapshot_atomicity_on_empty_lists(db: sqlite3.Connection, host: HostMeta) -> None:
    # GPU 0개, proc 0개 Snapshot 도 *host 만* 적재되며 에러 없음
    # (데몬 startup 직후 NVML 응답 비어있는 케이스 방어).
    ts = datetime(2026, 5, 11, 0, 0, 0, tzinfo=UTC)
    write_snapshot(db, ts, host, Snapshot())

    assert db.execute("SELECT COUNT(*) FROM gpu_sample").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM proc_sample").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM host").fetchone()[0] == 1

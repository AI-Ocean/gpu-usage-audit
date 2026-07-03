"""SQLite 영속화 계층 — Go v0.1.0 의 OpenDB / UpsertHost / WriteSnapshot 동등.

설계 결정:
- stdlib `sqlite3` 만 사용 (cgo-free 효과 동일, 의존성 0).
- `datetime` 직렬화는 *우리가 직접* `isoformat()` 으로 한다. Python 3.12
  부터 sqlite3 의 자동 datetime adapter 가 deprecation 경고를 띄우는데,
  그것에 의존하지 않는 게 안전 + 명시적.
- 한 connection = 한 호출자가 들고 다님. 데몬은 lifetime 동안 유지,
  report 는 cmd 끝에 닫음. WAL pragma 는 DB 파일에 persistent,
  busy_timeout 은 connection 단위라 매 open_db 마다 박는다.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .model import GPUSample, HostMeta, Snapshot

# (gpu_uuid, ts) 컬럼 순서: report 쿼리가 *카드 단위* 로 시간 범위를
# 자르는 패턴이라 uuid 가 leading column.
SCHEMA = """
CREATE TABLE IF NOT EXISTS host (
    hostname       TEXT NOT NULL,
    env_kind       TEXT NOT NULL,
    driver_version TEXT,
    first_seen     DATETIME NOT NULL,
    last_seen      DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS daemon_run (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at       DATETIME NOT NULL,
    interval_seconds REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS gpu_sample (
    ts        DATETIME NOT NULL,
    gpu_uuid  TEXT     NOT NULL,
    util_pct  INTEGER  NOT NULL,
    run_id    INTEGER REFERENCES daemon_run(id),
    usage_state TEXT
);

CREATE TABLE IF NOT EXISTS proc_sample (
    ts            DATETIME NOT NULL,
    gpu_uuid      TEXT     NOT NULL,
    pid           INTEGER  NOT NULL,
    mem_used_mb   INTEGER,
    loginuid_user TEXT,
    owner_user    TEXT,
    run_id        INTEGER REFERENCES daemon_run(id),
    gpu_index     INTEGER,
    process_name  TEXT,
    process_type  TEXT NOT NULL DEFAULT 'compute'
);

-- gpu_device: device 정체성을 시변 metric 에서 분리해 정규화 (v1.1).
-- 한 GPU(UUID)당 한 행, 매 틱 upsert. local DB 는 단일 host 라 host_id 불필요.
CREATE TABLE IF NOT EXISTS gpu_device (
    gpu_uuid        TEXT     NOT NULL,
    name            TEXT     NOT NULL,
    memory_total_mb INTEGER  NOT NULL,
    first_seen      DATETIME NOT NULL,
    last_seen       DATETIME NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gpu_sample_uuid_ts  ON gpu_sample(gpu_uuid, ts);
CREATE INDEX IF NOT EXISTS idx_proc_sample_uuid_ts ON proc_sample(gpu_uuid, ts);
CREATE UNIQUE INDEX IF NOT EXISTS idx_gpu_device_uuid ON gpu_device(gpu_uuid);
"""


def _ts(dt: datetime) -> str:
    """datetime → SQLite 에 적재할 문자열. isoformat 으로 통일.

    `WHERE ts >= ?` 비교는 lex 정렬 = chronological 정렬 (ISO 8601 의
    성질). cutoff 도 같은 함수를 거치게 해서 직렬화 형식 불일치를 차단.
    """
    return dt.isoformat()


def open_db(path: str | Path) -> sqlite3.Connection:
    """SQLite 파일을 열고 PRAGMA + 스키마를 적용한다.

    PRAGMA 는 *스키마 적용 전* 에 박는다. journal_mode=WAL 은
    fetchone() 으로 결과 확인 — 드라이버가 모드 변경을 조용히 삼키는
    케이스를 표면화.
    """
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level="DEFERRED")
    # WAL 결과 확인 — 실패 시 명시적 에러.
    cur = conn.execute("PRAGMA journal_mode=WAL")
    row = cur.fetchone()
    mode = row[0] if row else ""
    if mode != "wal":
        conn.close()
        raise RuntimeError(f"expected journal_mode=wal, got {mode!r}")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    _migrate_schema(conn)
    return conn


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Apply additive migrations for DBs created before later columns existed.

    All migrations are additive nullable columns, so legacy rows keep their
    values and `gua report` (which reads only the original columns) is
    unaffected. `gpu_device` is created by SCHEMA's CREATE TABLE IF NOT EXISTS.
    """
    _ensure_column(conn, "gpu_sample", "run_id", "INTEGER")
    _ensure_column(conn, "proc_sample", "run_id", "INTEGER")
    # v1.1 cloud-sync enrichment.
    _ensure_column(conn, "gpu_sample", "gpu_index", "INTEGER")
    _ensure_column(conn, "gpu_sample", "memory_used_mb", "INTEGER")
    _ensure_column(conn, "gpu_sample", "temperature_c", "INTEGER")
    _ensure_column(conn, "gpu_sample", "power_w", "INTEGER")
    _ensure_column(conn, "gpu_sample", "usage_state", "TEXT")
    _rebuild_proc_sample_if_needed(conn)
    _ensure_column(conn, "proc_sample", "gpu_index", "INTEGER")
    _ensure_column(conn, "proc_sample", "process_name", "TEXT")
    _ensure_column(conn, "proc_sample", "process_type", "TEXT NOT NULL DEFAULT 'compute'")
    # 프로세스 실 uid 소유자 (loginuid 미설정 프로세스의 소유자 폴백).
    _ensure_column(conn, "proc_sample", "owner_user", "TEXT")


def _table_columns(conn: sqlite3.Connection, table: str) -> dict[str, tuple[Any, ...]]:
    return {row[1]: row for row in conn.execute(f"PRAGMA table_info({table})")}


def _rebuild_proc_sample_if_needed(conn: sqlite3.Connection) -> None:
    columns = _table_columns(conn, "proc_sample")
    mem_col = columns.get("mem_used_mb")
    if mem_col is None:
        return
    mem_not_null = bool(mem_col[3])
    if not mem_not_null and "process_type" in columns:
        return

    def existing_or_null(column: str) -> str:
        return column if column in columns else "NULL"

    process_type_expr = (
        "COALESCE(process_type, 'compute')" if "process_type" in columns else "'compute'"
    )
    with conn:
        conn.execute("DROP INDEX IF EXISTS idx_proc_sample_uuid_ts")
        conn.execute("ALTER TABLE proc_sample RENAME TO proc_sample_legacy")
        conn.execute(
            """
            CREATE TABLE proc_sample (
                ts            DATETIME NOT NULL,
                gpu_uuid      TEXT     NOT NULL,
                pid           INTEGER  NOT NULL,
                mem_used_mb   INTEGER,
                loginuid_user TEXT,
                run_id        INTEGER REFERENCES daemon_run(id),
                gpu_index     INTEGER,
                process_name  TEXT,
                process_type  TEXT NOT NULL DEFAULT 'compute'
            )
            """
        )
        conn.execute(
            "INSERT INTO proc_sample"
            "(ts, gpu_uuid, pid, mem_used_mb, loginuid_user, run_id, "
            "gpu_index, process_name, process_type) "
            "SELECT ts, gpu_uuid, pid, mem_used_mb, loginuid_user, "
            f"{existing_or_null('run_id')}, {existing_or_null('gpu_index')}, "
            f"{existing_or_null('process_name')}, {process_type_expr} "
            "FROM proc_sample_legacy"
        )
        conn.execute("DROP TABLE proc_sample_legacy")
        conn.execute("CREATE INDEX idx_proc_sample_uuid_ts ON proc_sample(gpu_uuid, ts)")


def _ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def start_daemon_run(
    conn: sqlite3.Connection,
    started_at: datetime,
    interval: timedelta,
) -> int:
    """Record one daemon run and return its row id for subsequent samples."""
    cur = conn.execute(
        "INSERT INTO daemon_run(started_at, interval_seconds) VALUES(?, ?)",
        (_ts(started_at), interval.total_seconds()),
    )
    conn.commit()
    run_id = cur.lastrowid
    if run_id is None:
        raise RuntimeError("failed to create daemon_run row")
    return run_id


def upsert_host(conn: sqlite3.Connection, host: HostMeta, last_seen: datetime) -> None:
    """단일 row 의 호스트 메타 유지. UPDATE → 0 영향이면 INSERT 패턴.

    schema 에 hostname UNIQUE 제약이 없어서 ON CONFLICT 를 못 쓰는 단순
    시나리오. 갱신 정책:
        env_kind, driver_version, last_seen → 매 틱 갱신
        hostname, first_seen                → 한 번 박히면 immutable
    """
    cur = conn.execute(
        "UPDATE host SET env_kind=?, driver_version=?, last_seen=? WHERE hostname=?",
        (host.env_kind, host.driver_version, _ts(last_seen), host.hostname),
    )
    if cur.rowcount > 0:
        return
    conn.execute(
        "INSERT INTO host(hostname, env_kind, driver_version, first_seen, last_seen) "
        "VALUES(?,?,?,?,?)",
        (
            host.hostname,
            host.env_kind,
            host.driver_version,
            _ts(host.first_seen),
            _ts(last_seen),
        ),
    )


def upsert_gpu_device(conn: sqlite3.Connection, sample: GPUSample, seen: datetime) -> None:
    """GPU device 정체성(name/memory_total)을 한 행으로 유지. first_seen 보존.

    name/memory_total 이 없는 (enrich 안 된) sample 은 건너뛴다 — local-only
    daemon 이 NVML 확장 전 데이터를 보내거나 fake 가 식별을 안 채운 경우.
    """
    if sample.name is None or sample.memory_total_mb is None:
        return
    seen_str = _ts(seen)
    conn.execute(
        "INSERT INTO gpu_device(gpu_uuid, name, memory_total_mb, first_seen, last_seen) "
        "VALUES(?,?,?,?,?) "
        "ON CONFLICT(gpu_uuid) DO UPDATE SET "
        "name=excluded.name, memory_total_mb=excluded.memory_total_mb, last_seen=excluded.last_seen",
        (sample.uuid, sample.name, sample.memory_total_mb, seen_str, seen_str),
    )


def write_snapshot(
    conn: sqlite3.Connection,
    ts: datetime,
    host: HostMeta,
    snap: Snapshot,
    *,
    run_id: int | None = None,
) -> None:
    """한 틱의 Snapshot + 호스트 메타를 *단일 트랜잭션* 으로 적재한다.

    host 까지 같이 묶는 이유: 한 ts 의 모든 사실이 *원자적으로* 들어가야
    일관성이 유지됨. host 만 따로 커밋되고 gpu_sample 실패 같은 부분
    상태가 만들어지지 않게.

    `with conn:` 컨텍스트가 자동 commit (예외 시 rollback). sqlite3
    모듈의 표준 트랜잭션 패턴.

    v1.1: device 정체성은 gpu_device 로 upsert, 시변 metric(util/memory_used/
    temperature/power/index)은 gpu_sample, process_name/gpu_index 는 proc_sample.
    """
    ts_str = _ts(ts)
    with conn:
        upsert_host(conn, host, ts)
        for g in snap.gpus:
            upsert_gpu_device(conn, g, ts)
        if snap.gpus:
            conn.executemany(
                "INSERT INTO gpu_sample"
                "(ts, gpu_uuid, util_pct, gpu_index, memory_used_mb, temperature_c, "
                "power_w, run_id, usage_state) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    (
                        ts_str,
                        g.uuid,
                        g.util_pct,
                        g.index,
                        g.memory_used_mb,
                        g.temperature_c,
                        g.power_w,
                        run_id,
                        g.usage_state,
                    )
                    for g in snap.gpus
                ],
            )
        if snap.procs:
            conn.executemany(
                "INSERT INTO proc_sample"
                "(ts, gpu_uuid, pid, mem_used_mb, loginuid_user, owner_user, gpu_index, "
                "process_name, run_id, process_type) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        ts_str,
                        p.gpu_uuid,
                        p.pid,
                        p.mem_used_mb,
                        p.loginuid_user,
                        p.owner_user,
                        p.gpu_index,
                        p.process_name,
                        run_id,
                        p.process_type,
                    )
                    for p in snap.procs
                ],
            )

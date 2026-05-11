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
from datetime import datetime
from pathlib import Path

from .model import HostMeta, Snapshot

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

CREATE TABLE IF NOT EXISTS gpu_sample (
    ts        DATETIME NOT NULL,
    gpu_uuid  TEXT     NOT NULL,
    util_pct  INTEGER  NOT NULL
);

CREATE TABLE IF NOT EXISTS proc_sample (
    ts            DATETIME NOT NULL,
    gpu_uuid      TEXT     NOT NULL,
    pid           INTEGER  NOT NULL,
    mem_used_mb   INTEGER  NOT NULL,
    loginuid_user TEXT
);

CREATE INDEX IF NOT EXISTS idx_gpu_sample_uuid_ts  ON gpu_sample(gpu_uuid, ts);
CREATE INDEX IF NOT EXISTS idx_proc_sample_uuid_ts ON proc_sample(gpu_uuid, ts);
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
    return conn


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


def write_snapshot(
    conn: sqlite3.Connection,
    ts: datetime,
    host: HostMeta,
    snap: Snapshot,
) -> None:
    """한 틱의 Snapshot + 호스트 메타를 *단일 트랜잭션* 으로 적재한다.

    host 까지 같이 묶는 이유: 한 ts 의 모든 사실이 *원자적으로* 들어가야
    일관성이 유지됨. host 만 따로 커밋되고 gpu_sample 실패 같은 부분
    상태가 만들어지지 않게.

    `with conn:` 컨텍스트가 자동 commit (예외 시 rollback). sqlite3
    모듈의 표준 트랜잭션 패턴.
    """
    ts_str = _ts(ts)
    with conn:
        upsert_host(conn, host, ts)
        if snap.gpus:
            conn.executemany(
                "INSERT INTO gpu_sample(ts, gpu_uuid, util_pct) VALUES(?,?,?)",
                [(ts_str, g.uuid, g.util_pct) for g in snap.gpus],
            )
        if snap.procs:
            conn.executemany(
                "INSERT INTO proc_sample(ts, gpu_uuid, pid, mem_used_mb, loginuid_user) "
                "VALUES(?,?,?,?,?)",
                [(ts_str, p.gpu_uuid, p.pid, p.mem_used_mb, p.loginuid_user) for p in snap.procs],
            )

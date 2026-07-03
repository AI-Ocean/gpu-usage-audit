"""§1~§5 report 계산. Go v0.1.0 의 Load* 함수 6개 동등 — SQL 문구도 그대로.

각 함수가 cutoff (datetime) 을 받아 *그 이후* 의 행만 본다. 직렬화는
db._ts 와 일치 — 같은 isoformat 을 통해 lex 비교가 chronological 이
되게.

분류 룰은 Go 와 SQL 모두에 *두 번 박혀* 있음:
- Python Classify (classify.py): 메모리 합산 후 Go 식 분기.
- SQL CASE: 같은 분기를 AVG(CASE) 로 fraction 산출.
같은 룰을 *두 곳* 에 두는 비용을 감수 — DB 측 집계가 한 번에 끝나고
report 가 빠르다. 두 룰이 어긋나지 않는지는 통합 테스트가 검증.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import groupby, pairwise
from typing import Any

from .db import _ts
from .model import HostRow

LEGACY_INTERVAL_FALLBACK = timedelta(seconds=30)


def load_host(conn: sqlite3.Connection) -> HostRow:
    """단일 host row 를 읽는다. row 없으면 *빈* HostRow — 헤더가 "host
    row 없음" 분기를 가짐. driver_version 의 NULL 은 빈 문자열로."""
    cur = conn.execute("SELECT hostname, env_kind, driver_version FROM host LIMIT 1")
    row = cur.fetchone()
    if row is None:
        return HostRow()
    return HostRow(hostname=row[0], env_kind=row[1], driver_version=row[2] or "")


# ── §1 Headline ─────────────────────────────────────────────────
#
# 윈도우 [cutoff, ∞) 안의 모든 gpu_sample 을 (gpu, ts) 단위로 그루핑하면서,
# 같은 (gpu, ts) 의 proc_sample 메모리를 LEFT JOIN + SUM 으로 합쳐 한 줄로.
# Python 의 summarize() 가 *런타임 메모리* 에서 했던 일을 SQL 이 *DB 시점*
# 에 한 셈.
#
# LEFT JOIN 이 핵심: 프로세스 없는 카드도 (proc_mem_mb=0 으로) 살아남아야
# truly-idle 로 집계됨. INNER JOIN 이었다면 통째로 사라짐.
HEADLINE_QUERY = """
WITH s AS (
    SELECT gs.gpu_uuid, gs.ts, gs.run_id, gs.util_pct,
           COALESCE(SUM(COALESCE(ps.mem_used_mb, 0)), 0) AS proc_mem_mb
    FROM gpu_sample gs
    LEFT JOIN proc_sample ps
        ON ps.gpu_uuid = gs.gpu_uuid
        AND ps.ts = gs.ts
        AND (ps.run_id = gs.run_id OR (ps.run_id IS NULL AND gs.run_id IS NULL))
    WHERE gs.ts >= ?
    GROUP BY gs.gpu_uuid, gs.ts, gs.run_id
)
SELECT
    AVG(CASE WHEN util_pct >= 10                          THEN 1.0 ELSE 0.0 END) AS active,
    AVG(CASE WHEN util_pct <  10 AND proc_mem_mb >  100   THEN 1.0 ELSE 0.0 END) AS idle_held,
    AVG(CASE WHEN util_pct <  10 AND proc_mem_mb <= 100   THEN 1.0 ELSE 0.0 END) AS truly_idle,
    COUNT(*)                                                                     AS samples
FROM s
"""


@dataclass(slots=True)
class Headline:
    """§1 결과. 0 샘플 윈도우는 samples==0 으로 표현 — 분수 값을 0 으로만
    보면 "0% active" 인지 "데이터 없음" 인지 구별이 안 됨."""

    active: float = 0.0
    idle_held: float = 0.0
    truly_idle: float = 0.0
    samples: int = 0


def load_headline(conn: sqlite3.Connection, cutoff: datetime) -> Headline:
    row = conn.execute(HEADLINE_QUERY, (_ts(cutoff),)).fetchone()
    if row is None:
        return Headline()
    active, idle_held, truly_idle, samples = row
    return Headline(
        active=active or 0.0,
        idle_held=idle_held or 0.0,
        truly_idle=truly_idle or 0.0,
        samples=samples,
    )


# ── §2 Idle capacity ─────────────────────────────────────────────
#
# low-util 틱 수 × interval(초) / 3600 = GPU-시간.
# idle-held 와 truly-idle 을 분리한다. 둘 다 util<10 이지만 의미가 다르다:
# idle-held 는 프로세스 메모리가 카드를 잡고 있어 다른 사용자가 쓰기 어렵고,
# truly-idle 은 실제로 비어 있는 용량이다.
# equiv_gpus = 상태 비율 × 카드 수. "8장 중 3.2장이 해당 상태였다" 식.
# 카드 수는 *gpu_sample 에서 distinct* 로 추론 — v2 는 별도 gpu 인벤토리
# 테이블이 없음 (단순화). 새 DB 는 daemon_run.interval_seconds 를 샘플별로
# 조인해 GPU-hours 를 계산한다. run_id 가 없는 legacy row 는 report --interval
# 또는 30s fallback 을 사용한다.
IDLE_CAPACITY_QUERY = """
WITH s AS (
    SELECT gs.gpu_uuid, gs.ts, gs.util_pct, gs.run_id,
           COALESCE(SUM(COALESCE(ps.mem_used_mb, 0)), 0) AS proc_mem_mb
    FROM gpu_sample gs
    LEFT JOIN proc_sample ps
        ON ps.gpu_uuid = gs.gpu_uuid
        AND ps.ts = gs.ts
        AND (ps.run_id = gs.run_id OR (ps.run_id IS NULL AND gs.run_id IS NULL))
    WHERE gs.ts >= ?
    GROUP BY gs.gpu_uuid, gs.ts, gs.run_id
),
gpu_count AS (
    SELECT COUNT(DISTINCT gpu_uuid) AS n FROM s
)
SELECT
    SUM(
        CASE WHEN util_pct < 10 AND proc_mem_mb > 100
             THEN COALESCE(?, dr.interval_seconds, ?) ELSE 0 END
    ) / 3600.0 AS idle_held_gpu_hours,
    SUM(
        CASE WHEN util_pct < 10 AND proc_mem_mb <= 100
             THEN COALESCE(?, dr.interval_seconds, ?) ELSE 0 END
    ) / 3600.0 AS truly_idle_gpu_hours,
    CASE WHEN COUNT(*) = 0 THEN 0.0
         ELSE SUM(CASE WHEN util_pct < 10 AND proc_mem_mb > 100 THEN 1.0 ELSE 0.0 END) / COUNT(*)
              * (SELECT n FROM gpu_count)
    END AS idle_held_equiv_gpus,
    CASE WHEN COUNT(*) = 0 THEN 0.0
         ELSE SUM(CASE WHEN util_pct < 10 AND proc_mem_mb <= 100 THEN 1.0 ELSE 0.0 END) / COUNT(*)
              * (SELECT n FROM gpu_count)
    END AS truly_idle_equiv_gpus,
    COUNT(*) AS samples
FROM s
LEFT JOIN daemon_run dr ON dr.id = s.run_id
"""


@dataclass(slots=True)
class IdleCapacity:
    idle_held_gpu_hours: float = 0.0
    truly_idle_gpu_hours: float = 0.0
    idle_held_equiv_gpus: float = 0.0
    truly_idle_equiv_gpus: float = 0.0
    samples: int = 0
    interval_source: str = "recorded daemon interval (legacy rows fall back to 30s)"


def load_idle_capacity(
    conn: sqlite3.Connection,
    cutoff: datetime,
    interval: timedelta | None = None,
) -> IdleCapacity:
    override_s = interval.total_seconds() if interval is not None else None
    fallback_s = LEGACY_INTERVAL_FALLBACK.total_seconds()
    row = conn.execute(
        IDLE_CAPACITY_QUERY,
        (_ts(cutoff), override_s, fallback_s, override_s, fallback_s),
    ).fetchone()
    if row is None:
        return IdleCapacity()
    idle_held_h, truly_idle_h, idle_held_equiv, truly_idle_equiv, samples = row
    return IdleCapacity(
        idle_held_gpu_hours=idle_held_h or 0.0,
        truly_idle_gpu_hours=truly_idle_h or 0.0,
        idle_held_equiv_gpus=idle_held_equiv or 0.0,
        truly_idle_equiv_gpus=truly_idle_equiv or 0.0,
        samples=samples,
        interval_source=_interval_source(interval),
    )


# ── §3 Per-GPU ──────────────────────────────────────────────────
#
# §1 과 같은 분류룰을 GPU 별로 분해. proc_mem 은 sub-query 로 미리
# (gpu, ts) 별 합산해서 LEFT JOIN.
PER_GPU_QUERY = """
SELECT
    gs.gpu_uuid,
    AVG(CASE WHEN gs.util_pct >= 10                                      THEN 1.0 ELSE 0.0 END) AS active,
    AVG(CASE WHEN gs.util_pct <  10 AND COALESCE(ps.proc_mem, 0) >  100  THEN 1.0 ELSE 0.0 END) AS idle_held,
    AVG(CASE WHEN gs.util_pct <  10 AND COALESCE(ps.proc_mem, 0) <= 100  THEN 1.0 ELSE 0.0 END) AS truly_idle,
    COUNT(*)                                                                                    AS samples
FROM gpu_sample gs
LEFT JOIN (
    SELECT gpu_uuid, ts, run_id, SUM(COALESCE(mem_used_mb, 0)) AS proc_mem
    FROM proc_sample
    GROUP BY gpu_uuid, ts, run_id
) ps ON ps.gpu_uuid = gs.gpu_uuid
    AND ps.ts = gs.ts
    AND (ps.run_id = gs.run_id OR (ps.run_id IS NULL AND gs.run_id IS NULL))
WHERE gs.ts >= ?
GROUP BY gs.gpu_uuid
ORDER BY gs.gpu_uuid
"""


@dataclass(slots=True)
class PerGPU:
    uuid: str
    active: float = 0.0
    idle_held: float = 0.0
    truly_idle: float = 0.0
    samples: int = 0


def load_per_gpu(conn: sqlite3.Connection, cutoff: datetime) -> list[PerGPU]:
    out: list[PerGPU] = []
    for uuid, active, idle_held, truly_idle, samples in conn.execute(PER_GPU_QUERY, (_ts(cutoff),)):
        out.append(
            PerGPU(
                uuid=uuid,
                active=active or 0.0,
                idle_held=idle_held or 0.0,
                truly_idle=truly_idle or 0.0,
                samples=samples,
            )
        )
    return out


# ── §4 Top identities ───────────────────────────────────────────
#
# 누가 GPU-시간을 가장 많이 소비했나 + 그 중 idle-held 비율.
# COALESCE 로 NULL loginuid_user 를 'unknown' 으로 묶음.
# 같은 identity 가 같은 GPU/tick 에 여러 프로세스를 띄워도 한 번만 센다.
TOP_IDENTITIES_QUERY = """
WITH owned AS (
    SELECT
        COALESCE(loginuid_user, 'unknown') AS identity,
        gpu_uuid,
        ts,
        run_id,
        SUM(COALESCE(mem_used_mb, 0)) AS mem_used_mb
    FROM proc_sample
    WHERE ts >= ?
    GROUP BY identity, gpu_uuid, ts, run_id
)
SELECT
    owned.identity                                                                  AS identity,
    SUM(COALESCE(?, dr.interval_seconds, ?)) / 3600.0                                AS gpu_hours,
    AVG(CASE WHEN gs.util_pct < 10 AND owned.mem_used_mb > 100 THEN 1.0 ELSE 0.0 END) AS idle_held,
    COUNT(*)                                                                        AS samples
FROM owned
JOIN gpu_sample gs ON gs.gpu_uuid = owned.gpu_uuid
    AND gs.ts = owned.ts
    AND (owned.run_id = gs.run_id OR (owned.run_id IS NULL AND gs.run_id IS NULL))
LEFT JOIN daemon_run dr ON dr.id = COALESCE(owned.run_id, gs.run_id)
GROUP BY identity
ORDER BY gpu_hours DESC
LIMIT 10
"""


@dataclass(slots=True)
class TopIdentity:
    identity: str
    gpu_hours: float
    idle_held: float
    samples: int


def load_top_identities(
    conn: sqlite3.Connection,
    cutoff: datetime,
    interval: timedelta | None = None,
) -> list[TopIdentity]:
    out: list[TopIdentity] = []
    override_s = interval.total_seconds() if interval is not None else None
    fallback_s = LEGACY_INTERVAL_FALLBACK.total_seconds()
    for identity, gpu_hours, idle_held, samples in conn.execute(
        TOP_IDENTITIES_QUERY, (_ts(cutoff), override_s, fallback_s)
    ):
        out.append(
            TopIdentity(
                identity=identity,
                gpu_hours=gpu_hours or 0.0,
                idle_held=idle_held or 0.0,
                samples=samples,
            )
        )
    return out


def _interval_source(interval: timedelta | None) -> str:
    if interval is None:
        return "recorded daemon interval (legacy rows fall back to 30s)"
    return f"report --interval ({interval})"


# ── §5 Heatmap ──────────────────────────────────────────────────
#
# ts 의 *요일×시간* 으로 그루핑. substr(ts, 1, 19) 로 nano/timezone 떼고
# strftime 로 dow/hour 추출. 0=일요일..6=토요일.
HEATMAP_QUERY = """
SELECT
    CAST(strftime('%w', substr(ts, 1, 19)) AS INTEGER) AS dow,
    CAST(strftime('%H', substr(ts, 1, 19)) AS INTEGER) AS hour,
    AVG(CASE WHEN util_pct >= 10 THEN 1.0 ELSE 0.0 END) AS active,
    COUNT(*)                                            AS samples
FROM gpu_sample
WHERE ts >= ?
GROUP BY dow, hour
ORDER BY dow, hour
"""


@dataclass(slots=True)
class HeatmapCell:
    dow: int
    hour: int
    active: float = 0.0
    samples: int = 0


def load_heatmap(conn: sqlite3.Connection, cutoff: datetime) -> list[HeatmapCell]:
    out: list[HeatmapCell] = []
    for dow, hour, active, samples in conn.execute(HEATMAP_QUERY, (_ts(cutoff),)):
        out.append(
            HeatmapCell(
                dow=dow,
                hour=hour,
                active=active or 0.0,
                samples=samples,
            )
        )
    return out


# ── Action report: 점유 세션 재구성 ─────────────────────────────
#
# proc_sample 을 (gpu_uuid, pid) 로 묶고 연속된 tick 을 이어붙여 "어떤
# 프로세스가 언제~언제 카드를 점유했나" 세션을 복원한다. 구간의
# gpu_sample.util 을 붙여 "얼마나 굴렸나"(카드 단위), mem_used_mb 로
# "얼마나 쥐고 있었나"(프로세스 단위), loginuid_user/owner_user 로 "누구".
#
# util 은 카드 단위 값이라 한 카드를 여러 pid 가 나눠 쓰면 특정 프로세스에
# 귀속되지 않는다 (shared 플래그로 표시). 메모리는 프로세스 단위라 정확.
ACTIVE_UTIL = 10  # util>=이면 실가동 (classify 와 동일 임계).
MEM_FLOOR_MB = 100  # 프로세스 메모리>이면 "점유" — framework 잔량 흡수.
# ponytail: 이보다 오래 같은 pid 가 안 보이면 별개 세션으로 끊는다. 30~60s
# 샘플의 2~3틱 결손을 흡수하고, 진짜로 죽었다 재시작한 건 나눈다.
SESSION_GAP = timedelta(seconds=180)


@dataclass(slots=True)
class Session:
    """한 프로세스가 한 카드를 연속 점유한 구간."""

    gpu_uuid: str
    gpu_index: int | None
    pid: int
    owner: str  # loginuid 우선, 없으면 실 uid, 둘 다 없으면 'unknown'
    has_login: bool  # loginuid 로 해석됨 = 로그인 세션 프로세스(사용자 작업)
    process_name: str
    process_type: str  # 'compute' | 'graphics'
    start: datetime
    end: datetime
    duration_h: float
    avg_util: float  # 구간 평균 util (카드 단위)
    active_frac: float  # util>=ACTIVE_UTIL 인 tick 비율
    peak_mem_mb: int
    samples: int
    shared: bool  # 구간 중 같은 카드에 다른 pid 도 있었나


def _first[T](values: Iterable[T | None]) -> T | None:
    for v in values:
        if v is not None:
            return v
    return None


def _make_session(
    uuid: str,
    pid: int,
    group: list[Any],
    util_map: dict[tuple[str, str], int],
    multi: dict[tuple[str, str], int],
) -> Session:
    tss = [datetime.fromisoformat(r[8]) for r in group]
    utils = [util_map[(uuid, r[8])] for r in group if (uuid, r[8]) in util_map]
    mems = [r[7] for r in group if r[7] is not None]
    login = _first(r[4] for r in group)
    return Session(
        gpu_uuid=uuid,
        gpu_index=_first(r[1] for r in group),
        pid=pid,
        owner=_first(r[3] for r in group) or "unknown",
        has_login=login is not None,
        process_name=_first(r[5] for r in group) or "?",
        process_type=_first(r[6] for r in group) or "compute",
        start=tss[0],
        end=tss[-1],
        duration_h=(tss[-1] - tss[0]).total_seconds() / 3600.0,
        avg_util=(sum(utils) / len(utils)) if utils else 0.0,
        active_frac=(sum(1 for u in utils if u >= ACTIVE_UTIL) / len(utils)) if utils else 0.0,
        peak_mem_mb=max(mems) if mems else 0,
        samples=len(group),
        shared=any(multi.get((uuid, r[8]), 0) > 1 for r in group),
    )


def load_sessions(
    conn: sqlite3.Connection, cutoff: datetime, gap: timedelta = SESSION_GAP
) -> list[Session]:
    """cutoff 이후 proc_sample 을 (gpu, pid) 연속 구간(세션)으로 복원한다."""
    since = _ts(cutoff)
    util_map: dict[tuple[str, str], int] = {
        (u, t): util
        for u, t, util in conn.execute(
            "SELECT gpu_uuid, ts, util_pct FROM gpu_sample WHERE ts >= ?", (since,)
        )
    }
    rows = conn.execute(
        "SELECT gpu_uuid, gpu_index, pid, COALESCE(loginuid_user, owner_user), "
        "loginuid_user, process_name, process_type, mem_used_mb, ts "
        "FROM proc_sample WHERE ts >= ? ORDER BY gpu_uuid, pid, ts",
        (since,),
    ).fetchall()

    multi: dict[tuple[str, str], int] = {}
    for r in rows:
        k = (r[0], r[8])
        multi[k] = multi.get(k, 0) + 1

    sessions: list[Session] = []
    for (uuid, pid), grp_iter in groupby(rows, key=lambda r: (r[0], r[2])):
        grp = list(grp_iter)
        chunk = [grp[0]]
        for prev, nxt in pairwise(grp):
            if datetime.fromisoformat(nxt[8]) - datetime.fromisoformat(prev[8]) > gap:
                sessions.append(_make_session(uuid, pid, chunk, util_map, multi))
                chunk = [nxt]
            else:
                chunk.append(nxt)
        sessions.append(_make_session(uuid, pid, chunk, util_map, multi))
    return sessions


@dataclass(slots=True)
class GpuStatus:
    """카드 한 장의 관측 구간 요약 — 전체 GPU 표 한 줄."""

    gpu_index: int | None
    gpu_uuid: str
    state: str  # 'in_use' | 'idle_held' | 'empty'
    avg_util: float
    peak_mem_mb: int
    owner: str | None  # 최장 점유 세션의 소유자
    process_name: str | None


def load_gpu_status(
    conn: sqlite3.Connection, cutoff: datetime, sessions: list[Session]
) -> list[GpuStatus]:
    """카드별 avg util / peak mem / 상태 + 최장 점유 세션의 소유·프로세스."""
    per_gpu = {pg.uuid: pg for pg in load_per_gpu(conn, cutoff)}
    metrics = {
        u: (avg or 0.0, peak or 0)
        for u, avg, peak in conn.execute(
            "SELECT gpu_uuid, AVG(util_pct), MAX(COALESCE(memory_used_mb, 0)) "
            "FROM gpu_sample WHERE ts >= ? GROUP BY gpu_uuid",
            (_ts(cutoff),),
        )
    }
    idx_map = _gpu_index_map(conn, cutoff)

    # 카드별 최장 세션(어떤 type이든) → 대표 소유자/프로세스.
    top: dict[str, Session] = {}
    for s in sessions:
        cur = top.get(s.gpu_uuid)
        if cur is None or s.duration_h > cur.duration_h:
            top[s.gpu_uuid] = s

    eps = 1e-9
    out: list[GpuStatus] = []
    for uuid, pg in per_gpu.items():
        if pg.active > eps:
            state = "in_use"
        elif pg.idle_held > eps:
            state = "idle_held"
        else:
            state = "empty"
        avg_util, peak_mem = metrics.get(uuid, (0.0, 0))
        rep = top.get(uuid)
        out.append(
            GpuStatus(
                gpu_index=idx_map.get(uuid),
                gpu_uuid=uuid,
                state=state,
                avg_util=avg_util,
                peak_mem_mb=peak_mem,
                owner=rep.owner if rep else None,
                process_name=rep.process_name if rep else None,
            )
        )
    out.sort(key=lambda g: (g.gpu_index is None, g.gpu_index if g.gpu_index is not None else 0))
    return out


@dataclass(slots=True)
class ActionReport:
    host: HostRow
    since: timedelta
    now: datetime
    interval_label: str
    total_samples: int
    gpu_count: int
    active: float  # 실가동 비율 (샘플)
    idle_held: float  # 유휴점유 비율
    truly_idle: float  # 완전유휴 비율
    idle_equiv: float  # 평균 몇 장이 (실가동 안 하고) 놀았나
    held_gpu_hours: float  # 잡고도 안 쓴(idle-held) GPU-시간
    actions: list[Session]  # 조치 대상: idle-held compute 세션 (지속시간 desc)
    gpus: list[GpuStatus]  # 전체 GPU 상태 (index 순)
    free_cards: list[tuple[int | None, str]]  # (gpu_index, uuid) — 내내 빈 카드


def _interval_label(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT interval_seconds FROM daemon_run ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return "30초"
    s = float(row[0])
    if s >= 60 and s % 60 == 0:
        return f"{int(s // 60)}분"
    return f"{s:g}초"


def _gpu_index_map(conn: sqlite3.Connection, cutoff: datetime) -> dict[str, int | None]:
    return {
        u: i
        for u, i in conn.execute(
            "SELECT gpu_uuid, MAX(gpu_index) FROM gpu_sample WHERE ts >= ? GROUP BY gpu_uuid",
            (_ts(cutoff),),
        )
    }


def build_action_report(
    conn: sqlite3.Connection,
    cutoff: datetime,
    now: datetime,
    since: timedelta,
    interval: timedelta | None = None,
) -> ActionReport:
    idle_cap = load_idle_capacity(conn, cutoff, interval)
    headline = load_headline(conn, cutoff)
    sessions = load_sessions(conn, cutoff)
    gpus = load_gpu_status(conn, cutoff, sessions)
    total = conn.execute(
        "SELECT COUNT(*) FROM gpu_sample WHERE ts >= ?", (_ts(cutoff),)
    ).fetchone()[0]

    # 조치 대상: compute 프로세스가 카드를 잡았으나 거의 안 쓴 세션.
    actions = [
        s
        for s in sessions
        if s.process_type == "compute" and s.avg_util < ACTIVE_UTIL and s.peak_mem_mb > MEM_FLOOR_MB
    ]
    actions.sort(key=lambda s: s.duration_h, reverse=True)

    # 내내 빈 카드: 관측 구간에서 실가동/유휴점유가 전혀 없던 카드.
    free = [(g.gpu_index, g.gpu_uuid) for g in gpus if g.state == "empty"]

    return ActionReport(
        host=load_host(conn),
        since=since,
        now=now,
        interval_label=_interval_label(conn),
        total_samples=total,
        gpu_count=len(gpus),
        active=headline.active,
        idle_held=headline.idle_held,
        truly_idle=headline.truly_idle,
        idle_equiv=idle_cap.truly_idle_equiv_gpus + idle_cap.idle_held_equiv_gpus,
        held_gpu_hours=idle_cap.idle_held_gpu_hours,
        actions=actions,
        gpus=gpus,
        free_cards=free,
    )

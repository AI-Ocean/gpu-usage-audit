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
from dataclasses import dataclass
from datetime import datetime, timedelta

from .db import _ts
from .model import HostRow


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
    SELECT gs.gpu_uuid, gs.ts, gs.util_pct,
           COALESCE(SUM(ps.mem_used_mb), 0) AS proc_mem_mb
    FROM gpu_sample gs
    LEFT JOIN proc_sample ps
        ON ps.gpu_uuid = gs.gpu_uuid AND ps.ts = gs.ts
    WHERE gs.ts >= ?
    GROUP BY gs.gpu_uuid, gs.ts
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
# 테이블이 없음 (단순화). interval 은 Python 에서 인자로 받는다 —
# 데몬과 report 가 *같은* interval 을 약속해야 의미가 맞음.
IDLE_CAPACITY_QUERY = """
WITH s AS (
    SELECT gs.gpu_uuid, gs.ts, gs.util_pct,
           COALESCE(SUM(ps.mem_used_mb), 0) AS proc_mem_mb
    FROM gpu_sample gs
    LEFT JOIN proc_sample ps
        ON ps.gpu_uuid = gs.gpu_uuid AND ps.ts = gs.ts
    WHERE gs.ts >= ?
    GROUP BY gs.gpu_uuid, gs.ts
),
gpu_count AS (
    SELECT COUNT(DISTINCT gpu_uuid) AS n FROM s
)
SELECT
    SUM(CASE WHEN util_pct < 10 AND proc_mem_mb >  100 THEN 1 ELSE 0 END) * ? / 3600.0 AS idle_held_gpu_hours,
    SUM(CASE WHEN util_pct < 10 AND proc_mem_mb <= 100 THEN 1 ELSE 0 END) * ? / 3600.0 AS truly_idle_gpu_hours,
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
"""


@dataclass(slots=True)
class IdleCapacity:
    idle_held_gpu_hours: float = 0.0
    truly_idle_gpu_hours: float = 0.0
    idle_held_equiv_gpus: float = 0.0
    truly_idle_equiv_gpus: float = 0.0
    samples: int = 0


def load_idle_capacity(
    conn: sqlite3.Connection,
    cutoff: datetime,
    interval: timedelta,
) -> IdleCapacity:
    interval_s = interval.total_seconds()
    row = conn.execute(IDLE_CAPACITY_QUERY, (_ts(cutoff), interval_s, interval_s)).fetchone()
    if row is None:
        return IdleCapacity()
    idle_held_h, truly_idle_h, idle_held_equiv, truly_idle_equiv, samples = row
    return IdleCapacity(
        idle_held_gpu_hours=idle_held_h or 0.0,
        truly_idle_gpu_hours=truly_idle_h or 0.0,
        idle_held_equiv_gpus=idle_held_equiv or 0.0,
        truly_idle_equiv_gpus=truly_idle_equiv or 0.0,
        samples=samples,
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
    SELECT gpu_uuid, ts, SUM(mem_used_mb) AS proc_mem
    FROM proc_sample
    GROUP BY gpu_uuid, ts
) ps ON ps.gpu_uuid = gs.gpu_uuid AND ps.ts = gs.ts
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
        SUM(mem_used_mb) AS mem_used_mb
    FROM proc_sample
    WHERE ts >= ?
    GROUP BY identity, gpu_uuid, ts
)
SELECT
    owned.identity                                                                  AS identity,
    COUNT(*) * ? / 3600.0                                                           AS gpu_hours,
    AVG(CASE WHEN gs.util_pct < 10 AND owned.mem_used_mb > 100 THEN 1.0 ELSE 0.0 END) AS idle_held,
    COUNT(*)                                                                        AS samples
FROM owned
JOIN gpu_sample gs ON gs.gpu_uuid = owned.gpu_uuid AND gs.ts = owned.ts
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
    interval: timedelta,
) -> list[TopIdentity]:
    out: list[TopIdentity] = []
    for identity, gpu_hours, idle_held, samples in conn.execute(
        TOP_IDENTITIES_QUERY, (_ts(cutoff), interval.total_seconds())
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

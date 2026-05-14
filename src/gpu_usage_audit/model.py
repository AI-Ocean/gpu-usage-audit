"""도메인 데이터 모델 — Go v0.1.0 의 GPUSample/ProcSample/Snapshot/HostMeta 등.

dataclass(slots=True) 로 통일 — 메모리 절약 + 오타 방지 (없는 속성
대입이 즉시 에러). 대부분 *불변* 으로 가져갈 수 있어 frozen 도 가능
하지만, 데몬 루프 안에서 점진 구성하는 경우엔 mutable 이 편리.
기본은 mutable, 호출자가 *값 객체* 처럼 다뤄야 한다는 규약.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from .classify import Class

RuntimeMode = Literal["host", "unsupported"]
TelemetrySource = Literal["nvml"]
SchedulerSource = Literal["none"]
PlanConfidence = Literal["high", "medium", "low"]


@dataclass(slots=True)
class GPUSample:
    """한 카드의 한 틱 — UUID + SM 사용률(%)."""

    uuid: str
    util_pct: int


@dataclass(slots=True)
class ProcSample:
    """한 카드 위의 한 프로세스 — (카드, PID) 단위 메모리 + 해석된 사용자명.

    `loginuid_user` 는 *해석 가능했을 때만* 사용자명, 아니면 None
    (NULL/unknown 셋 모두 다른 의미라 빈 문자열로 합치지 않는다).
    Go 의 `*string` 과 동등 의도 — Optional[str] 로 표현.
    """

    gpu_uuid: str
    pid: int
    mem_used_mb: int
    loginuid_user: str | None = None


@dataclass(slots=True)
class Snapshot:
    """한 틱의 사실 — 모든 카드 + 모든 (카드, 프로세스) 메모리.

    Tier.collect() 가 반환하는 단위, daemon 루프가 DB 로 적재하는 단위.
    """

    gpus: list[GPUSample] = field(default_factory=list)
    procs: list[ProcSample] = field(default_factory=list)


@dataclass(slots=True)
class HostMeta:
    """데몬 startup 에 한 번 결정하고 *수명 내내 들고 다니는* 호스트 컨텍스트.

    hostname/env_kind/driver_version 은 데몬 lifetime 동안 변하지 않는다는
    가정. first_seen 은 host row 의 immutable 필드 (재시작 후에도 첫
    INSERT 시각 보존), last_seen 은 매 틱 갱신.
    """

    hostname: str
    env_kind: str
    driver_version: str
    first_seen: datetime


@dataclass(slots=True)
class RuntimePlan:
    """`gua doctor` 가 만든 로컬 호스트 readiness 판정."""

    mode: RuntimeMode
    telemetry: TelemetrySource
    scheduler: SchedulerSource
    confidence: PlanConfidence
    reasons: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    required_privileges: list[str] = field(default_factory=list)


@dataclass(slots=True)
class HostRow:
    """report 측이 host 테이블에서 읽어 헤더에 노출하는 모양.

    `driver_version` 이 NULL 가능이라 빈 문자열로 표현 (헤더에 "driver "
    없이 보이는 케이스).
    """

    hostname: str = ""
    env_kind: str = ""
    driver_version: str = ""


@dataclass(slots=True)
class CardSummary:
    """한 카드의 한 틱 결과 — Summarize() 의 출력 단위.

    `procs` 는 그 카드 위 프로세스들, mem 내림차순 정렬.
    """

    uuid: str
    util_pct: int
    proc_mem_mb: int
    klass: Class
    procs: list[ProcSample] = field(default_factory=list)

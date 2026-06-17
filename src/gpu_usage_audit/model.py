"""도메인 데이터 모델 — Go v0.1.0 의 GPUSample/ProcSample/Snapshot/HostMeta 등.

dataclass(slots=True) 로 통일 — 메모리 절약 + 오타 방지 (없는 속성
대입이 즉시 에러). 대부분 *불변* 으로 가져갈 수 있어 frozen 도 가능
하지만, 데몬 루프 안에서 점진 구성하는 경우엔 mutable 이 편리.
기본은 mutable, 호출자가 *값 객체* 처럼 다뤄야 한다는 규약.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .classify import Class


@dataclass(slots=True)
class GPUSample:
    """한 카드의 한 틱 — UUID + SM 사용률(%).

    v1.1 cloud sync 를 위한 device identity/metric 필드는 *optional* 로 확장.
    local report 경로는 uuid/util_pct 만 읽으므로 None 이어도 무손상이고,
    NVMLTier/FakeTier 가 채우면 cloud snapshot payload 까지 만들 수 있다.

    name/memory_total_mb 는 *device 정체성* — 저장 시 gpu_device 로 정규화.
    index/memory_used_mb/temperature_c/power_w 는 *시변 metric* — gpu_sample 행.
    """

    uuid: str
    util_pct: int
    index: int | None = None
    name: str | None = None
    memory_total_mb: int | None = None
    memory_used_mb: int | None = None
    temperature_c: int | None = None
    power_w: int | None = None


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
    gpu_index: int | None = None
    process_name: str | None = None


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

    1.0은 로컬 베어메탈 호스트 전용이므로 env_kind 는 "bare" 로 기록한다.
    hostname/env_kind/driver_version 은 데몬 lifetime 동안 변하지 않는다는 가정.
    first_seen 은 host row 의 immutable 필드, last_seen 은 매 틱 갱신.
    """

    hostname: str
    env_kind: str
    driver_version: str
    first_seen: datetime


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

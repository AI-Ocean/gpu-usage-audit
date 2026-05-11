"""한 (카드, 틱) 의 (util, 메모리) → 분류 결정.

Go v0.1.0 의 `Classify` 와 동일한 룰을 한 곳에 모음:

    util >= 10                  → active
    util <  10 AND mem >  100   → idle-held
    util <  10 AND mem <= 100   → truly-idle

100 MB 임계는 PyTorch/TF 런타임 baseline (torch import 만으로
~100 MB 잡힘) 을 흡수해 "GPU 잡고 있다" 로 잘못 분류되지 않게 하는
것이 의도. 임계가 ">=" 인지 ">" 인지가 결과에 영향이 크므로
테이블 드리븐 테스트에 경계값 (10/9, 100/101) 을 콕 박아둠.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Class(StrEnum):
    """3-way 분류. StrEnum 이라 .value 가 곧 문자열 — render/DB 비교 시 편리.

    Go 의 `type Class string` 과 동등. 문자열 값 자체는 그대로 유지
    ("active" / "idle-held" / "truly-idle") — DB 에 적재되거나
    report 헤더에 노출될 때 v0.1.0 과 호환.
    """

    ACTIVE = "active"
    IDLE_HELD = "idle-held"
    TRULY_IDLE = "truly-idle"


@dataclass(frozen=True, slots=True)
class Sample:
    """분류 입력. (한 카드, 한 틱) 의 SM 사용률 + 그 카드 위 프로세스 메모리 합.

    `proc_mem_mb` 는 *카드 단위 합산값* — 한 카드 위 여러 프로세스가
    있으면 호출자가 미리 합쳐서 넘긴다 (Summarize 책임).
    """

    util_pct: int
    proc_mem_mb: int


def classify(sample: Sample) -> Class:
    """Sample → Class. 분기 우선순위: active > idle-held > truly-idle.

    active 가 우선이라 "util 80 + mem 70GB" 같은 경우 *학습 중* 으로
    분류 (idle-held 가 아님). 학습이 끝나면 util 이 떨어지면서 같은
    메모리 보유 상태가 idle-held 로 분류로 *전환*.
    """
    if sample.util_pct >= 10:
        return Class.ACTIVE
    if sample.proc_mem_mb > 100:
        return Class.IDLE_HELD
    return Class.TRULY_IDLE

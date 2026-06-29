"""로컬 라이브 util 버퍼 — uuid별 최근 N개 1초 표본 (work-spec 0032-2).

`gua top` 이 스크롤 그래프를 그리려면 최근 표본 히스토리가 필요하다. 보드(0032-1)는
1시간 ring buffer 를 두지만, 로컬 터미널 뷰는 화면에 들어오는 분량(기본 10분)이면 충분.
1초 규칙 수집이라 `deque(maxlen)` 으로 충분 — 시간 기반 eviction 불필요(가장 게으른 형태).

같은 표본이 동시에 보드 ws 로도 나갈 수 있으나, 데몬 ws 경로는 이 버퍼를 거치지 않고
바로 송신한다(보드가 권위 버퍼를 가짐). 이 버퍼는 *로컬 뷰 전용*.
"""

from __future__ import annotations

from collections import deque

from .model import UtilSample


class LiveUtilBuffer:
    """uuid → 최근 util 표본(deque). append 가 maxlen 초과분을 자동 축출."""

    def __init__(self, max_samples: int = 600) -> None:
        self._max = max_samples
        self._by_uuid: dict[str, deque[UtilSample]] = {}

    def append(self, sample: UtilSample) -> None:
        d = self._by_uuid.get(sample.uuid)
        if d is None:
            d = deque(maxlen=self._max)
            self._by_uuid[sample.uuid] = d
        d.append(sample)

    def append_all(self, samples: list[UtilSample]) -> None:
        for s in samples:
            self.append(s)

    def read(self, uuid: str) -> list[UtilSample]:
        return list(self._by_uuid.get(uuid, ()))

    def uuids(self) -> list[str]:
        return list(self._by_uuid)

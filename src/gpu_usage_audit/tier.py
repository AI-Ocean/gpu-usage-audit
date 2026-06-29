"""GPU telemetry source 추상 + demo/test용 FakeTier.

Tier 는 "한 틱의 GPU 텔레메트리를 어디서 받아오는가" 의 추상. 운영용
NVMLTier 와 demo/test용 FakeTier 가 같은 자리에 꽂힌다.

Python 에는 typing.Protocol — Go 의 interface 와 *구조적 호환*
(implements 선언 불필요). FakeTier 와 NVMLTier 가 같은 모양을 가지면
자동으로 같은 자리에 들어감.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from .model import GPUSample, ProcSample, Snapshot, UtilSample


class Tier(Protocol):
    """Tier 인터페이스. 세 메서드:

    probe: 데몬 시작 시 한 번 호출. 드라이버 버전 등 *변하지 않는 메타* 를
        받는 용도. 진짜 NVML 에선 nvml.SystemGetDriverVersion 호출.
    collect: 무거운 틱 — gpus + 프로세스 enumerate + 온도/전력. 스냅샷 cadence(느림).
    collect_util: *가벼운* 1초 util/mem 표본 — 프로세스 enumerate 없음. 라이브 그래프용.
        ts 는 epoch seconds (보드 ws 와이어가 float).
    """

    def probe(self) -> str: ...
    def collect(self, ts: datetime) -> Snapshot: ...
    def collect_util(self, ts: float) -> list[UtilSample]: ...


class FakeTier:
    """결정적 시간 변동 가짜 데이터.

    GPU-0 의 5틱 주기 (학습 → idle-held → cleanup 의 워크로드를 *압축*):
        0..1  학습 활성 — util 80, 메모리 70GB
        2..3  학습 끝, 메모리는 못 놓음 — util 2, 메모리 70GB (= idle-held)
        4     정리 후 truly-idle — util 0, 메모리 0
    진짜 데몬에선 분/시간 단위로 일어날 일이지만, 데모용으로 5틱에 압축.

    GPU-1: 항상 Jupyter 가 8GB 잡고 있음 → 매 틱 idle-held.
    GPU-2: graphics-only idle (Xorg 시뮬레이션, compute proc 없음).

    같은 호출 시퀀스에 같은 결과 — `_tick` 이 인스턴스 내부 상태이므로
    두 인스턴스가 *독립적으로* 진행된다.

    식별자 정책: FakeTier 가 loginuid_user 를 *미리* 박는다. 데몬이
    None 만 system_user_lookup 으로 해석하므로, 미리 박힌 항목은
    /proc 을 건드리지 않음. 이로써 가짜 PID 가 우연히 *실 시스템* 의
    PID 와 매칭돼 호스트 사용자명을 끌어들이는 부조화를 차단.
    """

    def __init__(self) -> None:
        self._tick = 0

    def probe(self) -> str:
        return "560.35.05-fake"

    def collect(self, ts: datetime) -> Snapshot:
        # ts 는 Tier 인터페이스 호환용 — Fake 구현은 시간을 참조하지 않는다.
        del ts
        phase = self._tick % 5
        self._tick += 1

        if phase < 2:
            gpu0_util, gpu0_mem = 80, 70000
        elif phase < 4:
            gpu0_util, gpu0_mem = 2, 70000
        else:
            gpu0_util, gpu0_mem = 0, 0

        # 메모리 0 이면 그 프로세스는 카드 위에 없는 것 — NVML 도 그렇게 본다.
        # loginuid_user/process_name 미리 박음: alice(학습), bob(Jupyter),
        # None(unknown 시연용). cloud sync 가 /proc 을 건드리지 않게 fake 가 채운다.
        procs: list[ProcSample] = []
        if gpu0_mem > 0:
            procs.append(
                ProcSample(
                    gpu_uuid="GPU-0",
                    pid=1234,
                    mem_used_mb=gpu0_mem,
                    loginuid_user="alice",
                    gpu_index=0,
                    process_name="python",
                )
            )
        procs.extend(
            [
                ProcSample(
                    gpu_uuid="GPU-1",
                    pid=5678,
                    mem_used_mb=8000,
                    loginuid_user="bob",
                    gpu_index=1,
                    process_name="jupyter",
                ),
                # PID 9999 는 의도적으로 미해결 — §4 의 "unknown" 분류 시연.
                ProcSample(
                    gpu_uuid="GPU-1",
                    pid=9999,
                    mem_used_mb=200,
                    loginuid_user=None,
                    gpu_index=1,
                    process_name=None,
                ),
                ProcSample(
                    gpu_uuid="GPU-2",
                    pid=1200,
                    mem_used_mb=128,
                    loginuid_user="gdm",
                    gpu_index=2,
                    process_name="Xorg",
                    process_type="graphics",
                ),
            ]
        )

        return Snapshot(
            gpus=[
                _fake_gpu("GPU-0", index=0, util_pct=gpu0_util, used_mb=gpu0_mem),
                _fake_gpu("GPU-1", index=1, util_pct=2, used_mb=8200),
                _fake_gpu("GPU-2", index=2, util_pct=0, used_mb=128),
            ],
            procs=procs,
        )

    def collect_util(self, ts: float) -> list[UtilSample]:
        """가벼운 util 표본. `_tick`(스냅샷 phase)과 독립 — 시간(ts)으로 위상 결정.

        collect() 의 5-phase util 값을 그대로 흉내내되 *상태를 안 advance* 한다.
        그래서 데몬이 collect()(느림)와 collect_util()(1초)을 섞어 불러도 desync 없음.
        """
        phase = int(ts) % 5
        if phase < 2:
            gpu0_util, gpu0_mem = 80, 70000
        elif phase < 4:
            gpu0_util, gpu0_mem = 2, 70000
        else:
            gpu0_util, gpu0_mem = 0, 0
        return [
            UtilSample(uuid="GPU-0", ts=ts, util_pct=gpu0_util, mem_used_mb=gpu0_mem),
            UtilSample(uuid="GPU-1", ts=ts, util_pct=2, mem_used_mb=8200),
            UtilSample(uuid="GPU-2", ts=ts, util_pct=0, mem_used_mb=128),
        ]


def _fake_gpu(uuid: str, *, index: int, util_pct: int, used_mb: int) -> GPUSample:
    """결정적 fake GPU sample — RTX A6000 48GB 한 장을 흉내."""
    return GPUSample(
        uuid=uuid,
        util_pct=util_pct,
        index=index,
        name="NVIDIA RTX A6000",
        memory_total_mb=49140,
        memory_used_mb=used_mb,
        temperature_c=40 + util_pct // 2,
        power_w=70 + util_pct,
    )

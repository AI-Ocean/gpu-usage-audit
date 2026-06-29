"""보드 라이브 util 스트림 ws 클라이언트 (work-spec 0032-2).

에이전트가 자기 GPU 의 1초 util 표본을 보드로 흘리는 *영속 outbound* ws 연결.
랩 NAT/방화벽 친화(에이전트가 밖으로 나감). 끊기면 지수 백오프로 자동 재연결.

stdlib 에 ws 클라이언트가 없어 `websockets`(sync 클라이언트) 의존을 쓴다 — HTTP
스냅샷 push(urllib)와는 별개의 새 전송. 인증은 enrollment 때 받은 agent token(Bearer)
재사용(0032-1 보드 `/agent/v1/stream` 과 동일 계약).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect as _ws_connect

from ..model import UtilSample
from .config import normalize_server_url

logger = logging.getLogger(__name__)

# 끊겨도 무한 재시도하지만, 송신/연결 에러는 이 둘로 좁혀 잡는다(BLE 회피).
_STREAM_ERRORS = (OSError, WebSocketException)


class WsConnection(Protocol):
    """`with connect(...) as ws:` 의 ws — 우리가 쓰는 건 send 하나뿐."""

    def send(self, message: str) -> None: ...
    def __enter__(self) -> WsConnection: ...
    def __exit__(self, *exc: object) -> None: ...


WsConnect = Callable[..., WsConnection]


def util_message(ts: float, samples: list[UtilSample]) -> dict[str, Any]:
    """보드 `UtilStreamMessage` 와이어 포맷. ts=epoch float, util 0-100, mem MB."""
    return {
        "type": "util",
        "ts": ts,
        "gpus": [
            {"deviceUuid": s.uuid, "utilPct": s.util_pct, "memUsedMb": s.mem_used_mb}
            for s in samples
        ],
    }


def derive_ws_url(server_url: str) -> str:
    """http(s) 보드 URL → ws(s) 스트림 엔드포인트."""
    base = normalize_server_url(server_url)  # trailing slash 제거 + http(s) 검증
    if base.startswith("https://"):
        return "wss://" + base.removeprefix("https://") + "/agent/v1/stream"
    return "ws://" + base.removeprefix("http://") + "/agent/v1/stream"


def stream_util(
    *,
    server_url: str,
    agent_token: str,
    sample_source: Callable[[float], list[UtilSample]],
    clock: Callable[[], float],
    stop: threading.Event,
    interval: float = 1.0,
    connect: WsConnect | None = None,
    backoff_start: float = 1.0,
    max_backoff: float = 30.0,
) -> None:
    """stop 될 때까지 interval 마다 util 표본을 ws 로 송신. 끊기면 백오프 재연결.

    별도 스레드에서 돌도록 설계(데몬 스냅샷 루프와 동시). `connect`/`clock`/
    `sample_source` 는 주입 가능 — GPU·실서버 없이 테스트.
    """
    do_connect = connect or _ws_connect
    ws_url = derive_ws_url(server_url)
    headers = {"Authorization": f"Bearer {agent_token}"}
    backoff = backoff_start
    while not stop.is_set():
        try:
            with do_connect(ws_url, additional_headers=headers) as ws:
                backoff = backoff_start  # 연결 성공 → 백오프 리셋
                logger.info("util ws stream connected: %s", ws_url)
                while not stop.is_set():
                    ts = clock()
                    ws.send(json.dumps(util_message(ts, sample_source(ts))))
                    if stop.wait(interval):
                        return
        except _STREAM_ERRORS as exc:
            if stop.is_set():
                return
            logger.warning(
                "util ws stream lost (%s); reconnecting in %.0fs", exc, backoff
            )
            if stop.wait(backoff):
                return
            backoff = min(backoff * 2, max_backoff)


def start_util_stream_thread(
    *,
    server_url: str,
    agent_token: str,
    sample_source: Callable[[float], list[UtilSample]],
    stop: threading.Event,
    clock: Callable[[], float] = time.time,
    interval: float = 1.0,
    connect: WsConnect | None = None,
) -> threading.Thread:
    """스냅샷 루프와 *동시에* 도는 util ws 스트림 스레드를 띄운다(데몬 --cloud 용).

    daemon=True 라 프로세스 종료를 막지 않음. 종료는 `stop` 으로 신호하고 join.
    """
    thread = threading.Thread(
        target=stream_util,
        kwargs={
            "server_url": server_url,
            "agent_token": agent_token,
            "sample_source": sample_source,
            "clock": clock,
            "stop": stop,
            "interval": interval,
            "connect": connect,
        },
        name="util-ws-stream",
        daemon=True,
    )
    thread.start()
    return thread

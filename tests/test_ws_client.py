"""ws_client 단위테스트 (0032-2 T2.2). 실서버·GPU 불필요 — connect 주입."""

import json
import threading

import pytest

from gpu_usage_audit.cloud.ws_client import (
    derive_ws_url,
    start_util_stream_thread,
    stream_util,
    util_message,
)
from gpu_usage_audit.model import UtilSample
from gpu_usage_audit.tier import FakeTier


def test_util_message_wire_format() -> None:
    msg = util_message(123.5, [UtilSample("GPU-0", 123.5, 87, 41210)])
    assert msg == {
        "type": "util",
        "ts": 123.5,
        "gpus": [{"deviceUuid": "GPU-0", "utilPct": 87, "memUsedMb": 41210}],
    }


def test_derive_ws_url_scheme() -> None:
    assert derive_ws_url("https://board.example.com") == "wss://board.example.com/agent/v1/stream"
    assert derive_ws_url("http://localhost:8000/") == "ws://localhost:8000/agent/v1/stream"


class _FakeWs:
    """주입용 가짜 ws 연결. send 기록 + (옵션) fail_at 에서 끊김 + stop_after 에서 종료."""

    def __init__(
        self,
        sent: list[str],
        *,
        fail_at: int | None = None,
        stop: threading.Event | None = None,
        stop_after: int | None = None,
    ) -> None:
        self._sent = sent
        self._fail_at = fail_at
        self._stop = stop
        self._stop_after = stop_after
        self._n = 0

    def __enter__(self) -> "_FakeWs":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def send(self, message: str) -> None:
        self._n += 1
        if self._fail_at is not None and self._n == self._fail_at:
            raise ConnectionResetError("simulated drop")
        self._sent.append(message)
        if (
            self._stop is not None
            and self._stop_after is not None
            and len(self._sent) >= self._stop_after
        ):
            self._stop.set()  # send 기준 종료 → 다음 stop.wait 에서 빠져나감


def _samples(ts: float) -> list[UtilSample]:
    return [UtilSample("GPU-0", ts, 50, 1000)]


def test_stream_sends_until_stopped() -> None:
    sent: list[str] = []
    seen_headers: dict[str, str] = {}
    stop = threading.Event()
    counter = {"n": 0}

    def clock() -> float:
        counter["n"] += 1
        return float(counter["n"])

    def connect(url: str, *, additional_headers: dict[str, str]) -> _FakeWs:
        seen_headers.update(additional_headers)
        return _FakeWs(sent, stop=stop, stop_after=3)

    stream_util(
        server_url="http://localhost:8000",
        agent_token="tok-abc",
        sample_source=_samples,
        clock=clock,
        stop=stop,
        interval=0.0,
        connect=connect,
    )

    assert seen_headers == {"Authorization": "Bearer tok-abc"}
    assert len(sent) == 3  # 3번째 send 후 stop → 정확히 3
    assert json.loads(sent[0])["ts"] == 1.0


def test_stream_reconnects_after_drop() -> None:
    sent: list[str] = []
    stop = threading.Event()
    counter = {"n": 0}
    connects = {"n": 0}

    def clock() -> float:
        counter["n"] += 1
        if counter["n"] >= 5:
            stop.set()
        return float(counter["n"])

    def connect(url: str, *, additional_headers: dict[str, str]) -> _FakeWs:
        connects["n"] += 1
        # 첫 연결: 2번째 send 에서 끊김 → 재연결 발생해야 함
        fail_at = 2 if connects["n"] == 1 else None
        return _FakeWs(sent, fail_at=fail_at)

    stream_util(
        server_url="http://localhost:8000",
        agent_token="t",
        sample_source=_samples,
        clock=clock,
        stop=stop,
        interval=0.0,
        connect=connect,
        backoff_start=0.0,  # 테스트서 sleep 없음
    )

    assert connects["n"] >= 2  # 끊긴 뒤 재연결함
    assert len(sent) >= 1


def test_stop_before_start_sends_nothing() -> None:
    sent: list[str] = []
    stop = threading.Event()
    stop.set()

    def connect(url: str, *, additional_headers: dict[str, str]) -> _FakeWs:
        raise AssertionError("이미 stop 이면 connect 하지 않아야")

    stream_util(
        server_url="http://localhost:8000",
        agent_token="t",
        sample_source=_samples,
        clock=lambda: 1.0,
        stop=stop,
        interval=0.0,
        connect=connect,
    )
    assert sent == []


def test_start_util_stream_thread_streams_collect_util() -> None:
    """데몬 배선: tier.collect_util → ws 송신(3 GPU 페이로드), stop 으로 종료·join."""
    sent: list[str] = []
    stop = threading.Event()
    counter = {"n": 0}

    def clock() -> float:
        counter["n"] += 1
        return float(counter["n"])

    def connect(url: str, *, additional_headers: dict[str, str]) -> _FakeWs:
        return _FakeWs(sent, stop=stop, stop_after=2)

    thread = start_util_stream_thread(
        server_url="http://localhost:8000",
        agent_token="tok",
        sample_source=FakeTier().collect_util,
        stop=stop,
        clock=clock,
        interval=0.0,
        connect=connect,
    )
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert len(sent) == 2
    assert len(json.loads(sent[0])["gpus"]) == 3  # FakeTier 3 GPU


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])

"""LiveUtilBuffer 단위테스트 (0032-2 T2.1). GPU·네트워크 불필요(순수)."""

from gpu_usage_audit.live_buffer import LiveUtilBuffer
from gpu_usage_audit.model import UtilSample


def _s(uuid: str, ts: float, util: int) -> UtilSample:
    return UtilSample(uuid=uuid, ts=ts, util_pct=util, mem_used_mb=util * 10)


def test_append_read_separates_by_uuid() -> None:
    buf = LiveUtilBuffer()
    buf.append(_s("GPU-0", 1.0, 50))
    buf.append(_s("GPU-1", 1.0, 10))
    buf.append(_s("GPU-0", 2.0, 60))
    assert [s.util_pct for s in buf.read("GPU-0")] == [50, 60]
    assert [s.util_pct for s in buf.read("GPU-1")] == [10]
    assert sorted(buf.uuids()) == ["GPU-0", "GPU-1"]


def test_maxlen_evicts_oldest() -> None:
    buf = LiveUtilBuffer(max_samples=3)
    for i in range(5):
        buf.append(_s("GPU-0", float(i), i))
    # 최근 3개만 (0,1 축출)
    assert [s.util_pct for s in buf.read("GPU-0")] == [2, 3, 4]


def test_read_unknown_uuid_is_empty() -> None:
    assert LiveUtilBuffer().read("nope") == []


def test_append_all() -> None:
    buf = LiveUtilBuffer()
    buf.append_all([_s("GPU-0", 1.0, 1), _s("GPU-1", 1.0, 2)])
    assert len(buf.read("GPU-0")) == 1
    assert len(buf.read("GPU-1")) == 1

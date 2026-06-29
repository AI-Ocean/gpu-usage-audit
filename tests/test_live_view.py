"""gua top 렌더/루프 단위테스트 (0032-2 T2.4). GPU 불필요 — FakeTier."""

import io
import threading
from datetime import UTC, datetime

from gpu_usage_audit.live_buffer import LiveUtilBuffer
from gpu_usage_audit.live_view import render_top, run_top, sparkline
from gpu_usage_audit.model import UtilSample
from gpu_usage_audit.tier import FakeTier


def test_sparkline_levels_and_width() -> None:
    assert sparkline([0, 50, 100], 3) == " ▄█"  # 0→space, 50→▄(idx4), 100→█
    assert len(sparkline([50, 50], 10)) == 10  # 폭만큼 우측정렬 패딩
    assert sparkline([100], 5).endswith("█")


def test_render_top_shows_name_util_and_process() -> None:
    buf = LiveUtilBuffer()
    buf.append_all(FakeTier().collect_util(0.0))  # phase0: GPU-0 util 80
    snapshot = FakeTier().collect(datetime.now(UTC))  # 첫 collect: GPU-0 alice 프로세스
    out = render_top(buf, snapshot)
    assert "NVIDIA RTX A6000" in out
    assert "util  80%" in out
    assert "[" in out and "]" in out  # 스파크라인
    assert "alice" in out  # GPU-0 프로세스


def test_render_top_without_snapshot_still_renders_util() -> None:
    buf = LiveUtilBuffer()
    buf.append(UtilSample("GPU-x", 1.0, 42, 100))
    out = render_top(buf, None)
    assert "GPU-x" in out
    assert "util  42%" in out


def test_run_top_renders_n_frames() -> None:
    stop = threading.Event()
    out = io.StringIO()
    counter = {"n": 0}

    def clock() -> float:
        counter["n"] += 1
        return float(counter["n"])

    frames = run_top(
        FakeTier(),
        stop=stop,
        out=out,
        interval=0.0,
        clock=clock,
        max_frames=2,
    )
    assert frames == 2
    assert "NVIDIA RTX A6000" in out.getvalue()

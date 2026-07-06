from datetime import date
from typing import cast

from gpu_usage_audit.cloud.archive_sync import DailyArchiveScheduler
from gpu_usage_audit.cloud.config import CloudConfig

# config is only used by the default runner; these tests inject their own runner.
_NO_CONFIG = cast(CloudConfig, None)


def _join(sched: DailyArchiveScheduler) -> None:
    assert sched._thread is not None
    sched._thread.join()


def test_scheduler_runs_once_per_utc_day() -> None:
    calls: list[date] = []
    sched = DailyArchiveScheduler(_NO_CONFIG, db_path=":memory:", runner=calls.append)

    d1, d2 = date(2026, 7, 5), date(2026, 7, 6)
    sched.maybe_run(d1)
    _join(sched)
    sched.maybe_run(d1)  # same day -> no new run
    _join(sched)
    sched.maybe_run(d2)  # new day -> runs again
    _join(sched)

    assert calls == [d1, d2]


def test_scheduler_swallows_runner_errors() -> None:
    def boom(_today: date) -> None:
        raise RuntimeError("upload failed")

    sched = DailyArchiveScheduler(_NO_CONFIG, db_path=":memory:", runner=boom)
    sched.maybe_run(date(2026, 7, 6))  # must not raise
    _join(sched)
    # a failed day is still marked done (retries next day, not next tick)
    assert sched._last_day == date(2026, 7, 6)

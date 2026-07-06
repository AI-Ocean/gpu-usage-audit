from datetime import date

from gpu_usage_audit.cloud.archive_sync import DailyArchiveScheduler


def test_scheduler_runs_once_per_utc_day() -> None:
    calls: list[date] = []
    sched = DailyArchiveScheduler(config=None, db_path=":memory:", runner=calls.append)

    d1, d2 = date(2026, 7, 5), date(2026, 7, 6)
    sched.maybe_run(d1)
    sched._thread.join()
    sched.maybe_run(d1)  # same day -> no new run
    if sched._thread is not None:
        sched._thread.join()
    sched.maybe_run(d2)  # new day -> runs again
    sched._thread.join()

    assert calls == [d1, d2]


def test_scheduler_swallows_runner_errors() -> None:
    def boom(_today: date) -> None:
        raise RuntimeError("upload failed")

    sched = DailyArchiveScheduler(config=None, db_path=":memory:", runner=boom)
    sched.maybe_run(date(2026, 7, 6))  # must not raise
    sched._thread.join()
    # a failed day is still marked done (retries next day, not next tick)
    assert sched._last_day == date(2026, 7, 6)

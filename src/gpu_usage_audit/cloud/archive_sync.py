"""Daily raw-history archive orchestration + daemon scheduler.

`sync_archive_once` is shared by the `gua sync-archive` CLI and the daemon so
both do the exact same thing: enumerate closed days, upload the ones the board is
missing, prune beyond retention. `DailyArchiveScheduler` lets the always-running
daemon do it itself (once per UTC day, in a background thread) — no separate cron
for the operator to install.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ..db import open_db
from .archive import (
    ARCHIVE_TABLES,
    DEFAULT_RETENTION_DAYS,
    META_DATE,
    META_TABLES,
    closed_days_with_data,
    dump_day_csv_gz,
    dump_table_csv_gz,
)
from .client import list_archives, prune_archives, put_archive
from .config import CloudConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ArchiveResult:
    days: int
    uploaded: int


def sync_archive_once(
    config: CloudConfig,
    *,
    db_path: str | Path,
    today: date,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> ArchiveResult:
    """Upload every closed day the board is missing, refresh the dimension tables,
    then prune beyond the retention window. Idempotent — re-running only fills gaps.
    Opens its own read connection so it is safe to call from a daemon thread."""
    conn = open_db(Path(db_path))
    try:
        days = closed_days_with_data(conn, today=today, retention_days=retention_days)
        stored = set(list_archives(config))
        uploaded = 0
        for day in days:
            for table in ARCHIVE_TABLES:
                if (day, table) in stored:
                    continue
                put_archive(config, day=day, table=table, data=dump_day_csv_gz(conn, day, table))
                uploaded += 1
        for table in META_TABLES:
            put_archive(config, day=META_DATE, table=table, data=dump_table_csv_gz(conn, table))
        prune_archives(config, retention_days=retention_days)
    finally:
        conn.close()
    return ArchiveResult(days=len(days), uploaded=uploaded)


class DailyArchiveScheduler:
    """Runs the archive at most once per UTC day, in a background thread so it
    never blocks the sampling loop. Failures are logged, not raised. First call
    (last_day is None) runs immediately, so a fresh daemon backfills on startup."""

    def __init__(
        self,
        config: CloudConfig,
        *,
        db_path: str | Path,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        runner: Callable[[date], None] | None = None,
    ) -> None:
        self._config = config
        self._db_path = db_path
        self._retention_days = retention_days
        self._runner = runner or self._default_runner
        self._last_day: date | None = None
        self._thread: threading.Thread | None = None

    def maybe_run(self, today: date) -> None:
        if today == self._last_day:
            return
        if self._thread is not None and self._thread.is_alive():
            return  # yesterday's run still going; retry next tick (still gated by day)
        self._last_day = today
        self._thread = threading.Thread(
            target=self._safe_run, args=(today,), name="gua-archive", daemon=True
        )
        self._thread.start()

    def _safe_run(self, today: date) -> None:
        try:
            self._runner(today)
        except Exception:
            logger.exception("archive sync failed; will retry next day")

    def _default_runner(self, today: date) -> None:
        result = sync_archive_once(
            self._config,
            db_path=self._db_path,
            today=today,
            retention_days=self._retention_days,
        )
        logger.info(
            "archive sync: %d day(s) in %dd window, uploaded %d object(s)",
            result.days,
            self._retention_days,
            result.uploaded,
        )

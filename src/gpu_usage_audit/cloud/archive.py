"""Local history → daily gzip-CSV archives for GUA Board object storage.

The agent keeps full-resolution history locally; complete past days are dumped
as gzip CSV and uploaded (via the board) to object storage, so reports can be
rebuilt from raw at any time. Cheap: one host-day of raw ≈ 0.2 MB gzipped.

Idempotency: one deterministic object per (day, table). The agent lists what the
board already has and uploads only the gap, bounded to the retention window; the
board prunes objects older than the window.

Contract source of truth = GUA Board repo (POST/GET/DELETE /agent/v1/archives).
"""

from __future__ import annotations

import csv
import gzip
import io
import sqlite3
from datetime import date, timedelta

# Per-day time-series tables. gpu_sample = util/mem/power/state curves;
# proc_sample = which pid/user held which card (the "who" ledger).
ARCHIVE_TABLES = ("gpu_sample", "proc_sample")

# Small dimension tables with no `ts`: daemon_run carries the per-run sampling
# interval (so a report converts sample counts → time correctly even when the
# interval changed over the window), gpu_device carries name + total memory.
# Re-uploaded whole each run under META_DATE (overwrite), so they stay tiny and
# current. The board must skip META_DATE when pruning by age.
META_TABLES = ("daemon_run", "gpu_device")
META_DATE = "_full"

DEFAULT_RETENTION_DAYS = 30


def closed_days_with_data(
    conn: sqlite3.Connection,
    *,
    today: date,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> list[str]:
    """Sorted 'YYYY-MM-DD' for complete past days that have rows, within retention.

    `today` (UTC) is still open so it is excluded — only closed days are archived.
    Days older than the retention window are excluded (the board prunes those).
    Samples are stored as UTC isoformat, so the `YYYY-MM-DD` prefix is the UTC day.
    """
    cutoff = (today - timedelta(days=retention_days)).isoformat()
    today_str = today.isoformat()
    days: set[str] = set()
    for table in ARCHIVE_TABLES:
        # ponytail: substr scan (no ts-only index); fine for a daily job that
        # normally dumps one new day. Add an index if backfill scans get slow.
        rows = conn.execute(
            f"SELECT DISTINCT substr(ts, 1, 10) AS day FROM {table} "
            "WHERE substr(ts, 1, 10) < :today AND substr(ts, 1, 10) >= :cutoff",
            {"today": today_str, "cutoff": cutoff},
        )
        for (day,) in rows:
            if day:
                days.add(day)
    return sorted(days)


def dump_day_csv_gz(conn: sqlite3.Connection, day: str, table: str) -> bytes:
    """gzip(CSV) of one table's rows for one UTC day. Header row always present."""
    columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(columns)
    rows = conn.execute(
        f"SELECT {', '.join(columns)} FROM {table} WHERE substr(ts, 1, 10) = ? ORDER BY ts",
        (day,),
    )
    writer.writerows(rows)
    return gzip.compress(buffer.getvalue().encode("utf-8"), 6)


def dump_table_csv_gz(conn: sqlite3.Connection, table: str) -> bytes:
    """gzip(CSV) of a whole small dimension table (daemon_run / gpu_device)."""
    columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(columns)
    writer.writerows(conn.execute(f"SELECT {', '.join(columns)} FROM {table}"))
    return gzip.compress(buffer.getvalue().encode("utf-8"), 6)

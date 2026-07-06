import csv
import gzip
import io
import sqlite3
from datetime import date

from gpu_usage_audit.cloud.archive import (
    ARCHIVE_TABLES,
    META_DATE,
    META_TABLES,
    closed_days_with_data,
    dump_day_csv_gz,
    dump_table_csv_gz,
)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE gpu_sample(ts TEXT, gpu_uuid TEXT, util_pct INT, gpu_index INT, usage_state TEXT);"
        "CREATE TABLE proc_sample(ts TEXT, gpu_uuid TEXT, pid INT, process_name TEXT);"
        "CREATE TABLE daemon_run(id INT, started_at TEXT, interval_seconds REAL);"
        "CREATE TABLE gpu_device(gpu_uuid TEXT, name TEXT, memory_total_mb INT);"
    )
    conn.executemany(
        "INSERT INTO gpu_sample VALUES(?,?,?,?,?)",
        [
            ("2026-05-01T10:00:00+00:00", "G", 0, 0, "idle"),  # older than retention
            ("2026-06-29T08:00:00+00:00", "G", 50, 0, "active"),  # closed, in window
            ("2026-06-30T09:00:00+00:00", "G", 0, 1, "idle_held"),  # closed, in window
            ("2026-07-06T01:00:00+00:00", "G", 10, 0, "idle"),  # today -> excluded
            ("2026-06-30T09:00:30+00:00", "G", 77, None, "active"),  # raw is kept regardless of index
        ],
    )
    conn.execute("INSERT INTO proc_sample VALUES('2026-06-30T09:00:00+00:00','G',4018648,'python')")
    conn.execute("INSERT INTO daemon_run VALUES(1,'2026-06-29T00:00:00+00:00',30)")
    conn.execute("INSERT INTO gpu_device VALUES('G','NVIDIA RTX A6000',49140)")
    return conn


def test_closed_days_excludes_today_and_beyond_retention() -> None:
    days = closed_days_with_data(_db(), today=date(2026, 7, 6), retention_days=30)
    assert days == ["2026-06-29", "2026-06-30"]


def test_dump_day_is_lossless_for_the_day() -> None:
    text = gzip.decompress(dump_day_csv_gz(_db(), "2026-06-30", "gpu_sample")).decode()
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == ["ts", "gpu_uuid", "util_pct", "gpu_index", "usage_state"]
    assert len(rows) - 1 == 2  # both 2026-06-30 rows, including the NULL-index one


def test_dump_empty_day_is_header_only() -> None:
    text = gzip.decompress(dump_day_csv_gz(_db(), "2026-06-29", "proc_sample")).decode()
    assert len(list(csv.reader(io.StringIO(text)))) == 1


def test_dump_table_dumps_whole_dimension_table() -> None:
    text = gzip.decompress(dump_table_csv_gz(_db(), "daemon_run")).decode()
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == ["id", "started_at", "interval_seconds"]
    assert rows[1][2] == "30.0"


def test_archive_and_meta_constants() -> None:
    assert META_DATE == "_full"
    assert set(META_TABLES) == {"daemon_run", "gpu_device"}
    assert set(ARCHIVE_TABLES) == {"gpu_sample", "proc_sample"}

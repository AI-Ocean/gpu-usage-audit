"""§1~§5 query 테스트. Go v0.1.0 의 LoadXxx 테스트와 동일 fixture/계산.

시나리오 (40초, 10s interval, 2 카드, 2 사용자):
  ts0:  GPU-0 util=80 alice 70GB ,  GPU-1 util=2 bob 70GB
  ts10: GPU-0 util=80 alice 70GB ,  GPU-1 util=2 bob 70GB
  ts20: GPU-0 util=2  (proc 없음),  GPU-1 util=2 bob 70GB
  ts30: GPU-0 util=2  (proc 없음),  GPU-1 util=2 bob 70GB

  → gpu_sample 8 행, proc_sample 6 행.
  → active 25% / idle-held 50% / truly-idle 25%
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gpu_usage_audit.db import open_db, write_snapshot
from gpu_usage_audit.model import GPUSample, HostMeta, HostRow, ProcSample, Snapshot
from gpu_usage_audit.report import (
    load_headline,
    load_heatmap,
    load_host,
    load_idle_capacity,
    load_per_gpu,
    load_top_identities,
)

INTERVAL = timedelta(seconds=10)
BASE = datetime(2026, 5, 11, 0, 0, 0, tzinfo=UTC)


def _fixture_host() -> HostMeta:
    return HostMeta(
        hostname="testhost",
        env_kind="bare",
        driver_version="999.99-test",
        first_seen=BASE,
    )


def _load_fixture(conn: sqlite3.Connection) -> None:
    host = _fixture_host()
    snaps: list[Snapshot] = [
        Snapshot(
            gpus=[GPUSample(uuid="GPU-0", util_pct=80), GPUSample(uuid="GPU-1", util_pct=2)],
            procs=[
                ProcSample(gpu_uuid="GPU-0", pid=100, mem_used_mb=70000, loginuid_user="alice"),
                ProcSample(gpu_uuid="GPU-1", pid=200, mem_used_mb=70000, loginuid_user="bob"),
            ],
        ),
        Snapshot(
            gpus=[GPUSample(uuid="GPU-0", util_pct=80), GPUSample(uuid="GPU-1", util_pct=2)],
            procs=[
                ProcSample(gpu_uuid="GPU-0", pid=100, mem_used_mb=70000, loginuid_user="alice"),
                ProcSample(gpu_uuid="GPU-1", pid=200, mem_used_mb=70000, loginuid_user="bob"),
            ],
        ),
        Snapshot(
            gpus=[GPUSample(uuid="GPU-0", util_pct=2), GPUSample(uuid="GPU-1", util_pct=2)],
            procs=[
                ProcSample(gpu_uuid="GPU-1", pid=200, mem_used_mb=70000, loginuid_user="bob"),
            ],
        ),
        Snapshot(
            gpus=[GPUSample(uuid="GPU-0", util_pct=2), GPUSample(uuid="GPU-1", util_pct=2)],
            procs=[
                ProcSample(gpu_uuid="GPU-1", pid=200, mem_used_mb=70000, loginuid_user="bob"),
            ],
        ),
    ]
    for i, snap in enumerate(snaps):
        write_snapshot(conn, BASE + INTERVAL * i, host, snap)


@pytest.fixture
def db_loaded(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = open_db(tmp_path / "fixture.db")
    _load_fixture(conn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def db_empty(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = open_db(tmp_path / "empty.db")
    try:
        yield conn
    finally:
        conn.close()


def _close(a: float, b: float, tol: float = 1e-9) -> bool:
    return math.isclose(a, b, abs_tol=tol)


# ── load_host ────────────────────────────────────────────────────


def test_load_host_empty(db_empty: sqlite3.Connection) -> None:
    assert load_host(db_empty) == HostRow()


def test_load_host_populated(db_loaded: sqlite3.Connection) -> None:
    assert load_host(db_loaded) == HostRow(
        hostname="testhost", env_kind="bare", driver_version="999.99-test"
    )


# ── load_headline ───────────────────────────────────────────────


def test_load_headline_window(db_loaded: sqlite3.Connection) -> None:
    h = load_headline(db_loaded, BASE)
    assert h.samples == 8
    assert _close(h.active, 2.0 / 8)
    assert _close(h.idle_held, 4.0 / 8)
    assert _close(h.truly_idle, 2.0 / 8)


def test_load_headline_empty(db_empty: sqlite3.Connection) -> None:
    h = load_headline(db_empty, datetime.now(UTC))
    assert h.samples == 0
    assert h.active == 0
    assert h.idle_held == 0
    assert h.truly_idle == 0


def test_load_headline_cutoff_past_all(db_loaded: sqlite3.Connection) -> None:
    h = load_headline(db_loaded, BASE + timedelta(hours=1))
    assert h.samples == 0


# ── load_idle_capacity ──────────────────────────────────────────


def test_load_idle_capacity(db_loaded: sqlite3.Connection) -> None:
    idle_capacity = load_idle_capacity(db_loaded, BASE, INTERVAL)
    assert idle_capacity.samples == 8
    assert _close(idle_capacity.idle_held_gpu_hours, 4 * 10 / 3600)
    assert _close(idle_capacity.truly_idle_gpu_hours, 2 * 10 / 3600)
    assert _close(idle_capacity.idle_held_equiv_gpus, 1.0)
    assert _close(idle_capacity.truly_idle_equiv_gpus, 0.5)


# ── load_per_gpu ────────────────────────────────────────────────


def test_load_per_gpu(db_loaded: sqlite3.Connection) -> None:
    rows = load_per_gpu(db_loaded, BASE)
    assert len(rows) == 2
    g0, g1 = rows
    assert g0.uuid == "GPU-0"
    assert _close(g0.active, 0.5)
    assert _close(g0.idle_held, 0.0)
    assert _close(g0.truly_idle, 0.5)
    assert g0.samples == 4
    assert g1.uuid == "GPU-1"
    assert _close(g1.active, 0.0)
    assert _close(g1.idle_held, 1.0)
    assert _close(g1.truly_idle, 0.0)
    assert g1.samples == 4


# ── load_top_identities ────────────────────────────────────────


def test_load_top_identities(db_loaded: sqlite3.Connection) -> None:
    rows = load_top_identities(db_loaded, BASE, INTERVAL)
    assert len(rows) == 2
    bob, alice = rows  # ORDER BY gpu_hours DESC
    assert bob.identity == "bob"
    assert _close(bob.gpu_hours, 4 * 10 / 3600)
    assert _close(bob.idle_held, 1.0)
    assert bob.samples == 4
    assert alice.identity == "alice"
    assert _close(alice.gpu_hours, 2 * 10 / 3600)
    assert _close(alice.idle_held, 0.0)
    assert alice.samples == 2


def test_load_top_identities_collapses_same_identity_on_same_gpu_tick(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "top-collapse.db")
    try:
        write_snapshot(
            conn,
            BASE,
            _fixture_host(),
            Snapshot(
                gpus=[GPUSample(uuid="GPU-0", util_pct=2)],
                procs=[
                    ProcSample(gpu_uuid="GPU-0", pid=100, mem_used_mb=70000, loginuid_user="alice"),
                    ProcSample(gpu_uuid="GPU-0", pid=101, mem_used_mb=200, loginuid_user="alice"),
                ],
            ),
        )

        rows = load_top_identities(conn, BASE, INTERVAL)
        assert len(rows) == 1
        alice = rows[0]
        assert alice.identity == "alice"
        assert alice.samples == 1
        assert _close(alice.gpu_hours, 10 / 3600)
        assert _close(alice.idle_held, 1.0)
    finally:
        conn.close()


# ── load_heatmap ────────────────────────────────────────────────


def test_load_heatmap_two_cells(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "heat.db")
    try:
        host = _fixture_host()
        mon_midnight = datetime(2026, 5, 11, 0, 0, 0, tzinfo=UTC)
        tue_1am = datetime(2026, 5, 12, 1, 0, 0, tzinfo=UTC)

        # 셀 1: 월요일 자정대 — 2 tick × 2 GPU
        for i in range(2):
            write_snapshot(
                conn,
                mon_midnight + timedelta(seconds=i),
                host,
                Snapshot(
                    gpus=[
                        GPUSample(uuid="GPU-0", util_pct=80),
                        GPUSample(uuid="GPU-1", util_pct=2),
                    ]
                ),
            )
        # 셀 2: 화요일 1시대 — 1 tick × 2 GPU
        write_snapshot(
            conn,
            tue_1am,
            host,
            Snapshot(
                gpus=[
                    GPUSample(uuid="GPU-0", util_pct=80),
                    GPUSample(uuid="GPU-1", util_pct=2),
                ]
            ),
        )

        cells = load_heatmap(conn, mon_midnight)
        assert len(cells) == 2

        mon, tue = cells
        # SQLite strftime %w 와 Python Weekday — date 자체로 직접 비교 어렵지만
        # ORDER BY dow,hour 가 mon→tue 순서임을 활용. dow 값은 fixture 시점에
        # 고정 (mon=1, tue=2 in strftime '%w' = Mon..Sat=1..6, Sun=0).
        assert mon.dow == 1
        assert mon.hour == 0
        assert _close(mon.active, 0.5)
        assert mon.samples == 4
        assert tue.dow == 2
        assert tue.hour == 1
        assert _close(tue.active, 0.5)
        assert tue.samples == 2
    finally:
        conn.close()


def test_load_heatmap_empty(db_empty: sqlite3.Connection) -> None:
    assert load_heatmap(db_empty, datetime.now(UTC)) == []

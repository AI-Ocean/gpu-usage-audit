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

from gpu_usage_audit.db import open_db, start_daemon_run, write_snapshot
from gpu_usage_audit.model import GPUSample, HostMeta, HostRow, ProcSample, Snapshot
from gpu_usage_audit.report import (
    build_action_report,
    load_headline,
    load_heatmap,
    load_host,
    load_idle_capacity,
    load_per_gpu,
    load_sessions,
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


def test_load_idle_capacity_uses_recorded_interval_by_default(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "recorded-interval.db")
    try:
        host = _fixture_host()
        run_a = start_daemon_run(conn, BASE, timedelta(seconds=5))
        run_b = start_daemon_run(conn, BASE + timedelta(minutes=1), timedelta(seconds=20))
        write_snapshot(
            conn,
            BASE,
            host,
            Snapshot(
                gpus=[GPUSample(uuid="GPU-0", util_pct=2)],
                procs=[ProcSample(gpu_uuid="GPU-0", pid=100, mem_used_mb=70000)],
            ),
            run_id=run_a,
        )
        write_snapshot(
            conn,
            BASE + timedelta(minutes=1),
            host,
            Snapshot(
                gpus=[GPUSample(uuid="GPU-0", util_pct=2)],
                procs=[ProcSample(gpu_uuid="GPU-0", pid=100, mem_used_mb=70000)],
            ),
            run_id=run_b,
        )

        idle_capacity = load_idle_capacity(conn, BASE)
        assert idle_capacity.samples == 2
        assert _close(idle_capacity.idle_held_gpu_hours, (5 + 20) / 3600)

        identities = load_top_identities(conn, BASE)
        assert len(identities) == 1
        assert _close(identities[0].gpu_hours, (5 + 20) / 3600)
    finally:
        conn.close()


def test_load_idle_capacity_interval_override_wins_over_recorded_interval(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "override-interval.db")
    try:
        run_id = start_daemon_run(conn, BASE, timedelta(seconds=5))
        write_snapshot(
            conn,
            BASE,
            _fixture_host(),
            Snapshot(
                gpus=[GPUSample(uuid="GPU-0", util_pct=2)],
                procs=[ProcSample(gpu_uuid="GPU-0", pid=100, mem_used_mb=70000)],
            ),
            run_id=run_id,
        )

        idle_capacity = load_idle_capacity(conn, BASE, timedelta(seconds=30))
        assert _close(idle_capacity.idle_held_gpu_hours, 30 / 3600)
    finally:
        conn.close()


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


# ── sessions / action report ─────────────────────────────────────


def test_load_sessions_reconstructs_contiguous(db_loaded: sqlite3.Connection) -> None:
    sessions = load_sessions(db_loaded, BASE - timedelta(seconds=1))
    by_pid = {s.pid: s for s in sessions}
    assert set(by_pid) == {100, 200}

    alice = by_pid[100]  # GPU-0: ts0,ts10 util=80
    assert alice.owner == "alice"
    assert alice.has_login
    assert alice.samples == 2
    assert _close(alice.avg_util, 80.0)
    assert alice.end - alice.start == timedelta(seconds=10)

    bob = by_pid[200]  # GPU-1: ts0..ts30 util=2, 70GB
    assert bob.owner == "bob"
    assert bob.samples == 4
    assert _close(bob.avg_util, 2.0)
    assert bob.peak_mem_mb == 70000
    assert not bob.shared  # 카드당 tick 마다 pid 하나뿐


def test_action_report_flags_idle_held(db_loaded: sqlite3.Connection) -> None:
    now = BASE + INTERVAL * 4
    rep = build_action_report(db_loaded, BASE - timedelta(seconds=1), now, timedelta(minutes=1))
    # bob 은 70GB 잡고 util 2% → 조치 대상. alice(util 80)는 실사용이라 제외.
    assert [s.pid for s in rep.actions] == [200]
    assert rep.actions[0].owner == "bob"
    # GPU-0 은 실가동, GPU-1 은 유휴점유 → 내내 빈 카드 없음.
    assert rep.free_cards == []


def test_load_sessions_splits_on_gap(db_empty: sqlite3.Connection) -> None:
    host = _fixture_host()
    snap = Snapshot(
        gpus=[GPUSample(uuid="GPU-0", util_pct=2)],
        procs=[ProcSample(gpu_uuid="GPU-0", pid=100, mem_used_mb=5000, loginuid_user="alice")],
    )
    write_snapshot(db_empty, BASE, host, snap)
    write_snapshot(db_empty, BASE + INTERVAL, host, snap)  # 연속
    write_snapshot(db_empty, BASE + timedelta(seconds=600), host, snap)  # >180s → 새 세션
    sessions = load_sessions(db_empty, BASE - timedelta(seconds=1))
    assert [s.samples for s in sessions] == [2, 1]

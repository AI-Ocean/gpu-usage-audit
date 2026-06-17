"""`gua enroll` / `gua sync-once` CLI 동작 (network 모킹)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from gpu_usage_audit.__main__ import gua_main
from gpu_usage_audit.cloud.client import CloudError
from gpu_usage_audit.cloud.config import CloudConfig, load_cloud_config, save_cloud_config
from gpu_usage_audit.model import GPUSample, Snapshot


def _config() -> CloudConfig:
    return CloudConfig(
        server_url="https://board.example.com",
        host_id="host-123",
        display_name="a6000-01",
        agent_token="gua_agent_secret",
        token_prefix="gua_agent_xx",
    )


# ── enroll ───────────────────────────────────────────────────────


def test_enroll_writes_config_and_hides_tokens(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, Any] = {}

    def fake_claim(**kwargs: Any) -> CloudConfig:
        captured.update(kwargs)
        return _config()

    monkeypatch.setattr("gpu_usage_audit.__main__.claim_enrollment", fake_claim)
    config_path = tmp_path / "cloud.json"

    rc = gua_main(
        [
            "enroll",
            "--server-url",
            "https://board.example.com",
            "--enrollment-token",
            "enroll-secret-must-not-leak",
            "--config",
            str(config_path),
            "--hostname",
            "gpu-01",
            "--driver-version",
            "560.35.05",
        ]
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert load_cloud_config(config_path) == _config()
    assert captured["enrollment_token"] == "enroll-secret-must-not-leak"
    # 출력에는 prefix/display/host id 만 — 어떤 token 원문도 없음.
    assert "a6000-01" in out and "host-123" in out and "gua_agent_xx" in out
    assert "enroll-secret-must-not-leak" not in out
    assert "gua_agent_secret" not in out


def test_enroll_refuses_existing_config_without_force(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "cloud.json"
    save_cloud_config(_config(), config_path)

    called = False

    def fake_claim(**_kwargs: Any) -> CloudConfig:
        nonlocal called
        called = True
        return _config()

    monkeypatch.setattr("gpu_usage_audit.__main__.claim_enrollment", fake_claim)

    rc = gua_main(
        [
            "enroll",
            "--server-url",
            "https://board.example.com",
            "--enrollment-token",
            "tok",
            "--config",
            str(config_path),
            "--driver-version",
            "x",
        ]
    )

    assert rc == 2
    assert "already exists" in capsys.readouterr().err
    assert called is False  # claim 시도 전에 막혀야 한다.


def test_enroll_reports_claim_failure_as_exit_1(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_claim(**_kwargs: Any) -> CloudConfig:
        raise CloudError("GUA Board request failed with HTTP 401: invalid or expired token")

    monkeypatch.setattr("gpu_usage_audit.__main__.claim_enrollment", fake_claim)
    config_path = tmp_path / "cloud.json"

    rc = gua_main(
        [
            "enroll",
            "--server-url",
            "https://board.example.com",
            "--enrollment-token",
            "tok",
            "--config",
            str(config_path),
            "--driver-version",
            "x",
        ]
    )

    assert rc == 1
    assert "HTTP 401" in capsys.readouterr().err
    assert not config_path.exists()


# ── sync-once ─────────────────────────────────────────────────────


def test_sync_once_fake_writes_local_db_then_pushes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "cloud.json"
    save_cloud_config(_config(), config_path)
    db_path = tmp_path / "gua.db"
    pushed: dict[str, Any] = {}

    def fake_post(config: CloudConfig, payload: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        pushed["payload"] = payload
        return {"status": "accepted", "gpuSlotsSeen": len(payload["gpus"])}

    monkeypatch.setattr("gpu_usage_audit.__main__.post_observation", fake_post)

    rc = gua_main(["sync-once", "--fake", "--config", str(config_path), "--db", str(db_path)])

    assert rc == 0
    # local write 먼저 — DB 에 FakeTier 의 3개 GPU 가 적재됨.
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM gpu_sample").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM gpu_device").fetchone()[0] == 3
    finally:
        conn.close()
    # push payload 는 contract 모양.
    payload = pushed["payload"]
    assert payload["schemaVersion"] == "availability.snapshot.v1"
    assert payload["host"]["hostId"] == "host-123"
    assert len(payload["gpus"]) == 3
    assert "pushed 3 GPUs" in capsys.readouterr().out


def test_sync_once_push_failure_keeps_local_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "cloud.json"
    save_cloud_config(_config(), config_path)
    db_path = tmp_path / "gua.db"

    def fake_post(*_args: Any, **_kw: Any) -> dict[str, Any]:
        raise CloudError("could not reach GUA Board server: connection refused")

    monkeypatch.setattr("gpu_usage_audit.__main__.post_observation", fake_post)

    rc = gua_main(["sync-once", "--fake", "--config", str(config_path), "--db", str(db_path)])

    assert rc == 1
    err = capsys.readouterr().err
    assert "local snapshot saved" in err
    assert "cloud push failed" in err
    # push 가 실패해도 local write 는 보존된다.
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM gpu_sample").fetchone()[0] == 3
    finally:
        conn.close()


def test_sync_once_unbuildable_payload_keeps_local_write_without_pushing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "cloud.json"
    save_cloud_config(_config(), config_path)
    db_path = tmp_path / "gua.db"

    class _BadTier:
        # name/memory_total 없는 GPU → payload builder 가 ValueError.
        def probe(self) -> str:
            return "560.x-fake"

        def collect(self, _ts: object) -> Snapshot:
            return Snapshot(gpus=[GPUSample(uuid="GPU-0", util_pct=0)])

    monkeypatch.setattr("gpu_usage_audit.__main__.FakeTier", _BadTier)

    posted = False

    def fake_post(*_args: Any, **_kw: Any) -> dict[str, Any]:
        nonlocal posted
        posted = True
        return {}

    monkeypatch.setattr("gpu_usage_audit.__main__.post_observation", fake_post)

    rc = gua_main(["sync-once", "--fake", "--config", str(config_path), "--db", str(db_path)])

    assert rc == 1
    assert posted is False  # 빌드 실패 시 push 시도 안 함.
    err = capsys.readouterr().err
    assert "local snapshot saved" in err
    assert "could not build a valid payload" in err
    # local write 는 빌드 실패와 무관하게 보존된다.
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM gpu_sample").fetchone()[0] == 1
    finally:
        conn.close()


def test_sync_once_without_enrollment_exits_2(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = gua_main(
        [
            "sync-once",
            "--fake",
            "--config",
            str(tmp_path / "absent.json"),
            "--db",
            str(tmp_path / "gua.db"),
        ]
    )
    assert rc == 2
    assert "run `gua enroll`" in capsys.readouterr().err

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
from gpu_usage_audit.nvml import NVMLNotAvailableError


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


def test_sync_once_emits_partial_when_process_list_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "cloud.json"
    save_cloud_config(_config(), config_path)
    db_path = tmp_path / "gua.db"

    class _PartialTier:
        # core GPU metric 은 수집했지만 한 카드의 process list 가 권한 부족.
        def probe(self) -> str:
            return "560.35.05"

        def collect(self, _ts: object) -> Snapshot:
            return Snapshot(
                gpus=[GPUSample(uuid="GPU-0", util_pct=10, index=0, name="GPU", memory_total_mb=1000)]
            )

        @property
        def last_process_list_unavailable(self) -> bool:
            return True

        def close(self) -> None:
            pass

    monkeypatch.setattr("gpu_usage_audit.__main__.NVMLTier", _PartialTier)
    pushed: dict[str, Any] = {}

    def fake_post(config: CloudConfig, payload: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        pushed["payload"] = payload
        return {}

    monkeypatch.setattr("gpu_usage_audit.__main__.post_observation", fake_post)

    # --fake 없이 실 NVML 경로를 탄다 (위에서 NVMLTier 를 monkeypatch).
    rc = gua_main(["sync-once", "--config", str(config_path), "--db", str(db_path)])

    assert rc == 0
    payload = pushed["payload"]
    assert payload["collectionStatus"] == "partial"
    assert payload["errors"] == ["process_list_unavailable"]
    # 핵심: GPU 데이터는 그대로 push 됐다.
    assert len(payload["gpus"]) == 1
    out = capsys.readouterr().out
    assert "partial: process_list_unavailable" in out
    # local write 도 보존.
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM gpu_sample").fetchone()[0] == 1
    finally:
        conn.close()


def test_sync_once_pushes_error_heartbeat_when_nvml_init_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "cloud.json"
    save_cloud_config(_config(), config_path)
    db_path = tmp_path / "gua.db"

    class _DeadTier:
        # 드라이버를 잃은 host — NVML init 자체가 실패.
        def probe(self) -> str:
            raise NVMLNotAvailableError("the NVIDIA driver is not loaded")

        def collect(self, _ts: object) -> Snapshot:  # pragma: no cover - 도달 안 함
            raise AssertionError("collect must not run after probe failure")

        def close(self) -> None:
            pass

    monkeypatch.setattr("gpu_usage_audit.__main__.NVMLTier", _DeadTier)
    pushed: dict[str, Any] = {}

    def fake_post(config: CloudConfig, payload: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        pushed["payload"] = payload
        return {}

    monkeypatch.setattr("gpu_usage_audit.__main__.post_observation", fake_post)

    rc = gua_main(["sync-once", "--config", str(config_path), "--db", str(db_path)])

    # error heartbeat 는 보냈지만 수집 실패라 non-zero exit.
    assert rc == 1
    payload = pushed["payload"]
    assert payload["collectionStatus"] == "error"
    assert payload["errors"] == ["nvml_init_failed"]
    assert payload["gpus"] == []
    err = capsys.readouterr().err
    assert "pushed error heartbeat" in err
    assert "driver is not loaded" in err
    # 데이터가 없으므로 local DB 는 쓰지 않는다.
    assert not db_path.exists()


def test_sync_once_error_heartbeat_push_failure_reports_both(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "cloud.json"
    save_cloud_config(_config(), config_path)

    class _DeadTier:
        def probe(self) -> str:
            raise NVMLNotAvailableError("the NVIDIA driver is not loaded")

        def collect(self, _ts: object) -> Snapshot:  # pragma: no cover
            raise AssertionError

        def close(self) -> None:
            pass

    monkeypatch.setattr("gpu_usage_audit.__main__.NVMLTier", _DeadTier)

    def fake_post(*_args: Any, **_kw: Any) -> dict[str, Any]:
        raise CloudError("could not reach GUA Board server: connection refused")

    monkeypatch.setattr("gpu_usage_audit.__main__.post_observation", fake_post)

    rc = gua_main(["sync-once", "--config", str(config_path), "--db", str(tmp_path / "gua.db")])

    assert rc == 1
    err = capsys.readouterr().err
    assert "error heartbeat push also failed" in err


def test_sync_once_creates_missing_db_parent_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "cloud.json"
    save_cloud_config(_config(), config_path)
    db_path = tmp_path / "nested" / "dir" / "gua.db"  # 부모 디렉토리 없음.

    monkeypatch.setattr("gpu_usage_audit.__main__.post_observation", lambda *a, **k: {})

    rc = gua_main(["sync-once", "--fake", "--config", str(config_path), "--db", str(db_path)])

    assert rc == 0
    assert db_path.exists()


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


# ── daemon --cloud ───────────────────────────────────────────────


def test_daemon_cloud_without_enrollment_exits_2(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # --cloud 인데 enroll 안 됨 → NVML 열기 *전에* 설정 검증 실패로 종료.
    rc = gua_main(
        [
            "daemon",
            "--foreground",
            "--cloud",
            "--db",
            str(tmp_path / "gua.db"),
            "--config",
            str(tmp_path / "absent.json"),
        ]
    )
    assert rc == 2
    assert "run `gua enroll`" in capsys.readouterr().err


def test_daemon_start_propagates_cloud_flags_to_background(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # 백그라운드 spawn 커맨드에 --cloud/--config 가 실려야 데몬이 push 한다.
    captured: dict[str, Any] = {}

    class FakePopen:
        def __init__(self, command: list[str], **_kwargs: Any) -> None:
            captured["command"] = command
            self.pid = 4242

        def poll(self) -> int | None:
            return None

    monkeypatch.setattr("gpu_usage_audit.__main__.subprocess.Popen", FakePopen)
    monkeypatch.setattr("gpu_usage_audit.__main__.time.sleep", lambda *_a, **_k: None)

    config_path = tmp_path / "cloud.json"
    rc = gua_main(
        [
            "daemon",
            "--cloud",
            "--db",
            str(tmp_path / "gua.db"),
            "--config",
            str(config_path),
            "--pid-file",
            str(tmp_path / "daemon.pid"),
            "--log-file",
            str(tmp_path / "daemon.log"),
        ]
    )
    assert rc == 0
    command = captured["command"]
    assert "--cloud" in command
    assert "--config" in command
    assert str(config_path) in command

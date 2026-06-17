"""cloud.config — CloudConfig 저장/로드/검증 + 0600 권한."""

from __future__ import annotations

from pathlib import Path
from stat import S_IMODE

import pytest

from gpu_usage_audit.cloud.config import (
    CloudConfig,
    CloudConfigError,
    load_cloud_config,
    normalize_server_url,
    save_cloud_config,
)


def _config() -> CloudConfig:
    return CloudConfig(
        server_url="https://board.example.com",
        host_id="host-123",
        display_name="a6000-01",
        agent_token="gua_agent_secret",
        token_prefix="gua_agent_xx",
    )


def test_save_and_load_round_trips_and_is_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "cloud.json"
    saved = save_cloud_config(_config(), path)

    assert saved == path
    assert load_cloud_config(path) == _config()
    assert S_IMODE(path.stat().st_mode) == 0o600


def test_save_requires_force_to_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "cloud.json"
    save_cloud_config(_config(), path)

    with pytest.raises(CloudConfigError):
        save_cloud_config(_config(), path)

    save_cloud_config(_config(), path, overwrite=True)


def test_load_missing_config_points_at_enroll(tmp_path: Path) -> None:
    with pytest.raises(CloudConfigError, match="run `gua enroll`"):
        load_cloud_config(tmp_path / "absent.json")


def test_from_dict_rejects_missing_fields() -> None:
    with pytest.raises(CloudConfigError, match="missing fields"):
        CloudConfig.from_dict({"serverUrl": "https://board.example.com"})


def test_normalize_server_url_requires_absolute_http() -> None:
    assert normalize_server_url("https://board.example.com/") == "https://board.example.com"
    with pytest.raises(CloudConfigError):
        normalize_server_url("board.example.com")

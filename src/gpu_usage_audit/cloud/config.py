"""Cloud sync 설정 영속화 — `~/.gua/cloud.json`.

enrollment 으로 받은 host-scoped agent token 을 담는다. token 은 secret 이므로
파일은 owner-only(0600) 로, atomic replace 로 저장한다. 이 설정은 local
telemetry DB(gua.db) 와 분리된다 — cloud 링크와 로컬 수집의 관심사가 다르다.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..paths import DEFAULT_CLOUD_CONFIG_PATH, expand_path


class CloudConfigError(Exception):
    """Cloud 설정 읽기/쓰기/검증 실패. 사용자 facing 메시지로도 사용."""


@dataclass(frozen=True)
class CloudConfig:
    server_url: str
    host_id: str
    display_name: str
    agent_token: str
    token_prefix: str

    def to_dict(self) -> dict[str, str]:
        return {
            "serverUrl": self.server_url,
            "hostId": self.host_id,
            "displayName": self.display_name,
            "agentToken": self.agent_token,
            "tokenPrefix": self.token_prefix,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CloudConfig:
        required = ["serverUrl", "hostId", "displayName", "agentToken", "tokenPrefix"]
        missing = [key for key in required if not isinstance(data.get(key), str) or not data[key]]
        if missing:
            raise CloudConfigError(f"cloud config missing fields: {', '.join(missing)}")
        return cls(
            server_url=normalize_server_url(data["serverUrl"]),
            host_id=data["hostId"],
            display_name=data["displayName"],
            agent_token=data["agentToken"],
            token_prefix=data["tokenPrefix"],
        )


def normalize_server_url(value: str) -> str:
    """trailing slash 제거 + 절대 http(s) URL 검증."""
    stripped = value.strip().rstrip("/")
    parsed = urlparse(stripped)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CloudConfigError("server URL must be an absolute http(s) URL")
    return stripped


def load_cloud_config(path: str | Path = DEFAULT_CLOUD_CONFIG_PATH) -> CloudConfig:
    config_path = expand_path(path)
    try:
        data = json.loads(config_path.read_text())
    except FileNotFoundError as exc:
        raise CloudConfigError(
            f"cloud config not found: {config_path}; run `gua enroll` first"
        ) from exc
    except json.JSONDecodeError as exc:
        raise CloudConfigError(f"cloud config is not valid JSON: {config_path}") from exc

    if not isinstance(data, dict):
        raise CloudConfigError("cloud config root must be an object")
    return CloudConfig.from_dict(data)


def save_cloud_config(
    config: CloudConfig,
    path: str | Path = DEFAULT_CLOUD_CONFIG_PATH,
    *,
    overwrite: bool = False,
) -> Path:
    """0600 으로 atomic 저장. 기존 파일은 overwrite=True 일 때만 덮어쓴다."""
    config_path = expand_path(path)
    if config_path.exists() and not overwrite:
        raise CloudConfigError(
            f"cloud config already exists: {config_path}; pass --force to overwrite"
        )

    config_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = config_path.with_name(f".{config_path.name}.tmp")
    temp_path.write_text(json.dumps(config.to_dict(), indent=2, sort_keys=True) + "\n")
    os.chmod(temp_path, 0o600)
    temp_path.replace(config_path)
    os.chmod(config_path, 0o600)
    return config_path

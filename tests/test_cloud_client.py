"""cloud.client — urllib claim/observation (urlopen 모킹). token 미노출 검증."""

from __future__ import annotations

import io
import json
from typing import Any
from urllib.error import HTTPError, URLError

import pytest

from gpu_usage_audit.cloud.client import CloudError, claim_enrollment, post_observation
from gpu_usage_audit.cloud.config import CloudConfig


class _FakeResponse:
    def __init__(self, payload: dict[str, Any] | None) -> None:
        self._payload = payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return b"" if self._payload is None else json.dumps(self._payload).encode("utf-8")


def test_claim_enrollment_posts_and_returns_config(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[Any, float]] = []

    def fake_urlopen(request: Any, *, timeout: float) -> _FakeResponse:
        captured.append((request, timeout))
        return _FakeResponse(
            {
                "hostId": "host-123",
                "displayName": "a6000-01",
                "agentToken": "gua_agent_secret",
                "tokenPrefix": "gua_agent_xx",
            }
        )

    monkeypatch.setattr("gpu_usage_audit.cloud.client.urlopen", fake_urlopen)

    config = claim_enrollment(
        server_url="https://board.example.com/",
        enrollment_token="  enroll-secret  ",
        hostname="gpu-01",
        agent_version="1.1.0",
        driver_version="560.35.05",
        timeout=3,
    )

    request, timeout = captured[0]
    assert request.full_url == "https://board.example.com/agent/v1/enrollments/claim"
    assert timeout == 3
    assert json.loads(request.data.decode("utf-8")) == {
        "enrollmentToken": "enroll-secret",
        "hostname": "gpu-01",
        "agentVersion": "1.1.0",
        "driverVersion": "560.35.05",
    }
    assert config == CloudConfig(
        server_url="https://board.example.com",
        host_id="host-123",
        display_name="a6000-01",
        agent_token="gua_agent_secret",
        token_prefix="gua_agent_xx",
    )


def test_claim_enrollment_http_error_does_not_leak_token(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: Any, *, timeout: float) -> _FakeResponse:
        raise HTTPError(
            request.full_url,
            401,
            "Unauthorized",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b'{"detail":"invalid or expired enrollment token"}'),
        )

    monkeypatch.setattr("gpu_usage_audit.cloud.client.urlopen", fake_urlopen)

    with pytest.raises(CloudError) as err:
        claim_enrollment(
            server_url="https://board.example.com",
            enrollment_token="secret-token-must-not-leak",
        )

    message = str(err.value)
    assert "HTTP 401" in message
    assert "invalid or expired enrollment token" in message
    assert "secret-token-must-not-leak" not in message


def test_claim_enrollment_network_error_is_friendly(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: Any, *, timeout: float) -> _FakeResponse:
        raise URLError("connection refused")

    monkeypatch.setattr("gpu_usage_audit.cloud.client.urlopen", fake_urlopen)

    with pytest.raises(CloudError, match="could not reach GUA Board server"):
        claim_enrollment(
            server_url="https://board.example.com",
            enrollment_token="enroll-secret",
        )


def test_post_observation_sends_bearer_and_parses_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[Any] = []

    def fake_urlopen(request: Any, *, timeout: float) -> _FakeResponse:
        captured.append(request)
        return _FakeResponse({"status": "accepted", "gpuSlotsSeen": 2})

    monkeypatch.setattr("gpu_usage_audit.cloud.client.urlopen", fake_urlopen)

    config = CloudConfig(
        server_url="https://board.example.com",
        host_id="host-123",
        display_name="a6000-01",
        agent_token="gua_agent_secret",
        token_prefix="gua_agent_xx",
    )
    result = post_observation(config, {"schemaVersion": "availability.snapshot.v1"})

    request = captured[0]
    assert request.full_url == "https://board.example.com/agent/v1/observations"
    assert request.get_header("Authorization") == "Bearer gua_agent_secret"
    assert result == {"status": "accepted", "gpuSlotsSeen": 2}


def test_post_observation_http_error_does_not_leak_agent_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(request: Any, *, timeout: float) -> _FakeResponse:
        raise HTTPError(
            request.full_url,
            401,
            "Unauthorized",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b"{}"),
        )

    monkeypatch.setattr("gpu_usage_audit.cloud.client.urlopen", fake_urlopen)

    config = CloudConfig(
        server_url="https://board.example.com",
        host_id="host-123",
        display_name="a6000-01",
        agent_token="gua_agent_secret",
        token_prefix="gua_agent_xx",
    )
    with pytest.raises(CloudError) as err:
        post_observation(config, {"schemaVersion": "availability.snapshot.v1"})
    assert "gua_agent_secret" not in str(err.value)

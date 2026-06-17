"""GUA Board agent HTTP client — stdlib urllib (신규 런타임 의존성 0).

두 endpoint 만 호출한다:
  POST /agent/v1/enrollments/claim   (one-time enrollment token → agent token)
  POST /agent/v1/observations        (Bearer agent token, latest snapshot)

에러 메시지에는 *어떤 token 원문도* 넣지 않는다 — 서버 응답의 detail 과
HTTP status 만 노출한다.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import CloudConfig, CloudConfigError, normalize_server_url


class CloudError(Exception):
    """Cloud 통신 실패. 사용자 facing 메시지로 사용 (token 미포함)."""


def claim_enrollment(
    *,
    server_url: str,
    enrollment_token: str,
    hostname: str | None = None,
    agent_version: str | None = None,
    driver_version: str | None = None,
    timeout: float = 10.0,
) -> CloudConfig:
    """enrollment token 을 claim 해 host-scoped agent token 을 받는다."""
    try:
        base = normalize_server_url(server_url)
    except CloudConfigError as exc:
        raise CloudError(str(exc)) from exc

    token = enrollment_token.strip()
    if not token:
        raise CloudError("enrollment token cannot be blank")

    payload: dict[str, str] = {"enrollmentToken": token}
    for key, value in (
        ("hostname", hostname),
        ("agentVersion", agent_version),
        ("driverVersion", driver_version),
    ):
        text = _optional_text(value)
        if text is not None:
            payload[key] = text

    body = _post_json(f"{base}/agent/v1/enrollments/claim", payload, timeout=timeout)
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise CloudError("GUA Board server returned invalid JSON") from exc

    try:
        return CloudConfig(
            server_url=base,
            host_id=_required_str(data, "hostId"),
            display_name=_required_str(data, "displayName"),
            agent_token=_required_str(data, "agentToken"),
            token_prefix=_required_str(data, "tokenPrefix"),
        )
    except CloudConfigError as exc:
        raise CloudError(str(exc)) from exc


def post_observation(
    config: CloudConfig,
    payload: dict[str, Any],
    *,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """latest snapshot 을 host-scoped agent token 으로 push 한다."""
    body = _post_json(
        f"{config.server_url}/agent/v1/observations",
        payload,
        timeout=timeout,
        headers={"Authorization": f"Bearer {config.agent_token}"},
    )
    if not body:
        return {}
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise CloudError("GUA Board server returned invalid JSON") from exc
    return parsed if isinstance(parsed, dict) else {}


def _post_json(
    url: str,
    payload: dict[str, Any],
    *,
    timeout: float,
    headers: dict[str, str] | None = None,
) -> str:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            **(headers or {}),
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body: str = response.read().decode("utf-8")
            return body
    except HTTPError as exc:
        raise CloudError(_http_error_message(exc)) from exc
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise CloudError(f"could not reach GUA Board server: {reason}") from exc


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _required_str(data: object, key: str) -> str:
    if isinstance(data, dict):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    raise CloudConfigError(f"server response missing field: {key}")


def _http_error_message(error: HTTPError) -> str:
    detail = ""
    try:
        body = json.loads(error.read().decode("utf-8"))
        if isinstance(body, dict) and isinstance(body.get("detail"), str):
            detail = f": {body['detail']}"
    except Exception:
        detail = ""
    return f"GUA Board request failed with HTTP {error.code}{detail}"

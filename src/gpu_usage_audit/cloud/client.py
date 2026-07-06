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


def list_archives(
    config: CloudConfig,
    *,
    timeout: float = 10.0,
) -> list[tuple[str, str]]:
    """이 host 가 이미 저장한 (date, table) 목록 — 없는 것만 올리기 위한 diff 용."""
    body = _request(
        f"{config.server_url}/agent/v1/archives",
        method="GET",
        timeout=timeout,
        headers={"Authorization": f"Bearer {config.agent_token}"},
    )
    try:
        data = json.loads(body) if body else {}
    except json.JSONDecodeError as exc:
        raise CloudError("GUA Board server returned invalid JSON") from exc
    archives = data.get("archives", []) if isinstance(data, dict) else []
    return [
        (a["date"], a["table"])
        for a in archives
        if isinstance(a, dict) and "date" in a and "table" in a
    ]


def put_archive(
    config: CloudConfig,
    *,
    day: str,
    table: str,
    data: bytes,
    timeout: float = 60.0,
) -> None:
    """하루치 gzip CSV 를 board 로 올린다 (board 가 object storage 에 씀)."""
    _request(
        f"{config.server_url}/agent/v1/archives?date={day}&table={table}",
        method="POST",
        data=data,
        timeout=timeout,
        headers={
            "Authorization": f"Bearer {config.agent_token}",
            "Content-Type": "application/gzip",
        },
    )


def prune_archives(
    config: CloudConfig,
    *,
    retention_days: int,
    timeout: float = 10.0,
) -> None:
    """보존 창(일)을 넘은 이 host 의 아카이브를 board 가 지우게 한다."""
    _request(
        f"{config.server_url}/agent/v1/archives?olderThanDays={retention_days}",
        method="DELETE",
        timeout=timeout,
        headers={"Authorization": f"Bearer {config.agent_token}"},
    )


def _request(
    url: str,
    *,
    method: str,
    data: bytes | None = None,
    timeout: float,
    headers: dict[str, str] | None = None,
) -> str:
    request = Request(
        url,
        data=data,
        headers={"Accept": "application/json", **(headers or {})},
        method=method,
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


def _post_json(
    url: str,
    payload: dict[str, Any],
    *,
    timeout: float,
    headers: dict[str, str] | None = None,
) -> str:
    return _request(
        url,
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        timeout=timeout,
        headers={"Content-Type": "application/json", **(headers or {})},
    )


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

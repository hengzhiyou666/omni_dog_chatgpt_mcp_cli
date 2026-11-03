"""Read-only adapter for omni-inspection's authenticated HTTP API.

Cloud database identifiers and robot-local asset names are different namespaces.
This module preserves both and never sends a command to the robot.
"""

from __future__ import annotations

import json
import math
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import httpx

from omni_tools.config import Settings
from omni_tools.errors import OmniError


_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_ROUTES = 10_000


def _invalid_response() -> OmniError:
    return OmniError("upstream_invalid_response", "Backend returned an invalid response.")


def _api_base(value: str) -> str:
    """Accept a server URL, optional proxy prefix, and optional /api/v1 suffix."""
    try:
        if not isinstance(value, str) or not value or any(c.isspace() for c in value):
            raise ValueError
        if "\\" in value or any(ord(c) < 32 for c in value):
            raise ValueError
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError
        if parsed.username is not None or parsed.password is not None:
            raise ValueError
        if parsed.query or parsed.fragment or "?" in value or "#" in value:
            raise ValueError
        _ = parsed.port
        path = parsed.path.rstrip("/")
        if any(unquote(segment) in {".", ".."} for segment in path.split("/")):
            raise ValueError
        if "//" in path or "%" in path:
            raise ValueError
        if "/api/v1" in path:
            if not path.endswith("/api/v1") or path.count("/api/v1") != 1:
                raise ValueError
        else:
            path += "/api/v1"
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    except (ValueError, UnicodeError):
        raise OmniError(
            "configuration_error",
            "Backend URL must be an HTTP(S) server URL with an optional path prefix, "
            "without credentials, query, or fragment.",
        ) from None


def _segment(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or value in {".", ".."}:
        raise OmniError("invalid_argument", "A nonempty resource identifier is required.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise OmniError("invalid_argument", "Resource identifiers cannot contain control characters.")
    try:
        return quote(value, safe="")
    except UnicodeError:
        raise OmniError("invalid_argument", "Resource identifier is not valid Unicode.") from None


def _finite(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_finite(item) for item in value)
    if isinstance(value, dict):
        return all(_finite(item) for item in value.values())
    return True


def _constant(_: str) -> None:
    raise ValueError("non-finite JSON")


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _invalid_response()
    return value


def _objects(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise _invalid_response()
    return value


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid_response()
    return value


class HTTPBackend:
    """Synchronous GET-only backend; an injected client remains caller-owned."""

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.api_base = _api_base(settings.base_url)
        self._owns_client = client is None
        self._client = client or httpx.Client(follow_redirects=False, trust_env=False)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> HTTPBackend:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            response = self._client.get(
                self.api_base + path,
                params=params,
                headers={"Authorization": "Bearer " + self.settings.token, "Accept": "application/json"},
                timeout=self.settings.timeout,
                follow_redirects=False,
            )
        except httpx.TimeoutException:
            raise OmniError("upstream_timeout", "Backend request timed out.") from None
        except (httpx.RequestError, httpx.InvalidURL, ValueError):
            # Exception text can contain URLs, tokens or other confidential values.
            raise OmniError("upstream_unavailable", "Backend could not be reached.") from None

        envelope: Any = None
        if len(response.content) <= _MAX_RESPONSE_BYTES:
            try:
                envelope = json.loads(response.content, parse_constant=_constant)
                if not _finite(envelope):
                    envelope = None
            except (ValueError, UnicodeError, RecursionError):
                envelope = None
        if not 200 <= response.status_code < 300:
            details: dict[str, Any] = {"http_status": response.status_code}
            if isinstance(envelope, dict) and type(envelope.get("code")) is int:
                details["upstream_code"] = envelope["code"]
            raise OmniError("upstream_http_error", "Backend returned an unsuccessful HTTP status.", details)
        if not isinstance(envelope, dict) or type(envelope.get("code")) is not int:
            raise _invalid_response()
        if envelope["code"] != 0:
            raise OmniError(
                "upstream_rejected", "Backend rejected the request.",
                {"http_status": response.status_code, "upstream_code": envelope["code"]},
            )
        return envelope.get("data")

    def get_robot_status(self, robot_id: str) -> dict[str, Any]:
        state = _object(self._get(f"/robots/{_segment(robot_id)}/status"))
        self._check_robot(state, robot_id)
        return state

    @staticmethod
    def _check_robot(value: dict[str, Any], robot_id: str) -> None:
        if _identifier(value.get("robot_id")) != robot_id:
            raise OmniError("robot_mismatch", "Backend resource belongs to a different robot.")

    def list_maps(self, robot_id: str) -> dict[str, Any]:
        data = _object(self._get(f"/robots/{_segment(robot_id)}/maps"))
        maps = _objects(data.get("maps"))
        if type(data.get("total")) is not int or data["total"] != len(maps):
            raise _invalid_response()
        for item in maps:
            self._check_robot(item, robot_id)
            _identifier(item.get("map_id"))
        return {
            **data,
            "robot_id": robot_id,
            "maps": [{**item, "cloud_map_id": item["map_id"]} for item in maps],
            "identity_note": "map_id/cloud_map_id identify the cloud record; robot_map_id is the robot-local name.",
        }

    def list_routes(self, robot_id: str) -> dict[str, Any]:
        # The upstream routes endpoint is global and ignores robot_id filters.
        # Join its cloud map_id to this robot's map catalog, without claiming
        # robot-local route presence, freshness, or dispatchability.
        maps = self.list_maps(robot_id)["maps"]
        map_ids = {item["map_id"] for item in maps}
        routes: list[dict[str, Any]] = []
        offset = 0
        total = 0
        while True:
            page = _object(self._get("/patrol/routes", {"limit": 500, "offset": offset}))
            batch = _objects(page.get("routes"))
            total = page.get("total")
            if type(total) is not int or total < 0 or total > _MAX_ROUTES:
                raise _invalid_response()
            if len(batch) > 500 or (not batch and offset < total):
                raise _invalid_response()
            for route in batch:
                _identifier(route.get("route_id"))
                map_id = route.get("map_id")
                if map_id is not None and not isinstance(map_id, str):
                    raise _invalid_response()
                if map_id in map_ids:
                    routes.append({**route, "cloud_route_id": route["route_id"]})
            offset += len(batch)
            if offset >= total:
                break
            if offset >= _MAX_ROUTES:
                raise _invalid_response()
        return {
            "robot_id": robot_id,
            "routes": routes,
            "total": len(routes),
            "upstream_total": total,
            "scope": "cloud_map_association",
            "scope_note": "Filtered by this robot's cloud map catalog. Robot-local route presence and dispatchability are not verified.",
            "identity_note": "route_id/cloud_route_id identify the cloud record; robot_route_id is the robot-local name.",
        }

    def get_task(self, task_id: str, robot_id: str) -> dict[str, Any]:
        _segment(robot_id)
        task = _object(self._get(f"/patrol/tasks/{_segment(task_id)}"))
        self._check_robot(task, robot_id)
        if _identifier(task.get("task_id")) != task_id:
            raise _invalid_response()
        return task

    def get_inspection_result(self, task_id: str, robot_id: str) -> dict[str, Any]:
        task = self.get_task(task_id, robot_id)
        path = f"/patrol/tasks/{_segment(task_id)}"
        events = _objects(self._get(path + "/events"))
        evidence = _objects(self._get(path + "/evidence"))
        for item in events + evidence:
            if _identifier(item.get("task_id")) != task_id:
                raise _invalid_response()
            if "robot_id" in item:
                self._check_robot(item, robot_id)
        return {"task": task, "events": events, "evidence": evidence}

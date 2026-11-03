"""Adapter for omni-inspection's authenticated HTTP API.

Cloud database identifiers and robot-local asset names are different namespaces.
This module preserves both. Writes require the explicit execution policy.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import httpx

from omni_tools.config import Settings
from omni_tools.errors import OmniError
from omni_tools.policy import require_write
from omni_tools.util import identifier


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
    """Synchronous backend; an injected client remains caller-owned."""

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
        return self._request("GET", path, params=params)

    def _request(
        self, method: str, path: str, *, params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        # No retry middleware: a timed-out write may already have taken effect.
        try:
            response = self._client.request(
                method,
                self.api_base + path,
                params=params,
                json=body,
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

    @staticmethod
    def _returned_id(value: Any) -> str:
        try:
            return identifier(_identifier(value))
        except OmniError:
            raise _invalid_response() from None

    def _write_error(
        self, error: OmniError, phase: str, robot_id: str, *,
        task_id: str | None = None, plan_id: str | None = None,
        goal_id: str | None = None, navigation: bool = False,
    ) -> OmniError:
        """Preserve partial operation identity without replaying a mutation."""
        if goal_id:
            recovery = {"method": "GET", "path": f"/api/v1/robots/{_segment(robot_id)}/navigation/goals/{_segment(goal_id)}"}
        elif plan_id:
            recovery = {"method": "GET", "path": f"/api/v1/robots/{_segment(robot_id)}/navigation/plans/{_segment(plan_id)}"}
            if phase == "submit_navigation_goal":
                recovery["note"] = (
                    "A plan lookup does not confirm execution. Inspect robot state or the operator console; "
                    "do not automatically submit the plan again."
                )
                recovery["also"] = {"method": "GET", "path": f"/api/v1/robots/{_segment(robot_id)}/status"}
        elif task_id:
            recovery = {"method": "GET", "path": f"/api/v1/patrol/tasks/{_segment(task_id)}"}
        elif navigation:
            recovery = {
                "method": "GET", "path": f"/api/v1/robots/{_segment(robot_id)}/status",
                "note": "No plan identifier was confirmed. Inspect state; do not automatically recreate or execute a plan.",
            }
        else:
            recovery = {"method": "GET", "path": "/api/v1/patrol/tasks", "params": {"robot_id": robot_id}}
        details = {
            **error.details, "phase": phase, "robot_id": robot_id,
            "outcome_unknown": True, "recover_by_get": recovery,
            "retry_writes": False,
        }
        for name, value in (("task_id", task_id), ("plan_id", plan_id), ("goal_id", goal_id)):
            if value is not None:
                details[name] = value

        def redact(value: Any) -> Any:
            if isinstance(value, str) and self.settings.token:
                return value.replace(self.settings.token, "[redacted]")
            if isinstance(value, dict):
                return {key: redact(item) for key, item in value.items()}
            return value

        return OmniError(error.code, error.message, redact(details))

    def start_inspection(self, route_id: str, robot_id: str) -> dict[str, Any]:
        require_write(self.settings, robot_id)
        route_id = identifier(route_id, "route_id")
        robot_id = identifier(robot_id, "robot_id")
        task_id = None
        phase = "create_inspection_task"
        try:
            task = _object(self._request("POST", "/patrol/tasks", body={
                "route_id": route_id, "robot_id": robot_id, "task_type": "manual", "priority": 0,
            }))
            self._check_robot(task, robot_id)
            task_id = self._returned_id(task.get("task_id"))
            if task.get("route_id") != route_id or task.get("status") != "pending":
                raise _invalid_response()
            phase = "start_inspection_task"
            started = _object(self._request("POST", f"/patrol/tasks/{_segment(task_id)}/start"))
            self._check_robot(started, robot_id)
            if (started.get("task_id") != task_id or started.get("route_id") != route_id
                    or not isinstance(started.get("status"), str) or not started["status"]):
                raise _invalid_response()
            return started
        except OmniError as error:
            raise self._write_error(error, phase, robot_id, task_id=task_id) from None

    @staticmethod
    def _coordinate(value: Any) -> float:
        try:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError
            number = float(value)
            if not math.isfinite(number):
                raise ValueError
            return number
        except (ValueError, TypeError, OverflowError):
            raise OmniError("invalid_argument", "Navigation coordinates and yaw must be finite numbers.") from None

    @staticmethod
    def _check_plan(
        plan: dict[str, Any], map_id: str, map_version: int, pose: dict[str, Any],
    ) -> None:
        if plan.get("map_id") != map_id or type(plan.get("map_version")) is not int or plan["map_version"] != map_version:
            raise OmniError("map_mismatch", "Backend navigation plan does not match the requested map.")
        if plan.get("status") != "ready" or plan.get("executable") is not True:
            raise OmniError("plan_not_executable", "Backend navigation plan is not executable.")
        try:
            expires_at = datetime.fromisoformat(plan["expires_at"].replace("Z", "+00:00"))
            if expires_at.tzinfo is None:
                raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError):
            raise _invalid_response() from None
        if expires_at <= datetime.now(timezone.utc):
            raise OmniError("plan_expired", "Backend navigation plan has expired.")
        actual = _object(plan.get("goal_pose"))
        for group, fields in pose.items():
            values = _object(actual.get(group))
            for key, expected in fields.items():
                value = values.get(key)
                try:
                    matches = (type(value) in (int, float) and math.isfinite(value)
                               and math.isclose(value, expected, rel_tol=1e-9, abs_tol=1e-9))
                except OverflowError:
                    matches = False
                if not matches:
                    raise OmniError("goal_mismatch", "Backend navigation plan does not match the requested destination.")

    def navigate_to(
        self, map_id: str, map_version: int, x: float, y: float, yaw: float, robot_id: str,
    ) -> dict[str, Any]:
        require_write(self.settings, robot_id)
        robot_id = identifier(robot_id, "robot_id")
        map_id = identifier(map_id, "map_id")
        if type(map_version) is not int or not 1 <= map_version <= 4294967295:
            raise OmniError("invalid_argument", "map_version must be an integer between 1 and 4294967295.")
        x, y, yaw = self._coordinate(x), self._coordinate(y), self._coordinate(yaw)
        pose = {
            "position": {"x": x, "y": y, "z": 0.0},
            "orientation": {"x": 0.0, "y": 0.0, "z": math.sin(yaw / 2), "w": math.cos(yaw / 2)},
        }
        base = f"/robots/{_segment(robot_id)}/navigation"
        phase = "create_navigation_plan"
        plan_id = goal_id = None
        try:
            plan = _object(self._request("POST", base + "/plans", body={
                "map_id": map_id, "map_version": map_version, "goal_pose": pose,
            }))
            self._check_robot(plan, robot_id)
            plan_id = self._returned_id(plan.get("plan_id"))
            self._check_plan(plan, map_id, map_version, pose)
            phase = "submit_navigation_goal"
            goal = _object(self._request("POST", base + "/goals", body={"plan_id": plan_id}))
            self._check_robot(goal, robot_id)
            goal_id = self._returned_id(goal.get("goal_id"))
            if (goal.get("plan_id") != plan_id or goal.get("map_id") != map_id
                    or type(goal.get("map_version")) is not int or goal["map_version"] != map_version
                    or not isinstance(goal.get("status"), str) or not goal["status"]):
                raise _invalid_response()
            return goal
        except OmniError as error:
            raise self._write_error(
                error, phase, robot_id, plan_id=plan_id, goal_id=goal_id, navigation=True,
            ) from None

    def get_navigation_task(self, task_id: str, robot_id: str) -> dict[str, Any]:
        robot_id = identifier(robot_id, "robot_id")
        task_id = identifier(task_id, "task_id")
        goal = _object(self._get(f"/robots/{_segment(robot_id)}/navigation/goals/{_segment(task_id)}"))
        self._check_robot(goal, robot_id)
        if self._returned_id(goal.get("goal_id")) != task_id:
            raise _invalid_response()
        return goal

    def cancel_task(self, task_id: str, robot_id: str, kind: str = "inspection") -> dict[str, Any]:
        require_write(self.settings, robot_id)
        robot_id = identifier(robot_id, "robot_id")
        task_id = identifier(task_id, "task_id")
        if kind not in {"inspection", "navigation"}:
            raise OmniError("invalid_argument", "kind must be inspection or navigation.")
        if kind == "inspection":
            task = self.get_task(task_id, robot_id)
            if task.get("status") in {"completed", "failed", "cancelled"}:
                return {**task, "cancel_requested": False, "already_terminal": True}
            try:
                self._request("POST", f"/patrol/tasks/{_segment(task_id)}/cancel")
                current = self.get_task(task_id, robot_id)
                return {**current, "cancel_requested": True}
            except OmniError as error:
                raise self._write_error(error, "cancel_inspection_task", robot_id, task_id=task_id) from None
        goal = self.get_navigation_task(task_id, robot_id)
        if goal.get("status") in {"succeeded", "failed", "cancelled"}:
            return {**goal, "cancel_requested": False, "already_terminal": True}
        try:
            current = _object(self._request(
                "DELETE", f"/robots/{_segment(robot_id)}/navigation/goals/{_segment(task_id)}",
            ))
            self._check_robot(current, robot_id)
            if current.get("goal_id") != task_id or current.get("plan_id") != goal.get("plan_id"):
                raise _invalid_response()
            return {**current, "cancel_requested": True}
        except OmniError as error:
            raise self._write_error(error, "cancel_navigation_task", robot_id, goal_id=task_id, navigation=True) from None

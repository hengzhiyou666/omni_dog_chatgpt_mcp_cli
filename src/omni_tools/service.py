"""Common public operations used by CLI and MCP."""
from datetime import datetime, timezone
import math

from .config import Settings
from .errors import OmniError
from .policy import require_write
from .util import identifier, sanitize, task_identifier


class OmniService:
    def __init__(self, settings: Settings, backend=None):
        self.settings = settings
        if backend is None:
            if settings.backend == "ssh":
                from .backends.ssh import SSHBackend
                backend = SSHBackend(settings)
            else:
                from .backends.http import HTTPBackend
                backend = HTTPBackend(settings)
        self.backend = backend

    def _read(self, name, robot_id, *args):
        robot = identifier(self.settings.robot_id if robot_id is None else robot_id, "robot_id")
        data = getattr(self.backend, name)(*args, robot)
        return sanitize({"ok": True, "backend": self.settings.backend,
                         "robot_id": robot, "observed_at": datetime.now(timezone.utc).isoformat(),
                         "data": data})

    def get_robot_status(self, robot_id: str | None = None) -> dict:
        return self._read("get_robot_status", robot_id)

    def list_maps(self, robot_id: str | None = None) -> dict:
        return self._read("list_maps", robot_id)

    def list_routes(self, robot_id: str | None = None) -> dict:
        return self._read("list_routes", robot_id)

    def get_task(self, task_id: str, robot_id: str | None = None, kind: str = "inspection") -> dict:
        self._task_kind(kind)
        if kind == "navigation" and self.settings.backend == "http":
            return self._read("get_navigation_task", robot_id, task_identifier(task_id))
        return self._read("get_task", robot_id, task_identifier(task_id))

    def get_inspection_result(self, task_id: str, robot_id: str | None = None) -> dict:
        return self._read("get_inspection_result", robot_id, task_identifier(task_id))

    @staticmethod
    def _task_kind(kind):
        if not isinstance(kind, str) or kind not in {"inspection", "navigation"}:
            raise OmniError("invalid_argument", "Task kind must be inspection or navigation")

    def _mutation(self, name, robot_id, execute, parameters, args):
        robot = identifier(self.settings.robot_id if robot_id is None else robot_id, "robot_id")
        if type(execute) is not bool:
            raise OmniError("invalid_argument", "execute must be a boolean")
        if not execute:
            # Preview is local: no state reads, network calls or task allocation.
            data = {"operation": name, "executed": False, "preview": True,
                    "parameters": {"robot_id": robot, **parameters},
                    "execution_backend": "http",
                    "current_backend_supports_execution": self.settings.backend == "http",
                    "execution_requirements": ["HTTP backend", "OMNI_ALLOW_WRITES=1",
                                               "robot in OMNI_ALLOWED_ROBOTS", "execute=true"]}
        else:
            require_write(self.settings, robot)
            data = getattr(self.backend, name)(*args, robot)
            data = {**data, "executed": True,
                    "completion_note": "Request sent; use get_task for the confirmed outcome. Acceptance is not completion."}
        return sanitize({"ok": True, "backend": self.settings.backend, "robot_id": robot,
                         "observed_at": datetime.now(timezone.utc).isoformat(), "data": data})

    def start_inspection(self, route_id: str, robot_id: str | None = None, execute: bool = False) -> dict:
        route = identifier(route_id, "route_id")
        return self._mutation("start_inspection", robot_id, execute, {"route_id": route}, (route,))

    def navigate_to(self, map_id: str, map_version: int, x: float, y: float,
                    yaw: float = 0.0, robot_id: str | None = None, execute: bool = False) -> dict:
        map_id = identifier(map_id, "map_id")
        if type(map_version) is not int or not 1 <= map_version <= 4294967295:
            raise OmniError("invalid_argument", "map_version must be an integer from 1 to 4294967295")
        for value in (x, y, yaw):
            try:
                finite = type(value) in {int, float} and math.isfinite(value)
            except (OverflowError, ValueError):
                finite = False
            if not finite:
                raise OmniError("invalid_argument", "Coordinates and yaw must be finite numbers")
        params = {"map_id": map_id, "map_version": map_version, "x": x, "y": y, "yaw": yaw}
        return self._mutation("navigate_to", robot_id, execute, params, (map_id, map_version, x, y, yaw))

    def cancel_task(self, task_id: str, robot_id: str | None = None,
                    kind: str = "inspection", execute: bool = False) -> dict:
        task_id = identifier(task_id, "cloud task_id")
        self._task_kind(kind)
        robot = identifier(self.settings.robot_id if robot_id is None else robot_id, "robot_id")
        if type(execute) is not bool:
            raise OmniError("invalid_argument", "execute must be a boolean")
        if not execute:
            return self._mutation("cancel_task", robot, False,
                                  {"task_id": task_id, "kind": kind}, ())
        require_write(self.settings, robot)
        data = self.backend.cancel_task(task_id, robot, kind=kind)
        requested = data.get("cancel_requested", True)
        return sanitize({"ok": True, "backend": self.settings.backend, "robot_id": robot,
                         "observed_at": datetime.now(timezone.utc).isoformat(),
                         "data": {**data, "executed": requested,
                                  "completion_note": ("Cancellation requested; use get_task to confirm the terminal state."
                                                      if requested else "Task is already terminal; no cancellation command was sent.")}})

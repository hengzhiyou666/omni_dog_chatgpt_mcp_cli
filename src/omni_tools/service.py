"""Common public operations used by CLI and MCP."""
from datetime import datetime, timezone
import math
import re

from .config import Settings
from .errors import OmniError
from .policy import require_motion_write, require_posture_write, require_runtime_write, require_write
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

    def _posture(self, operation, posture, robot_id, execute):
        robot = identifier(self.settings.robot_id if robot_id is None else robot_id, "robot_id")
        if type(execute) is not bool:
            raise OmniError("invalid_argument", "execute must be a boolean")
        if not execute:
            data = {"operation": operation, "executed": False, "preview": True,
                    "parameters": {"robot_id": robot, "posture": posture},
                    "execution_backend": "ssh",
                    "current_backend_supports_execution": self.settings.backend == "ssh",
                    "execution_requirements": ["SSH backend", "OMNI_ALLOW_WRITES=1",
                                               "robot in OMNI_ALLOWED_ROBOTS", "execute=true",
                                               "idle robot with exclusive APP control authority"],
                    "physical_posture_confirmed": False}
        else:
            require_posture_write(self.settings, robot)
            data = {**self.backend.set_posture(posture, robot),
                    "operation": operation, "executed": True, "preview": False,
                    "physical_posture_confirmed": False,
                    "completion_note": "Bridge/vendor acknowledged the posture command; physical posture is not independently confirmed."}
        return sanitize({"ok": True, "backend": self.settings.backend, "robot_id": robot,
                         "observed_at": datetime.now(timezone.utc).isoformat(), "data": data})

    def stand_up(self, robot_id: str | None = None, execute: bool = False) -> dict:
        """Stand up (站立、起立、站起来); preview unless explicitly executed."""
        return self._posture("stand_up", "stand", robot_id, execute)

    def lie_down(self, robot_id: str | None = None, execute: bool = False) -> dict:
        """Lie down (坐下、卧下、趴下); all three mean lying down, never sitting."""
        return self._posture("lie_down", "lie_down", robot_id, execute)

    def move_forward(self, distance_m: float = 1.0, robot_id: str | None = None,
                     execute: bool = False) -> dict:
        """Move body-forward by bounded relative odometry distance, then stop."""
        robot = identifier(self.settings.robot_id if robot_id is None else robot_id, "robot_id")
        if type(execute) is not bool:
            raise OmniError("invalid_argument", "execute must be a boolean")
        try:
            valid = type(distance_m) in {int, float} and math.isfinite(distance_m) and 0.1 <= distance_m <= 2.0
        except (OverflowError, ValueError):
            valid = False
        if not valid:
            raise OmniError("invalid_argument", "distance_m must be a finite number from 0.1 to 2.0")
        distance_m = float(distance_m)
        if not execute:
            data = {"operation": "move_forward", "executed": False, "preview": True,
                    "parameters": {"robot_id": robot, "distance_m": distance_m},
                    "execution_backend": "ssh",
                    "current_backend_supports_execution": self.settings.backend == "ssh",
                    "execution_requirements": ["SSH backend", "OMNI_ALLOW_WRITES=1",
                                               "robot in OMNI_ALLOWED_ROBOTS", "execute=true",
                                               "standing idle robot with exclusive APP authority",
                                               "fresh continuous robot odometry"],
                    "completion_note": "Robot-side feedback controls distance and confirms stopping; the model does not stream velocities."}
        else:
            require_motion_write(self.settings, robot)
            data = {**self.backend.move_forward(distance_m, robot),
                    "operation": "move_forward", "executed": True, "preview": False}
        return sanitize({"ok": True, "backend": self.settings.backend, "robot_id": robot,
                         "observed_at": datetime.now(timezone.utc).isoformat(), "data": data})

    @staticmethod
    def _runtime_map(map_id, map_version, map_checksum, initial_x, initial_y, initial_z, initial_yaw):
        map_id = identifier(map_id, "map_id")
        if type(map_version) is not int or not 1 <= map_version <= 4294967295:
            raise OmniError("invalid_argument", "map_version must be an integer from 1 to 4294967295")
        if not isinstance(map_checksum, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", map_checksum):
            raise OmniError("invalid_argument", "map_checksum must be a SHA-256 hex digest")
        pose = dict(initial_x=initial_x, initial_y=initial_y, initial_z=initial_z, initial_yaw=initial_yaw)
        if any(type(value) not in {int, float} or not -3.402823466e38 <= value <= 3.402823466e38
               for value in pose.values()):
            raise OmniError("invalid_argument", "Initial pose values must be finite float32 numbers")
        return {"map_id": map_id, "map_version": map_version, "map_checksum": map_checksum.lower(), **pose}

    def _runtime_mutation(self, operation, parameters, robot_id, execute):
        robot = identifier(self.settings.robot_id if robot_id is None else robot_id, "robot_id")
        if type(execute) is not bool:
            raise OmniError("invalid_argument", "execute must be a boolean")
        if not execute:
            data = {"operation": operation, "executed": False, "preview": True,
                    "parameters": {"robot_id": robot, **parameters}, "execution_backend": "ssh",
                    "current_backend_supports_execution": self.settings.backend == "ssh",
                    "execution_requirements": ["SSH backend", "OMNI_ALLOW_WRITES=1",
                                               "robot in OMNI_ALLOWED_ROBOTS", "execute=true"]}
        else:
            require_runtime_write(self.settings, robot)
            data = {**getattr(self.backend, operation)(**parameters, robot_id=robot),
                    "operation": operation, "executed": True, "preview": False}
        return sanitize({"ok": True, "backend": self.settings.backend, "robot_id": robot,
                         "observed_at": datetime.now(timezone.utc).isoformat(), "data": data})

    def start_localization(self, map_id: str, map_version: int, map_checksum: str,
                           initial_x: float = 0.0, initial_y: float = 0.0,
                           initial_z: float = 0.0, initial_yaw: float = 0.0,
                           robot_id: str | None = None, execute: bool = False) -> dict:
        parameters = self._runtime_map(map_id, map_version, map_checksum,
                                       initial_x, initial_y, initial_z, initial_yaw)
        return self._runtime_mutation("start_localization", parameters, robot_id, execute)

    def start_route_recording(self, route_id: str, map_id: str, map_version: int, map_checksum: str,
                              initial_x: float = 0.0, initial_y: float = 0.0,
                              initial_z: float = 0.0, initial_yaw: float = 0.0,
                              robot_id: str | None = None, execute: bool = False) -> dict:
        parameters = {"route_id": identifier(route_id, "route_id"),
                      **self._runtime_map(map_id, map_version, map_checksum,
                                          initial_x, initial_y, initial_z, initial_yaw)}
        return self._runtime_mutation("start_route_recording", parameters, robot_id, execute)

    def finish_route_recording(self, recording_operation_id: str, disposition: str,
                               robot_id: str | None = None, execute: bool = False) -> dict:
        operation_id = task_identifier(recording_operation_id)
        if not isinstance(disposition, str) or disposition not in {"save", "discard"}:
            raise OmniError("invalid_argument", "disposition must be save or discard")
        return self._runtime_mutation("finish_route_recording",
                                      {"recording_operation_id": operation_id, "disposition": disposition},
                                      robot_id, execute)

    def set_runtime_idle(self, robot_id: str | None = None, execute: bool = False) -> dict:
        return self._runtime_mutation("set_runtime_idle", {}, robot_id, execute)

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

"""Common public operations used by CLI and MCP."""
from datetime import datetime, timezone

from .config import Settings
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
        robot = identifier(robot_id or self.settings.robot_id, "robot_id")
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

    def get_task(self, task_id: str, robot_id: str | None = None) -> dict:
        return self._read("get_task", robot_id, task_identifier(task_id))

    def get_inspection_result(self, task_id: str, robot_id: str | None = None) -> dict:
        return self._read("get_inspection_result", robot_id, task_identifier(task_id))

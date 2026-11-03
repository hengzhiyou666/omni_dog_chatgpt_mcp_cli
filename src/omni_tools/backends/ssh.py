"""Read-only robot queries over an existing, verified SSH host configuration.

The probe is streamed to Python's stdin; no helper is installed on the robot.
Only a fixed set of ROS subscriptions and query services is available.
"""
from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

from ..config import Settings
from ..errors import OmniError
from ..util import sanitize


_BOOTSTRAP = (
    "import json,sys; p=json.load(sys.stdin); "
    "n={'__name__':'omni_remote_probe'}; "
    "exec(compile(p['source'],'<omni-readonly-probe>','exec'),n); n['bootstrap'](p)"
)
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class SSHBackend:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _run(self, operation: str, robot_id: str, task_id: str | None = None) -> dict:
        if robot_id != self.settings.robot_id:
            raise OmniError("robot_not_configured", "Robot does not match this SSH configuration")
        if task_id is not None and (
            not isinstance(task_id, str) or not task_id or len(task_id) > 256
            or any(ord(c) < 32 for c in task_id)
        ):
            raise OmniError("invalid_argument", "Invalid task_id")
        marker = "OMNI_TOOLS_RESULT_" + uuid.uuid4().hex + ":"
        request = {"operation": operation, "robot_id": robot_id,
                   "timeout": self.settings.timeout, "task_id": task_id}
        source = Path(__file__).resolve().parents[1].joinpath("remote_probe.py").read_text()
        payload = json.dumps({"source": source, "request": request, "marker": marker},
                             allow_nan=False)
        # OpenSSH concatenates remote arguments through a shell. This command is
        # entirely constant: all user values travel as JSON on stdin instead.
        import shlex
        command = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                   "-o", "UpdateHostKeys=no",
                   "-o", f"ConnectTimeout={self.settings.ssh_connect_timeout}",
                   "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=1",
                   self.settings.ssh_host, "python3 -B -u -c " + shlex.quote(_BOOTSTRAP)]
        try:
            result = subprocess.run(command, input=payload, text=True, capture_output=True,
                                    timeout=self.settings.timeout + self.settings.ssh_connect_timeout + 3)
        except FileNotFoundError:
            raise OmniError("ssh_unavailable", "The SSH executable is unavailable") from None
        except subprocess.TimeoutExpired:
            raise OmniError("timeout", "SSH robot query exceeded its time limit") from None
        except OSError:
            raise OmniError("ssh_failed", "Unable to start the SSH robot query") from None
        # Do not return arbitrary stderr: SSH banners and runtime logs may
        # contain deployment details or credentials. Only accept our nonce line.
        if len(result.stdout.encode("utf-8")) > _MAX_RESPONSE_BYTES:
            raise OmniError("response_too_large", "Robot query exceeded the response limit")
        lines = [line[len(marker):] for line in result.stdout.splitlines() if line.startswith(marker)]
        if len(lines) != 1:
            if result.returncode == 255:
                raise OmniError("ssh_failed", "SSH connection or authentication failed; check the host configuration")
            raise OmniError("invalid_robot_response", "Robot returned no unique structured query response",
                            {"exit_code": result.returncode})
        try:
            envelope = json.loads(lines[0], parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, TypeError):
            raise OmniError("invalid_robot_response", "Robot query returned invalid JSON") from None
        if not isinstance(envelope, dict) or not isinstance(envelope.get("ok"), bool):
            raise OmniError("invalid_robot_response", "Robot query returned an invalid response envelope")
        if not envelope["ok"]:
            error = envelope.get("error")
            if not isinstance(error, dict) or not isinstance(error.get("code"), str) or not isinstance(error.get("message"), str):
                raise OmniError("invalid_robot_response", "Robot query returned an invalid error")
            raise OmniError(error["code"], error["message"], sanitize(error.get("details", {})))
        if result.returncode != 0 or not isinstance(envelope.get("data"), dict):
            raise OmniError("invalid_robot_response", "Robot query did not finish successfully")
        return sanitize(envelope["data"])

    def get_robot_status(self, robot_id: str) -> dict:
        return self._run("get_robot_status", robot_id)

    def list_maps(self, robot_id: str) -> dict:
        return self._run("list_maps", robot_id)

    def list_routes(self, robot_id: str) -> dict:
        return self._run("list_routes", robot_id)

    def get_task(self, task_id: str, robot_id: str) -> dict:
        return self._run("get_task", robot_id, task_id)

    def get_inspection_result(self, task_id: str, robot_id: str) -> dict:
        return self._run("get_inspection_result", robot_id, task_id)

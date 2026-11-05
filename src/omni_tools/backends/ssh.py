"""Robot queries and explicitly enabled bounded actions over verified SSH.

The probe is streamed to Python's stdin; no helper is installed on the robot.
The original read probe remains read-only. Writes use separate fixed probes.
"""
from __future__ import annotations

import json
import math
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
_POSTURE_BOOTSTRAP = (
    "import json,sys; p=json.load(sys.stdin); "
    "n={'__name__':'omni_remote_posture'}; "
    "exec(compile(p['probe_source'],'<omni-readonly-probe>','exec'),n); "
    "exec(compile(p['source'],'<omni-posture>','exec'),n); n['posture_bootstrap'](p)"
)
_MOTION_BOOTSTRAP = (
    "import json,sys; p=json.load(sys.stdin); "
    "n={'__name__':'omni_remote_motion'}; "
    "exec(compile(p['probe_source'],'<omni-readonly-probe>','exec'),n); "
    "exec(compile(p['posture_source'],'<omni-posture>','exec'),n); "
    "exec(compile(p['source'],'<omni-motion>','exec'),n); n['motion_bootstrap'](p)"
)
_RUNTIME_BOOTSTRAP = (
    "import json,sys; p=json.load(sys.stdin); "
    "n={'__name__':'omni_remote_runtime'}; "
    "exec(compile(p['probe_source'],'<omni-readonly-probe>','exec'),n); "
    "exec(compile(p['source'],'<omni-runtime>','exec'),n); n['runtime_bootstrap'](p)"
)
_RUNTIME_OPERATIONS = frozenset({"start_localization", "start_route_recording",
                                 "finish_route_recording", "set_runtime_idle"})
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class SSHBackend:
    transport_name = "SSH"
    transport_error_code = "ssh_failed"
    unavailable_code = "ssh_unavailable"

    def __init__(self, settings: Settings):
        self.settings = settings

    def _command(self, bootstrap):
        import shlex
        return ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                "-o", "UpdateHostKeys=no", "-o", f"ConnectTimeout={self.settings.ssh_connect_timeout}",
                "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=1",
                self.settings.ssh_host, "python3 -B -u -c " + shlex.quote(bootstrap)]

    def _execute(self, command, payload):
        return subprocess.run(command, input=payload, text=True, capture_output=True,
                              timeout=self.settings.timeout + self.settings.ssh_connect_timeout + 3)

    def _run(self, operation: str, robot_id: str, task_id: str | None = None,
             *, posture: str | None = None, distance_m: float | None = None,
             measurement_mode: str = "odometry", runtime_fields: dict | None = None) -> dict:
        if robot_id != self.settings.robot_id:
            raise OmniError("robot_not_configured", f"Robot does not match this {self.transport_name} configuration")
        if task_id is not None and (
            not isinstance(task_id, str) or not task_id or len(task_id) > 256
            or any(ord(c) < 32 for c in task_id)
        ):
            raise OmniError("invalid_argument", "Invalid task_id")
        marker = "OMNI_TOOLS_RESULT_" + uuid.uuid4().hex + ":"
        request = {"operation": operation, "robot_id": robot_id,
                   "timeout": self.settings.timeout, "task_id": task_id}
        source_root = Path(__file__).resolve().parents[1]
        source = source_root.joinpath("remote_probe.py").read_text()
        body = {"source": source, "request": request, "marker": marker}
        bootstrap = _BOOTSTRAP
        if operation == "set_posture":
            from ..policy import require_posture_write
            require_posture_write(self.settings, robot_id)
            if not isinstance(posture, str) or posture not in {"stand", "lie_down"}:
                raise OmniError("invalid_argument", "Posture must be stand or lie_down")
            request["posture"] = posture
            body["probe_source"] = source
            body["source"] = source_root.joinpath("remote_posture.py").read_text()
            bootstrap = _POSTURE_BOOTSTRAP
        elif operation == "move_forward":
            from ..policy import require_motion_write
            require_motion_write(self.settings, robot_id)
            if type(measurement_mode) is not str or measurement_mode not in {"odometry", "timed"}:
                raise OmniError("invalid_argument", "measurement_mode must be odometry or timed")
            if (type(distance_m) not in {int, float} or not 0.1 <= distance_m <= 2.0
                    or not math.isfinite(distance_m)):
                raise OmniError("invalid_argument", "Forward distance must be a finite number from 0.1 to 2 meters")
            request["distance_m"] = float(distance_m)
            if measurement_mode != "odometry":
                request["measurement_mode"] = measurement_mode
            body["probe_source"] = source
            body["posture_source"] = source_root.joinpath("remote_posture.py").read_text()
            body["source"] = source_root.joinpath("remote_motion.py").read_text()
            bootstrap = _MOTION_BOOTSTRAP
        elif operation in _RUNTIME_OPERATIONS:
            from ..policy import require_runtime_write
            from ..remote_runtime import validate_runtime_request
            from ..remote_probe import ProbeError
            require_runtime_write(self.settings, robot_id)
            request.update(runtime_fields or {})
            try:
                validate_runtime_request(request)
            except ProbeError as error:
                raise OmniError(error.code, error.message, error.details) from None
            body["probe_source"] = source
            body["source"] = source_root.joinpath("remote_runtime.py").read_text()
            bootstrap = _RUNTIME_BOOTSTRAP
        payload = json.dumps(body, allow_nan=False)
        # OpenSSH concatenates remote arguments through a shell. This command is
        # entirely constant: all user values travel as JSON on stdin instead.
        command = self._command(bootstrap)
        def transport_error(code, message, details=None):
            facts = dict(details or {})
            if operation == "set_posture":
                facts.update(operation=operation, posture=posture,
                             command_sent="unknown", retry_safe=False,
                             physical_posture_confirmed=False)
                message = "Posture transport outcome is unknown; do not automatically repeat the command"
            elif operation == "move_forward":
                facts.update(operation=operation, distance_m=distance_m,
                             command_sent="unknown", retry_safe=False,
                             completion="unknown", stop_confirmed=False)
                message = "Motion transport outcome is unknown; do not automatically repeat the command"
            elif operation in _RUNTIME_OPERATIONS:
                facts.update(operation=operation, command_sent="unknown", retry_safe=False,
                             completion="unknown", operation_id=None)
                message = "Runtime transport outcome is unknown; query current status before another request"
            return OmniError(code, message, facts)
        try:
            result = self._execute(command, payload)
        except FileNotFoundError:
            raise OmniError(self.unavailable_code, f"The {self.transport_name} executable is unavailable") from None
        except subprocess.TimeoutExpired:
            raise transport_error("timeout", "Robot query exceeded its time limit") from None
        except OSError:
            raise transport_error(self.transport_error_code, f"Unable to start the {self.transport_name} robot query") from None
        # Do not return arbitrary stderr: SSH banners and runtime logs may
        # contain deployment details or credentials. Only accept our nonce line.
        if len(result.stdout.encode("utf-8")) > _MAX_RESPONSE_BYTES:
            raise transport_error("response_too_large", "Robot query exceeded the response limit")
        lines = [line[len(marker):] for line in result.stdout.splitlines() if line.startswith(marker)]
        if len(lines) != 1:
            if result.returncode == 255:
                raise transport_error(self.transport_error_code, f"{self.transport_name} connection or authentication failed; check configuration")
            raise transport_error("invalid_robot_response", "Robot returned no unique structured query response",
                            {"exit_code": result.returncode})
        try:
            envelope = json.loads(lines[0], parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, TypeError):
            raise transport_error("invalid_robot_response", "Robot query returned invalid JSON") from None
        if not isinstance(envelope, dict) or not isinstance(envelope.get("ok"), bool):
            raise transport_error("invalid_robot_response", "Robot query returned an invalid response envelope")
        if not envelope["ok"]:
            error = envelope.get("error")
            if not isinstance(error, dict) or not isinstance(error.get("code"), str) or not isinstance(error.get("message"), str):
                raise transport_error("invalid_robot_response", "Robot query returned an invalid error")
            raise OmniError(error["code"], error["message"], sanitize(error.get("details", {})))
        if result.returncode != 0 or not isinstance(envelope.get("data"), dict):
            raise transport_error("invalid_robot_response", "Robot query did not finish successfully")
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

    def set_posture(self, posture: str, robot_id: str) -> dict:
        return self._run("set_posture", robot_id, posture=posture)

    def move_forward(self, distance_m: float, robot_id: str, measurement_mode: str = "odometry") -> dict:
        return self._run("move_forward", robot_id, distance_m=distance_m, measurement_mode=measurement_mode)

    def start_localization(self, map_id, map_version, map_checksum,
                           initial_x, initial_y, initial_z, initial_yaw, robot_id):
        return self._run("start_localization", robot_id, runtime_fields={
            "map_id": map_id, "map_version": map_version, "map_checksum": map_checksum,
            "initial_x": initial_x, "initial_y": initial_y, "initial_z": initial_z, "initial_yaw": initial_yaw})

    def start_route_recording(self, route_id, map_id, map_version, map_checksum,
                              initial_x, initial_y, initial_z, initial_yaw, robot_id):
        return self._run("start_route_recording", robot_id, runtime_fields={
            "route_id": route_id, "map_id": map_id, "map_version": map_version, "map_checksum": map_checksum,
            "initial_x": initial_x, "initial_y": initial_y, "initial_z": initial_z, "initial_yaw": initial_yaw})

    def finish_route_recording(self, recording_operation_id, disposition, robot_id):
        return self._run("finish_route_recording", robot_id, runtime_fields={
            "recording_operation_id": recording_operation_id, "disposition": disposition})

    def set_runtime_idle(self, robot_id):
        return self._run("set_runtime_idle", robot_id)

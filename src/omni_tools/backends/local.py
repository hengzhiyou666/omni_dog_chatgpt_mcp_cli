"""Run the existing fixed ROS workers on the robot without an SSH loopback."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

from .ssh import SSHBackend, _RUNTIME_OPERATIONS
from ..errors import OmniError
from ..remote_probe import ProbeError, check_cancelled, write_receipt


_WRITES = _RUNTIME_OPERATIONS | {"set_posture", "move_forward", "move", "rotate"}


class LocalBackend(SSHBackend):
    transport_name = "local ROS"
    transport_error_code = "local_failed"
    unavailable_code = "python_unavailable"

    def _command(self, bootstrap):
        if not sys.platform.startswith("linux"):
            raise OmniError("configuration_error", "The local ROS backend requires Linux")
        # Set this inside the child, not preexec_fn: MCP launches from threads.
        # Re-exec preserves the parent-death signal and the worker PID.
        parent_guard = (
            "import ctypes,os,signal\n"
            "libc=ctypes.CDLL(None,use_errno=True)\n"
            "if libc.prctl(1,signal.SIGTERM)!=0: raise OSError('Cannot configure parent-death signal')\n"
            f"if os.getppid()!={os.getpid()}: raise SystemExit('Caller exited before worker startup')\n"
        )
        return [sys.executable, "-B", "-u", "-c", parent_guard + bootstrap]

    def _run(self, operation, robot_id, *args, **kwargs):
        try:
            return super()._run(operation, robot_id, *args, **kwargs)
        except OmniError as error:
            # A task must not dispatch its queued next action after any failed
            # write. Preserve an already-written worker receipt for stop proof.
            path = os.getenv("OMNI_CANCEL_FILE", "")
            if operation in _WRITES and path and Path(path).is_absolute():
                try:
                    Path(path).touch()
                    try:
                        receipt = json.loads(Path(path + ".result.json").read_text())
                    except (OSError, ValueError):
                        receipt = {}
                    if not isinstance(receipt, dict) or receipt.get("state") != "finished":
                        write_receipt(operation, {"ok": False, "error": error.as_dict()}, force_local=True)
                except OSError:
                    # Receipt failure must not replace the original operation outcome.
                    pass
            raise

    def _execute(self, command, payload):
        operation = json.loads(payload)["request"]["operation"]
        writing = operation in _WRITES
        if writing:
            try:
                check_cancelled(force_local=True)
                write_receipt(operation, None, state="running", force_local=True)
            except ProbeError as error:
                raise OmniError(error.code, error.message, error.details) from None
            except OSError:
                raise OmniError("configuration_error", "Cannot create the task receipt", {
                    "command_sent": False, "retry_safe": False}) from None
        environment = {**os.environ, "OMNI_BACKEND": "local"}
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, start_new_session=True,
                                       env=environment)
        except OSError:
            if writing:
                write_receipt(operation, {"ok": False, "error": {
                    "code": "local_failed", "message": "Unable to start the local ROS worker",
                    "details": {"command_sent": False, "retry_safe": False}}}, force_local=True)
            raise
        try:
            stdout, stderr = process.communicate(payload, timeout=self.settings.timeout + 5)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as interruption:
            # TERM is handled by the worker without shutting down ROS first.
            # Its existing finally owns zero velocity and lease release.
            try:
                process.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                error = OmniError("timeout", "Local worker did not finish cancellation cleanup", {
                    "operation": operation, "command_sent": "unknown", "retry_safe": False,
                    "stop_confirmed": False, "control_released": False})
                if writing:
                    write_receipt(operation, {"ok": False, "error": error.as_dict()}, force_local=True)
                raise error from None
            if isinstance(interruption, KeyboardInterrupt):
                raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

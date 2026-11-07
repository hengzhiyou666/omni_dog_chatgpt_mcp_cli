"""New motion axes reuse existing transport, write gates and cancellation receipts."""
import json
from pathlib import Path
import shlex
import signal
import subprocess
from types import SimpleNamespace

import pytest

from omni_tools.backends.local import LocalBackend
from omni_tools.backends.ssh import SSHBackend, _MOTION_BOOTSTRAP
from omni_tools.config import Settings
from omni_tools.errors import OmniError
from omni_tools.remote_probe import write_receipt


def settings(**overrides):
    return Settings(allow_writes=True, allowed_robots=("dog4",), **overrides)


def invoke(backend, operation):
    if operation == "move":
        return backend.move("left", 0.2, "dog4")
    return backend.rotate(15.0, "counterclockwise", "dog4")


@pytest.mark.parametrize("operation", ["move", "rotate"])
def test_ssh_streams_timed_request_and_preserves_configured_timeout(monkeypatch, operation):
    calls = []
    def execute(self, command, payload):
        body = json.loads(payload)
        calls.append(body)
        target = {"distance_m": 0.2, "direction": "left"} if operation == "move" else {
            "angle_deg": 15.0, "direction": "counterclockwise"}
        assert body["request"] == {"operation": operation, "robot_id": "dog4", "task_id": None,
                                   "measurement_mode": "timed", "timeout": 60.0, **target}
        assert all(key in body for key in ("probe_source", "posture_source", "source"))
        assert shlex.split(command[-1]) == ["python3", "-B", "-u", "-c", _MOTION_BOOTSTRAP]
        assert command[:2] == ["ssh", "-T"]
        data = {"action": operation, "completion": "estimated_time", "stop_confirmed": True,
                "control_released": True, "actual_distance_m": None, "actual_angle_deg": None}
        return SimpleNamespace(returncode=0, stdout=body["marker"] + json.dumps({"ok": True, "data": data}))
    monkeypatch.setattr(SSHBackend, "_execute", execute)
    result = invoke(SSHBackend(settings(timeout=60.0)), operation)
    assert len(calls) == 1 and result["action"] == operation
    assert result["completion"] == "estimated_time" and result["stop_confirmed"]


@pytest.mark.parametrize("operation", ["move", "rotate"])
@pytest.mark.parametrize("mode", ["disabled", "not_allowed", "wrong_robot"])
def test_backend_write_rejections_do_not_execute(monkeypatch, operation, mode):
    monkeypatch.setattr(SSHBackend, "_execute", lambda *args: pytest.fail("must not execute"))
    config = {"allow_writes": mode != "disabled", "allowed_robots": () if mode == "not_allowed" else ("dog4",)}
    if mode == "wrong_robot":
        config["robot_id"] = "other"
    with pytest.raises(OmniError) as caught:
        invoke(SSHBackend(Settings(**config)), operation)
    assert caught.value.code == {"disabled": "writes_disabled", "not_allowed": "robot_not_allowed",
                                 "wrong_robot": "robot_not_configured"}[mode]


@pytest.mark.parametrize("operation", ["move", "rotate"])
@pytest.mark.parametrize("bad", [True, None, "1", float("nan"), float("inf"), -1, 10**1000])
def test_backend_rejects_invalid_magnitude_without_execution(monkeypatch, operation, bad):
    monkeypatch.setattr(SSHBackend, "_execute", lambda *args: pytest.fail("must not execute"))
    backend = SSHBackend(settings())
    with pytest.raises(OmniError) as caught:
        backend.move("forward", bad, "dog4") if operation == "move" else backend.rotate(bad, "clockwise", "dog4")
    assert caught.value.code == "invalid_argument"


@pytest.mark.parametrize("operation", ["move", "rotate"])
@pytest.mark.parametrize("direction", [[], None, True, "", "auto"])
def test_backend_rejects_invalid_direction_without_execution(monkeypatch, operation, direction):
    monkeypatch.setattr(SSHBackend, "_execute", lambda *args: pytest.fail("must not execute"))
    backend = SSHBackend(settings())
    with pytest.raises(OmniError, match="direction"):
        backend.move(direction, 0.2, "dog4") if operation == "move" else backend.rotate(15, direction, "dog4")


@pytest.mark.parametrize("operation", ["move", "rotate"])
@pytest.mark.parametrize("failure", ["timeout", "disconnect"])
def test_uncertain_outcome_is_never_retried(monkeypatch, operation, failure):
    calls = []
    def execute(*args):
        calls.append(1)
        if failure == "timeout":
            raise subprocess.TimeoutExpired("ssh", 25)
        return SimpleNamespace(returncode=255, stdout="")
    monkeypatch.setattr(SSHBackend, "_execute", execute)
    with pytest.raises(OmniError) as caught:
        invoke(SSHBackend(settings()), operation)
    assert len(calls) == 1
    assert caught.value.details["retry_safe"] is False
    assert caught.value.details["command_sent"] == "unknown"
    assert caught.value.details["stop_confirmed"] is False
    assert caught.value.details["direction"] == ("left" if operation == "move" else "counterclockwise")


@pytest.mark.parametrize("operation", ["move", "rotate"])
def test_local_cancelled_task_never_spawns_worker(monkeypatch, tmp_path, operation):
    cancel = tmp_path / "cancel"
    cancel.touch()
    monkeypatch.setenv("OMNI_CANCEL_FILE", str(cancel))
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("must not spawn"))
    with pytest.raises(OmniError) as caught:
        invoke(LocalBackend(settings(backend="local")), operation)
    assert caught.value.code == "cancelled"
    assert json.loads(Path(str(cancel) + ".result.json").read_text())["operation"] == operation


@pytest.mark.parametrize("operation", ["move", "rotate"])
def test_local_timeout_preserves_cleanup_receipt(monkeypatch, tmp_path, operation):
    cancel = tmp_path / "cancel"
    monkeypatch.setenv("OMNI_CANCEL_FILE", str(cancel))
    events = []
    class Worker:
        returncode = 0
        def __init__(self, command, **kwargs):
            assert kwargs["env"]["OMNI_BACKEND"] == "local"
        def communicate(self, payload=None, timeout=None):
            events.append(("communicate", timeout))
            if payload is not None:
                self.body = json.loads(payload)
                running = json.loads(Path(str(cancel) + ".result.json").read_text())
                assert running == {"state": "running", "operation": operation, "result": None}
                raise subprocess.TimeoutExpired("python", timeout)
            envelope = {"ok": False, "error": {"code": "cancelled", "message": "cancelled",
                        "details": {"stop_confirmed": True, "control_released": True, "retry_safe": False}}}
            write_receipt(operation, envelope, force_local=True)
            return self.body["marker"] + json.dumps(envelope), ""
        def send_signal(self, value):
            events.append(("signal", value))
    monkeypatch.setattr(subprocess, "Popen", Worker)
    with pytest.raises(OmniError) as caught:
        invoke(LocalBackend(settings(backend="local")), operation)
    assert caught.value.details["stop_confirmed"] and caught.value.details["control_released"]
    assert events == [("communicate", 17.0), ("signal", signal.SIGTERM), ("communicate", 5)]
    receipt = json.loads(Path(str(cancel) + ".result.json").read_text())
    assert receipt["state"] == "finished" and receipt["operation"] == operation
    assert receipt["result"]["error"]["details"]["control_released"]

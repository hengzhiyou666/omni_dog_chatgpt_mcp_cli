"""Local ROS reuses the fixed workers; transport never opens SSH or ROS in tests."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

from omni_tools.backends.local import LocalBackend
from omni_tools.config import Settings
from omni_tools.errors import OmniError
from omni_tools.remote_probe import write_receipt
from omni_tools.service import OmniService


def settings(**kwargs):
    return Settings(backend="local", allow_writes=True, allowed_robots=("dog4",), **kwargs)


def run_operation(backend, operation):
    if operation == "set_posture": return backend.set_posture("stand", "dog4")
    if operation == "move_forward": return backend.move_forward(1., "dog4")
    if operation == "start_localization": return backend.start_localization("map-a", 1, "a"*64, 0., 0., 0., 0., "dog4")
    if operation == "start_route_recording": return backend.start_route_recording("route-a", "map-a", 1, "a"*64, 0., 0., 0., 0., "dog4")
    if operation == "finish_route_recording": return backend.finish_route_recording("epoch/runtime/1", "discard", "dog4")
    if operation == "get_task": return backend.get_task("epoch/runtime/1", "dog4")
    if operation == "get_inspection_result": return backend.get_inspection_result("mission-a", "dog4")
    return getattr(backend, operation)("dog4")


class FakeWorker:
    def __init__(self, command, **kwargs):
        self.command, self.kwargs, self.returncode = command, kwargs, 0
        self.events = []
    def communicate(self, data=None, timeout=None):
        self.events.append(("communicate", timeout))
        payload = json.loads(data)
        op = payload["request"]["operation"]
        envelope = {"ok": True, "data": {"operation": op, "completion": "observed"}}
        if op in {"set_posture", "move_forward", "start_localization", "start_route_recording", "finish_route_recording", "set_runtime_idle"}:
            write_receipt(op, envelope, force_local=True)
        return payload["marker"] + json.dumps(envelope), "private diagnostic banner"
    def send_signal(self, sig): self.events.append(("signal", sig))
    def kill(self): self.events.append(("kill",))


@pytest.mark.parametrize("operation", ["get_robot_status", "list_maps", "list_routes", "get_task", "get_inspection_result",
                                      "set_posture", "move_forward", "start_localization", "start_route_recording", "finish_route_recording", "set_runtime_idle"])
def test_local_reuses_all_supported_worker_payloads_without_ssh(monkeypatch, operation):
    workers = []
    def create(command, **kwargs):
        assert command[:4] == [sys.executable, "-B", "-u", "-c"]
        assert "prctl(1,signal.SIGTERM)" in command[4]
        assert kwargs["start_new_session"] is True
        assert kwargs["env"]["OMNI_BACKEND"] == "local"
        worker = FakeWorker(command, **kwargs)
        workers.append(worker)
        return worker
    monkeypatch.setattr(subprocess, "Popen", create)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("local must not use SSH"))
    assert run_operation(LocalBackend(settings()), operation)["operation"] == operation
    assert len(workers) == 1


def test_local_factory_and_preview_do_not_spawn_ros(monkeypatch):
    monkeypatch.setenv("OMNI_BACKEND", "local")
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("preview must be offline"))
    service = OmniService(Settings.from_env())
    assert isinstance(service.backend, LocalBackend)
    for preview in (service.stand_up(), service.move_forward(), service.set_runtime_idle()):
        assert preview["data"]["preview"] is True
        assert preview["data"]["execution_backend"] == "local"
        assert preview["data"]["current_backend_supports_execution"] is True


@pytest.mark.parametrize("call", [lambda s: s.start_inspection("route-a", execute=True),
                                  lambda s: s.navigate_to("map-a", 1, 0., 0., execute=True),
                                  lambda s: s.cancel_task("task-a", execute=True)])
def test_local_does_not_expand_http_task_execution(monkeypatch, call):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("unsupported operation must not spawn"))
    with pytest.raises(OmniError) as caught:
        call(OmniService(settings()))
    assert caught.value.code == "unsupported_backend"


def test_task_marker_blocks_next_action_but_not_status_reads(monkeypatch, tmp_path):
    cancel = tmp_path / "cancel"
    monkeypatch.setenv("OMNI_CANCEL_FILE", str(cancel))
    cancel.touch()
    monkeypatch.setattr(subprocess, "Popen", FakeWorker)
    with pytest.raises(OmniError) as caught:
        LocalBackend(settings()).move_forward(1., "dog4")
    assert caught.value.code == "cancelled"
    receipt = Path(str(cancel) + ".result.json").read_bytes()
    assert LocalBackend(settings()).get_robot_status("dog4")["operation"] == "get_robot_status"
    assert Path(str(cancel) + ".result.json").read_bytes() == receipt


def test_timeout_requests_term_and_preserves_worker_cleanup_receipt(monkeypatch, tmp_path):
    cancel = tmp_path / "cancel"
    monkeypatch.setenv("OMNI_CANCEL_FILE", str(cancel))
    workers = []
    class SlowWorker(FakeWorker):
        def communicate(self, data=None, timeout=None):
            self.events.append(("communicate", timeout))
            if data is not None:
                self.payload = json.loads(data)
                running = json.loads(Path(str(cancel) + ".result.json").read_text())
                assert running == {"state": "running", "operation": "move_forward", "result": None}
                raise subprocess.TimeoutExpired("python", timeout)
            result = {"ok": False, "error": {"code": "cancelled", "message": "cancelled", "details": {
                "command_sent": True, "stop_confirmed": True, "control_released": True, "retry_safe": False}}}
            write_receipt("move_forward", result, force_local=True)
            return self.payload["marker"] + json.dumps(result), ""
    def create(*args, **kwargs):
        worker = SlowWorker(*args, **kwargs);workers.append(worker);return worker
    monkeypatch.setattr(subprocess, "Popen", create)
    with pytest.raises(OmniError) as caught:
        LocalBackend(settings()).move_forward(1., "dog4")
    assert caught.value.details["stop_confirmed"] is True
    assert workers[0].events == [("communicate", 17.0), ("signal", signal.SIGTERM), ("communicate", 5)]
    assert cancel.exists()
    receipt = json.loads(Path(str(cancel) + ".result.json").read_text())
    assert receipt["state"] == "finished"
    assert receipt["result"]["error"]["details"]["control_released"] is True


def test_unresponsive_cleanup_is_reported_unknown_and_blocks_later_actions(monkeypatch, tmp_path):
    cancel = tmp_path / "cancel"
    monkeypatch.setenv("OMNI_CANCEL_FILE", str(cancel))
    workers = []
    class HungWorker(FakeWorker):
        def communicate(self, data=None, timeout=None):
            self.events.append(("communicate", timeout))
            if timeout is not None: raise subprocess.TimeoutExpired("python", timeout)
            return "", ""
    def create(*args, **kwargs):
        worker = HungWorker(*args, **kwargs);workers.append(worker);return worker
    monkeypatch.setattr(subprocess, "Popen", create)
    with pytest.raises(OmniError) as caught:
        LocalBackend(settings()).move_forward(1., "dog4")
    assert caught.value.details["stop_confirmed"] is False
    assert caught.value.details["command_sent"] == "unknown"
    assert ("kill",) in workers[0].events
    receipt = json.loads(Path(str(cancel) + ".result.json").read_text())
    assert receipt["result"]["ok"] is False
    assert receipt["result"]["error"]["details"]["stop_confirmed"] is False
    with pytest.raises(OmniError, match="cancel"):
        LocalBackend(settings()).set_posture("stand", "dog4")
    assert len(workers) == 1

"""Runtime operations use the same verified SSH boundary as existing controls."""
import io
import json
import subprocess
from types import SimpleNamespace

import pytest

from omni_tools.backends.ssh import SSHBackend, _RUNTIME_BOOTSTRAP
from omni_tools.config import Settings
from omni_tools.errors import OmniError


def enabled():
    return Settings(allow_writes=True, allowed_robots=("dog4",))


def invoke(backend, operation):
    pose = ("map-a", 1, "a"*64, 0., 0., 0., 0., "dog4")
    if operation == "start_localization": return backend.start_localization(*pose)
    if operation == "start_route_recording": return backend.start_route_recording("route-a", *pose)
    if operation == "finish_route_recording": return backend.finish_route_recording("epoch/runtime/1", "discard", "dog4")
    return backend.set_runtime_idle("dog4")


@pytest.mark.parametrize("operation", ["start_localization", "start_route_recording", "finish_route_recording", "set_runtime_idle"])
def test_runtime_transport_is_fixed_and_streamed(monkeypatch, operation):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        payload = json.loads(kwargs["input"])
        assert payload["request"]["operation"] == operation
        assert "StrictHostKeyChecking=yes" in command
        assert "UpdateHostKeys=no" in command
        assert "epoch/runtime/1" not in command[-1]
        assert "map-a" not in command[-1]
        assert kwargs.get("shell", False) is False
        namespace = {"__name__": "omni_remote_runtime"}
        for key in ("probe_source", "source"):
            exec(compile(payload[key], "<streamed-runtime>", "exec"), namespace)
        namespace["validate_runtime_request"](payload["request"])
        assert callable(namespace["RuntimeSession"])
        return SimpleNamespace(returncode=0, stderr="private banner", stdout=payload["marker"] + json.dumps({
            "ok": True, "data": {"completion": "idle", "operation_id": "epoch/runtime/2"}}))
    monkeypatch.setattr(subprocess, "run", run)
    assert invoke(SSHBackend(enabled()), operation)["operation_id"] == "epoch/runtime/2"
    assert len(calls) == 1


@pytest.mark.parametrize(("settings", "code"), [
    (Settings(), "writes_disabled"),
    (Settings(allow_writes=True), "robot_not_allowed"),
    (Settings(backend="http", base_url="http://127.0.0.1", token="test", allow_writes=True,
              allowed_robots=("dog4",)), "unsupported_backend"),
])
def test_runtime_gate_rejects_before_transport(monkeypatch, settings, code):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("No SSH allowed"))
    with pytest.raises(OmniError) as caught:
        invoke(SSHBackend(settings), "set_runtime_idle")
    assert caught.value.code == code


def test_invalid_direct_backend_arguments_never_reach_ssh(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("No SSH allowed"))
    with pytest.raises(OmniError) as caught:
        SSHBackend(enabled()).start_localization("map-a", 1, "bad-checksum", 0., 0., 0., 0., "dog4")
    assert caught.value.code == "invalid_argument"


def test_runtime_transport_timeout_does_not_retry_or_claim_success(monkeypatch):
    calls = []
    def run(*args, **kwargs):
        calls.append(1)
        raise subprocess.TimeoutExpired("ssh", 25)
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(OmniError) as caught:
        invoke(SSHBackend(enabled()), "start_route_recording")
    assert len(calls) == 1
    assert caught.value.details["command_sent"] == "unknown"
    assert caught.value.details["retry_safe"] is False
    assert caught.value.details["completion"] == "unknown"


def test_runtime_bootstrap_compiles_read_and_runtime_sources(monkeypatch, capsys):
    payload = {"probe_source": "import json; trace=['read']", "source": "trace.append('runtime')\ndef runtime_bootstrap(p):\n print(json.dumps(trace))"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    exec(_RUNTIME_BOOTSTRAP, {})
    assert json.loads(capsys.readouterr().out) == ["read", "runtime"]

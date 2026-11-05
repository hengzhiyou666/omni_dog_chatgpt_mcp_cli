"""Forward-motion SSH transport is bounded, gated, and never retried."""
import io
import json
import subprocess
from types import SimpleNamespace

import pytest

from omni_tools.backends.ssh import SSHBackend, _MOTION_BOOTSTRAP
from omni_tools.config import Settings
from omni_tools.errors import OmniError


def enabled():
    return Settings(allow_writes=True, allowed_robots=("dog4",))


def response(payload, data=None, error=None, returncode=0):
    envelope = {"ok": False, "error": error} if error else {"ok": True, "data": data or {
        "completion": "measured_displacement", "stop_confirmed": True,
        "actual_distance_m": payload["request"]["distance_m"],
    }}
    return SimpleNamespace(returncode=returncode, stderr="private credential banner",
                           stdout="ROS startup banner\n" + payload["marker"] + json.dumps(envelope) + "\n")


@pytest.mark.parametrize(("settings", "code"), [
    (Settings(), "writes_disabled"),
    (Settings(allow_writes=True), "robot_not_allowed"),
    (Settings(backend="http", base_url="http://127.0.0.1", token="test",
              allow_writes=True, allowed_robots=("dog4",)), "unsupported_backend"),
])
def test_forward_backend_enforces_write_gate_before_ssh(monkeypatch, settings, code):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("SSH must not start"))
    with pytest.raises(OmniError) as caught:
        SSHBackend(settings).move_forward(1.0, "dog4")
    assert caught.value.code == code


@pytest.mark.parametrize("distance", [
    0, -1, 0.099, 2.001, float("nan"), float("inf"), -float("inf"),
    True, False, "1", None, [], 10 ** 400,
])
def test_forward_backend_rejects_invalid_distance_without_ssh(monkeypatch, distance):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("SSH must not start"))
    with pytest.raises(OmniError) as caught:
        SSHBackend(enabled()).move_forward(distance, "dog4")
    assert caught.value.code == "invalid_argument"


def test_forward_backend_rejects_wrong_robot_without_ssh(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("SSH must not start"))
    with pytest.raises(OmniError) as caught:
        SSHBackend(enabled()).move_forward(1.0, "dog5")
    assert caught.value.code == "robot_not_configured"


@pytest.mark.parametrize("distance", [0.1, 1, 1.25, 2.0])
def test_forward_ssh_streams_all_three_sources_and_preserves_fixed_command(monkeypatch, distance):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        payload = json.loads(kwargs["input"])
        assert payload["request"] == {
            "operation": "move_forward", "robot_id": "dog4", "timeout": 12.0,
            "task_id": None, "distance_m": float(distance),
        }
        assert command[:2] == ["ssh", "-T"]
        for option in ("BatchMode=yes", "StrictHostKeyChecking=yes", "UpdateHostKeys=no",
                       "ConnectTimeout=10", "ServerAliveInterval=5", "ServerAliveCountMax=1"):
            assert option in command
        assert command[-2] == "dog4"
        assert "move_forward" not in command[-1]
        assert kwargs.get("shell", False) is False
        assert kwargs["timeout"] == 25
        namespace = {"__name__": "omni_remote_motion"}
        for key in ("probe_source", "posture_source", "source"):
            exec(compile(payload[key], "<streamed-" + key + ">", "exec"), namespace)
        namespace["validate_motion_request"](payload["request"])
        assert namespace["MotionSession"].__mro__[1] is namespace["PostureSession"]
        assert callable(namespace["motion_bootstrap"])
        return response(payload)

    monkeypatch.setattr(subprocess, "run", run)
    result = SSHBackend(enabled()).move_forward(distance, "dog4")
    assert result["completion"] == "measured_displacement"
    assert result["actual_distance_m"] == float(distance)
    assert result["stop_confirmed"] is True
    assert len(calls) == 1


def test_motion_bootstrap_compiles_shared_sources_in_order(monkeypatch, capsys):
    payload = {
        "probe_source": "import json; trace=['read']",
        "posture_source": "trace.append('posture')",
        "source": "trace.append('motion')\ndef motion_bootstrap(payload):\n print(json.dumps(trace + [payload['request']['operation']]))",
        "request": {"operation": "move_forward"}, "marker": "marker:",
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    exec(_MOTION_BOOTSTRAP, {})
    assert json.loads(capsys.readouterr().out) == ["read", "posture", "motion", "move_forward"]


def test_timed_mode_is_explicit_in_json_payload_and_preserves_estimate(monkeypatch):
    calls = []

    def run(command, **kwargs):
        payload = json.loads(kwargs["input"])
        calls.append(payload["request"])
        assert payload["request"]["measurement_mode"] == "timed"
        assert "timed" not in command[-1]
        return response(payload, data={"completion": "estimated_time", "actual_distance_m": None,
                                       "stop_confirmed": True, "control_released": True})

    monkeypatch.setattr(subprocess, "run", run)
    result = SSHBackend(enabled()).move_forward(1.0, "dog4", "timed")
    assert len(calls) == 1
    assert result["completion"] == "estimated_time"
    assert result["actual_distance_m"] is None


@pytest.mark.parametrize("mode", [None, True, [], "auto", ""])
def test_invalid_measurement_mode_is_rejected_before_ssh(monkeypatch, mode):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(1))
    with pytest.raises(OmniError, match="measurement_mode"):
        SSHBackend(enabled()).move_forward(1.0, "dog4", mode)
    assert calls == []


@pytest.mark.parametrize("failure", [
    "timeout", "os_error", "disconnect", "missing_marker", "duplicate_marker",
    "invalid_json", "invalid_envelope", "nonzero_success", "oversized",
])
def test_unknown_forward_outcome_never_retries_or_claims_completion(monkeypatch, failure):
    calls = []

    def run(*args, **kwargs):
        calls.append(1)
        if failure == "timeout":
            raise subprocess.TimeoutExpired("ssh", 25)
        if failure == "os_error":
            raise OSError("private error")
        payload = json.loads(kwargs["input"])
        result = response(payload)
        if failure in {"disconnect", "missing_marker"}:
            result.stdout = ""
            result.returncode = 255 if failure == "disconnect" else 1
        elif failure == "duplicate_marker":
            result.stdout += result.stdout
        elif failure == "invalid_json":
            result.stdout = payload["marker"] + '{"ok":true,"data":{"distance":NaN}}'
        elif failure == "invalid_envelope":
            result.stdout = payload["marker"] + '[]'
        elif failure == "nonzero_success":
            result.returncode = 1
        elif failure == "oversized":
            result.stdout += "x" * 1024
        return result

    if failure == "oversized":
        monkeypatch.setattr("omni_tools.backends.ssh._MAX_RESPONSE_BYTES", 512)
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(OmniError) as caught:
        SSHBackend(enabled()).move_forward(1.0, "dog4")
    assert len(calls) == 1
    details = caught.value.details
    assert details["operation"] == "move_forward"
    assert details["distance_m"] == 1.0
    assert details["command_sent"] == "unknown"
    assert details["retry_safe"] is False
    assert details["completion"] == "unknown"
    assert details["stop_confirmed"] is False
    assert "automatically repeat" in caught.value.message
    assert "private" not in str(caught.value.as_dict())


def test_forward_preserves_confirmed_remote_failure_and_stop_result(monkeypatch):
    calls = []

    def run(*args, **kwargs):
        calls.append(1)
        return response(json.loads(kwargs["input"]), error={
            "code": "odometry_stale", "message": "Odometry feedback was lost",
            "details": {"retry_safe": False, "command_sent": True,
                        "stop_confirmed": True, "actual_distance_m": 0.4},
        })

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(OmniError) as caught:
        SSHBackend(enabled()).move_forward(1.0, "dog4")
    assert len(calls) == 1
    assert caught.value.code == "odometry_stale"
    assert caught.value.details["stop_confirmed"] is True
    assert caught.value.details["actual_distance_m"] == 0.4
    assert caught.value.details["retry_safe"] is False

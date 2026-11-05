import json
import subprocess
from types import SimpleNamespace

import pytest

from omni_tools.backends.ssh import SSHBackend
from omni_tools.config import Settings
from omni_tools.errors import OmniError
from omni_tools import remote_posture as posture
from omni_tools.remote_probe import ProbeError, validate_request


def request(command="stand"):
    return {"operation": "set_posture", "posture": command, "robot_id": "dog4", "timeout": 12}


def enabled():
    return Settings(allow_writes=True, allowed_robots=("dog4",))


class FakeSession:
    def __init__(self, *, deny=None, foreign=False, ack_error=None, release_error=False):
        self.deny = deny
        self.foreign = foreign
        self.ack_error = ack_error
        self.release_error = release_error
        self.sent = []
        self.released = []
        self.operations = []

    def prepare(self):
        pass

    def require_idle(self):
        if self.deny:
            raise ProbeError(self.deny, "Robot cannot accept posture")

    def require_available(self):
        pass

    def authority(self, operation, client_id):
        self.operations.append(operation)
        return {"accepted": True, "active_owner_type": 2 if self.foreign else 1,
                "active_client_id": "mission-active" if self.foreign else client_id}

    def wait_owned(self, client_id):
        pass

    def require_owned(self, client_id):
        pass

    def publish_once(self, command, client_id):
        self.sent.append((command, client_id))

    def wait_ack(self, command, client_id):
        if self.ack_error:
            raise ProbeError(self.ack_error, "Posture outcome is unavailable")
        return "success:" + command

    def release(self, client_id):
        self.released.append(client_id)
        if self.release_error:
            raise ProbeError("timeout", "Release timed out")
        return True


@pytest.mark.parametrize("command", ["stand", "lie_down"])
def test_single_posture_attempt_acknowledges_vendor_but_not_physical_pose(command):
    session = FakeSession()
    result = posture.execute_posture(request(command), session)
    assert len(session.sent) == 1
    assert session.sent[0][0] == command
    assert session.released == [session.sent[0][1]]
    assert session.sent[0][1].startswith("app-omni-")
    assert result["completion"] == "vendor_acknowledged"
    assert result["physical_posture_confirmed"] is False
    assert result["control_released"] is True


@pytest.mark.parametrize("failure", ["timeout", "posture_rejected", "control_lost"])
def test_ambiguous_or_failed_action_is_not_resent_and_releases_control(failure):
    session = FakeSession(ack_error=failure)
    with pytest.raises(ProbeError) as caught:
        posture.execute_posture(request(), session)
    assert len(session.sent) == len(session.released) == 1
    assert caught.value.code == failure
    assert caught.value.details["command_sent"] is True
    assert caught.value.details["retry_safe"] is False
    assert caught.value.details["control_released"] is True


def test_manual_override_is_insufficient_for_posture():
    session = FakeSession(foreign=True)
    with pytest.raises(ProbeError) as caught:
        posture.execute_posture(request(), session)
    assert caught.value.code == "control_denied"
    assert not session.sent
    assert len(session.released) == 1


def test_unexpected_failure_after_delivery_keeps_outcome_unknown():
    session = FakeSession()
    def fail(command, client):
        raise RuntimeError("Do not expose raw runtime data")
    session.wait_ack = fail
    with pytest.raises(ProbeError) as caught:
        posture.execute_posture(request(), session)
    assert caught.value.code == "posture_failed"
    assert caught.value.details["command_sent"] is True
    assert caught.value.details["retry_safe"] is False
    assert "runtime data" not in caught.value.message
    assert len(session.sent) == len(session.released) == 1


@pytest.mark.parametrize("failure", ["robot_busy", "estop_latched", "status_stale"])
def test_preconditions_block_acquire_and_actuation(failure):
    session = FakeSession(deny=failure)
    with pytest.raises(ProbeError):
        posture.execute_posture(request(), session)
    assert not session.sent and not session.operations and not session.released


def test_failed_release_remains_visible_after_vendor_acknowledgement():
    result = posture.execute_posture(request(), FakeSession(release_error=True))
    assert result["control_released"] is False
    assert result["physical_posture_confirmed"] is False
    assert len(result["warnings"]) == 2


@pytest.mark.parametrize("command", ["sit", "emergency_stop", "stand; reboot", None, [], 1])
def test_only_two_fixed_postures_are_accepted(command):
    with pytest.raises(ProbeError) as caught:
        posture.validate_posture_request(request(command))
    assert caught.value.code == "invalid_argument"


def test_existing_probe_still_rejects_posture_mutations():
    with pytest.raises(ProbeError) as caught:
        validate_request(request())
    assert caught.value.code == "unsupported_operation"


@pytest.mark.parametrize("settings,code", [
    (Settings(), "writes_disabled"),
    (Settings(allow_writes=True), "robot_not_allowed"),
])
def test_ssh_adapter_has_its_own_write_gate(monkeypatch, settings, code):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("SSH must not start"))
    with pytest.raises(OmniError) as caught:
        SSHBackend(settings).set_posture("stand", "dog4")
    assert caught.value.code == code


def test_ssh_posture_uses_separate_streamed_probe_and_constant_remote_command(monkeypatch):
    def run(command, **kwargs):
        payload = json.loads(kwargs["input"])
        assert payload["request"]["posture"] == "stand"
        assert "probe_source" in payload
        assert "set_posture" not in " ".join(command)
        assert kwargs.get("shell", False) is False
        assert "StrictHostKeyChecking=yes" in command
        assert "UpdateHostKeys=no" in command
        namespace = {"__name__": "omni_remote_posture"}
        exec(compile(payload["probe_source"], "<read-probe>", "exec"), namespace)
        exec(compile(payload["source"], "<posture-probe>", "exec"), namespace)
        namespace["validate_posture_request"](payload["request"])
        return SimpleNamespace(returncode=0, stderr="", stdout=payload["marker"] + json.dumps({
            "ok": True, "data": {"completion": "vendor_acknowledged", "physical_posture_confirmed": False}}))
    monkeypatch.setattr(subprocess, "run", run)
    result = SSHBackend(enabled()).set_posture("stand", "dog4")
    assert result["physical_posture_confirmed"] is False


@pytest.mark.parametrize("failure", ["timeout", "disconnect", "missing_marker"])
def test_lost_transport_does_not_imply_posture_was_not_executed(monkeypatch, failure):
    def run(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("ssh", 25)
        return SimpleNamespace(returncode=255 if failure == "disconnect" else 1, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(OmniError) as caught:
        SSHBackend(enabled()).set_posture("stand", "dog4")
    assert caught.value.details == {"operation": "set_posture", "posture": "stand",
        "command_sent": "unknown", "retry_safe": False, "physical_posture_confirmed": False,
        **({"exit_code": 1} if failure == "missing_marker" else {})}


def gated_session():
    session = posture.PostureSession.__new__(posture.PostureSession)
    session.samples = {
        "robot": {"operational_mode": 0, "mission_state": 0, "estop_latched": False, "charging": False},
        "mission": {"state": 0},
        "runtime": {"mode": 0, "desired_mode": 0, "phase": 0, "mission_active": False},
        "authority": {"state": 1, "base_owner_type": 0, "base_client_id": "", "manual_override_active": False},
    }
    session.received = {key: posture.time.monotonic() for key in session.samples}
    return session


@pytest.mark.parametrize("key,field,value,code", [
    ("robot", "estop_latched", True, "estop_latched"),
    ("robot", "charging", True, "robot_busy"),
    ("robot", "operational_mode", 3, "robot_busy"),
    ("mission", "state", 2, "robot_busy"),
    ("mission", "state", 3, "robot_busy"),
    ("runtime", "mission_active", True, "robot_busy"),
    ("runtime", "desired_mode", 1, "robot_busy"),
])
def test_live_state_gates_reject_busy_and_unsafe_states(key, field, value, code):
    session = gated_session()
    session.samples[key][field] = value
    with pytest.raises(ProbeError) as caught:
        session.require_idle()
    assert caught.value.code == code


def test_stale_state_and_another_controller_are_rejected():
    session = gated_session()
    session.samples["authority"].update(state=3, base_owner_type=1, base_client_id="app-other")
    with pytest.raises(ProbeError) as caught:
        session.require_available()
    assert caught.value.code == "control_busy"
    session.received["mission"] -= 10
    with pytest.raises(ProbeError) as caught:
        session.require_idle()
    assert caught.value.code == "status_stale"


def test_control_renewal_failure_ends_wait_without_resending(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(posture.time, "monotonic", lambda: now[0])
    session = posture.PostureSession.__new__(posture.PostureSession)
    session.ack = None
    session.require_idle = lambda: None
    session.require_owned = lambda client: None
    session.spin = lambda: now.__setitem__(0, now[0] + 1.1)
    calls = []
    def renew(operation, client):
        calls.append((operation, client))
        return {"accepted": False}
    session.authority = renew
    with pytest.raises(ProbeError) as caught:
        session.wait_ack("stand", "app-test")
    assert caught.value.code == "control_lost"
    assert calls == [(2, "app-test")]

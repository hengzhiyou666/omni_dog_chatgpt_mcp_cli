"""Cancellation is cooperative: persist the result after bounded action cleanup."""
import json
from pathlib import Path
import signal
import sys
from types import ModuleType, SimpleNamespace

import pytest

from omni_tools import remote_probe as probe
from omni_tools import remote_motion as motion
from omni_tools import remote_posture as posture
from omni_tools import remote_runtime as runtime


@pytest.fixture
def cancel_file(monkeypatch, tmp_path):
    path = tmp_path / "task.cancel"
    monkeypatch.setenv("OMNI_BACKEND", "local")
    monkeypatch.setenv("OMNI_CANCEL_FILE", str(path))
    return path


def receipt(path):
    return json.loads(Path(str(path) + ".result.json").read_text())


def test_receipt_records_running_then_failure_latches_before_atomic_replace(cancel_file, monkeypatch):
    probe.write_receipt("move_forward", None, state="running")
    assert not cancel_file.exists()
    assert receipt(cancel_file) == {"state": "running", "operation": "move_forward", "result": None}
    replace = probe.os.replace
    def commit(source, target):
        assert cancel_file.exists(), "subsequent actions must be blocked before publishing failure"
        assert receipt(cancel_file)["state"] == "running", "receipt must remain complete until atomic replacement"
        return replace(source, target)
    monkeypatch.setattr(probe.os, "replace", commit)
    result = {"ok": False, "error": {"code": "cancelled", "details": {"stop_confirmed": True}}}
    probe.write_receipt("move_forward", result)
    assert receipt(cancel_file)["result"] == result
    assert set(cancel_file.parent.iterdir()) == {Path(str(cancel_file) + ".result.json"), cancel_file}


def test_force_local_does_not_require_mutating_global_backend_environment(cancel_file, monkeypatch):
    monkeypatch.setenv("OMNI_BACKEND", "ssh")
    cancel_file.touch()
    probe.check_cancelled()  # SSH paths are unaffected by a local task marker.
    with pytest.raises(probe.ProbeError, match="cancelled"):
        probe.check_cancelled(force_local=True)
    probe.write_receipt("set_runtime_idle", {"ok": True}, force_local=True)
    assert receipt(cancel_file)["state"] == "finished"


def test_relative_cancellation_path_is_rejected(monkeypatch):
    monkeypatch.setenv("OMNI_BACKEND", "local")
    monkeypatch.setenv("OMNI_CANCEL_FILE", "relative.cancel")
    with pytest.raises(probe.ProbeError, match="absolute path"):
        probe.check_cancelled()


def test_read_emission_neither_checks_cancel_marker_nor_overwrites_action_receipt(cancel_file, capsys):
    cancel_file.touch()
    probe.write_receipt("move_forward", {"ok": False})
    before = receipt(cancel_file)
    probe._emit("READ:", {"ok": True, "data": {"read_only": True}})
    assert capsys.readouterr().out.startswith("READ:")
    assert receipt(cancel_file) == before


def test_final_receipt_survives_closed_stdout_without_becoming_a_false_failure(cancel_file, monkeypatch):
    def closed(*args, **kwargs):
        raise BrokenPipeError()
    monkeypatch.setattr("builtins.print", closed)
    result = {"ok": True, "data": {"stop_confirmed": True, "control_released": True}}
    probe._emit("RESULT:", result, operation="move_forward")
    assert receipt(cancel_file)["result"] == result
    assert not cancel_file.exists()


@pytest.mark.parametrize(("module", "bootstrap", "worker_request"), [
    (motion, motion.motion_bootstrap, {"operation": "move_forward", "robot_id": "dog4", "timeout": 12, "distance_m": 1}),
    (posture, posture.posture_bootstrap, {"operation": "set_posture", "robot_id": "dog4", "timeout": 12, "posture": "stand"}),
    (runtime, runtime.runtime_bootstrap, {"operation": "set_runtime_idle", "robot_id": "dog4", "timeout": 12}),
])
def test_cancelled_task_is_refused_before_bootstrap_environment_or_ros(module, bootstrap, worker_request, cancel_file, monkeypatch):
    cancel_file.touch()
    monkeypatch.setattr(module, "runtime_environment", lambda: pytest.fail("No ROS discovery after cancellation"))
    bootstrap({"request": worker_request, "marker": "RESULT:"})
    result = receipt(cancel_file)["result"]
    assert result["error"]["code"] == "cancelled"
    assert result["error"]["details"]["command_sent"] is False


@pytest.mark.parametrize("cancellation", ["marker", "signal"])
def test_motion_cancel_still_stops_and_releases_with_repeated_signal_ignored(cancel_file, monkeypatch, cancellation):
    calls = []
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGALRM)}
    class Session:
        def __init__(self, timeout): pass
        def prepare(self): pass
        def require_idle(self): pass
        def require_available(self): pass
        def wait_owned(self, client): pass
        def require_owned(self, client): pass
        def authority(self, op, client): return {"accepted": True, "active_owner_type": 1, "active_client_id": client}
        def start_locomotion(self, client): return "success:loco"
        def stationary_origin(self, client): return {"x": 0, "y": 0}
        def drive(self, distance, client, origin):
            calls.append("drive")
            cancel_file.touch()
            if cancellation == "signal": signal.raise_signal(signal.SIGTERM)
            probe.check_cancelled()
        def stop(self, client):
            calls.append("stop")
            probe.check_cancelled()  # Marker must not interrupt cleanup.
            signal.raise_signal(signal.SIGTERM)  # Neither may a repeated terminate.
            assert 0 < signal.getitimer(signal.ITIMER_REAL)[0] <= 3.5
            return True
        def measured_distance(self, origin): return .3
        def release(self, client):
            probe.check_cancelled()
            calls.append("release")
            return True
        def diagnostics(self): return {}
        def close(self): calls.append("close")
    monkeypatch.setattr(motion, "MotionSession", Session)
    motion.motion_worker({"operation": "move_forward", "robot_id": "dog4", "timeout": 12, "distance_m": 1}, "RESULT:")
    result = receipt(cancel_file)["result"]
    assert result["error"]["code"] == "cancelled"
    assert result["error"]["details"]["command_sent"] is True
    assert result["error"]["details"]["stop_confirmed"] is True
    assert result["error"]["details"]["control_released"] is True
    assert calls == ["drive", "stop", "release", "close"]
    assert {sig: signal.getsignal(sig) for sig in previous} == previous
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0
    assert probe._CLEANING_UP is False


def test_runtime_cancel_preserves_operation_and_does_not_switch_mode_or_discard(cancel_file, monkeypatch):
    calls = []
    class Session:
        def __init__(self, timeout): pass
        def execute(self, request):
            calls.append("wait")
            cancel_file.touch()
            probe.check_cancelled()
        def evidence(self): return {"command_sent": True, "operation_id": "epoch/runtime/9"}
        def close(self): calls.append("close")
    monkeypatch.setattr(runtime, "RuntimeSession", Session)
    runtime.runtime_worker({"operation": "set_runtime_idle", "robot_id": "dog4", "timeout": 12}, "RESULT:")
    error = receipt(cancel_file)["result"]["error"]
    assert error["code"] == "cancelled"
    assert error["details"]["operation_id"] == "epoch/runtime/9"
    assert error["details"]["retry_safe"] is False
    assert calls == ["wait", "close"]


@pytest.mark.parametrize("session_type", [posture.PostureSession, runtime.RuntimeSession])
def test_write_sessions_keep_ros_alive_for_cleanup_on_terminate(monkeypatch, session_type):
    observed = []
    class Node:
        def __init__(self, *args, **kwargs): pass
        def create_subscription(self, *args): return object()
        def create_publisher(self, *args): return object()
        def create_client(self, *args): return object()
    no_signals = object()
    modules = {name: ModuleType(name) for name in ["rclpy", "rclpy.node", "rclpy.signals", "rclpy.qos",
               "rosidl_runtime_py", "rosidl_runtime_py.convert", "rosidl_runtime_py.utilities"]}
    modules["rclpy"].init = lambda **kwargs: observed.append(kwargs["signal_handler_options"])
    modules["rclpy.node"].Node = Node
    modules["rclpy.signals"].SignalHandlerOptions = SimpleNamespace(NO=no_signals)
    modules["rclpy.qos"].QoSProfile = lambda **kwargs: kwargs
    modules["rclpy.qos"].ReliabilityPolicy = SimpleNamespace(RELIABLE=1, BEST_EFFORT=2)
    modules["rclpy.qos"].DurabilityPolicy = SimpleNamespace(VOLATILE=1, TRANSIENT_LOCAL=2)
    modules["rosidl_runtime_py.convert"].message_to_ordereddict = lambda message: {}
    modules["rosidl_runtime_py.utilities"].get_message = lambda typename: object
    modules["rosidl_runtime_py.utilities"].get_service = lambda typename: object
    for name, module in modules.items(): monkeypatch.setitem(sys.modules, name, module)
    session_type(12)
    assert observed == [no_signals]

"""Runtime identity, completion and unsaved-data contracts without ROS hardware."""
from collections import deque
from types import SimpleNamespace
import time

import pytest

from omni_tools.remote_probe import ProbeError
from omni_tools.remote_runtime import (RuntimeSession, check_runtime_preflight,
                                       runtime_worker, validate_runtime_request)

MAP = {"map_id": "map-a", "map_version": 1, "map_checksum": "a" * 64}


def request(operation="start_localization", **extra):
    return {"operation": operation, "robot_id": "dog4", "timeout": 12,
            **MAP, "initial_x": 0., "initial_y": 0., "initial_z": 0., "initial_yaw": 0., **extra}


def state(**extra):
    now = time.time_ns()
    return {"stamp": {"sec": now // 10**9, "nanosec": now % 10**9},
            "manager_epoch": "epoch", "operation_id": "epoch/runtime/1", "runtime_generation": 1,
            "mode": 0, "phase": 0, "ready": False, "mission_active": False,
            "recording_operation_id": "", "route_has_unsaved_data": False,
            "route_recording_state": 0, **MAP, **extra}


ROBOT = {"mission_state": 0, "estop_latched": False, "control_authority": 0}
AUTHORITY = {"base_owner_type": 0, "manual_override_active": False, "state": 1}


@pytest.mark.parametrize(("change", "error"), [
    ({"mission_active": True}, "mission_active"),
    ({"phase": 6}, "runtime_conflict"),
    ({"phase": 1}, "runtime_busy"),
    ({"phase": 5}, "runtime_error"),
    ({"recording_operation_id": "epoch/runtime/1"}, "unfinished_session"),
    ({"route_has_unsaved_data": True}, "unfinished_session"),
    ({"mode": 1}, "unfinished_session"),
])
def test_transitions_do_not_take_over_sessions(change, error):
    with pytest.raises(ProbeError) as caught:
        check_runtime_preflight(request(), state(**change), AUTHORITY, ROBOT)
    assert caught.value.code == error


def test_error_recovery_does_not_require_clearing_a_stale_recorder_state():
    check_runtime_preflight(request("set_runtime_idle"), state(phase=5, route_recording_state=4), AUTHORITY, ROBOT)


@pytest.mark.parametrize("phase", [1, 4, 5])
def test_exact_recording_can_be_explicitly_discarded_during_startup_or_error(phase):
    req = request("finish_route_recording", recording_operation_id="recording/old", disposition="discard")
    check_runtime_preflight(req, state(phase=phase, recording_operation_id="recording/old"), AUTHORITY, ROBOT)
    with pytest.raises(ProbeError, match="does not name"):
        check_runtime_preflight(req, state(phase=phase, recording_operation_id="recording/new"), AUTHORITY, ROBOT)


def test_single_point_save_is_rejected_without_freezing_the_recorder():
    req = request("finish_route_recording", recording_operation_id="recording/1", disposition="save")
    with pytest.raises(ProbeError) as caught:
        check_runtime_preflight(req, state(recording_operation_id="recording/1", route_point_count=1), AUTHORITY, ROBOT)
    assert caught.value.code == "insufficient_route_points"


def test_finish_does_not_treat_mapping_route_segments_as_standalone_recordings():
    req = request("finish_route_recording", recording_operation_id="mapping/1", disposition="discard")
    with pytest.raises(ProbeError) as caught:
        check_runtime_preflight(req, state(mode=1, phase=2, recording_operation_id="mapping/1"), AUTHORITY, ROBOT)
    assert caught.value.code == "unfinished_session"


@pytest.mark.parametrize(("robot", "authority", "code"), [
    ({**ROBOT, "estop_latched": True}, AUTHORITY, "estop_latched"),
    ({**ROBOT, "control_authority": 1}, AUTHORITY, "control_busy"),
    (ROBOT, {**AUTHORITY, "manual_override_active": True}, "control_busy"),
    (ROBOT, {**AUTHORITY, "state": 5}, "control_busy"),
])
def test_preflight_checks_operator_authority_and_estop(robot, authority, code):
    with pytest.raises(ProbeError) as caught:
        check_runtime_preflight(request(), state(), authority, robot)
    assert caught.value.code == code


@pytest.mark.parametrize("extra", [{"map_version": True}, {"map_checksum": "bad"},
                                    {"initial_x": float("nan")}, {"initial_z": 10**400},
                                    {"initial_yaw": float("inf")}, {"timeout": False}])
def test_invalid_fields_fail_before_ros(extra):
    with pytest.raises(ProbeError):
        validate_runtime_request(request(**extra))


def odom(stamp):
    return {"header": {"stamp": {"sec": int(stamp), "nanosec": int((stamp % 1) * 1e9)}, "frame_id": "omni_map"},
            "child_frame_id": "omni_base_link", "pose": {"pose": {
                "position": dict(x=0., y=0., z=0.), "orientation": dict(x=0., y=0., z=0., w=1.)}}}


def session(initial=None, updates=()):
    s = RuntimeSession.__new__(RuntimeSession)
    s.deadline = time.monotonic() + 2
    s.command_sent = False
    s.manager_epoch = s.operation_id = ""
    s.generation = None
    s.sent_at = 0.
    s.samples = {"runtime": initial or state(), "robot": {**ROBOT, "header": {"stamp": state()["stamp"]}},
                 "authority": {**AUTHORITY, "stamp": state()["stamp"]}, "tf": {"data": True}}
    s.received = {key: time.monotonic() for key in s.samples}
    s.odometry = deque(maxlen=3)
    s.node = SimpleNamespace(count_publishers=lambda _: 1)
    s.updates = deque(updates)
    s.polls = 0
    def spin_once(*args, **kwargs):
        s.polls += 1
        assert s.updates, "operation claimed ready before fresh evidence or stalled unexpectedly"
        s.samples["runtime"] = s.updates.popleft()
        s.received["runtime"] = time.monotonic()
        now = time.time()
        s.odometry = deque([(time.monotonic(), odom(now - offset)) for offset in (.06, .04, .02)], maxlen=3)
        s.samples["odom"] = s.odometry[-1][1]
        s.received["odom"] = time.monotonic()
    s.rclpy = SimpleNamespace(spin_once=spin_once)
    return s


def test_accepted_is_not_ready_and_failure_preserves_reason():
    s = session(updates=[state(operation_id="epoch/runtime/2", runtime_generation=2, phase=1),
                         state(operation_id="epoch/runtime/2", runtime_generation=2, phase=5,
                               reason_code=4012, reason_text="sensor timeout")])
    s.call = lambda *args, **kwargs: {"accepted": True, "operation_id": "epoch/runtime/2", "runtime_generation": 2}
    with pytest.raises(ProbeError) as caught:
        s.execute(request())
    assert s.polls == 2
    assert caught.value.code == "runtime_failed"
    assert caught.value.details["reason_code"] == 4012


def test_localization_waits_for_matching_generation_and_new_canonical_odometry():
    s = session(updates=[state(operation_id="epoch/runtime/2", runtime_generation=2, phase=1),
                         state(operation_id="epoch/runtime/2", runtime_generation=2, mode=2, phase=2, ready=True)])
    calls = []
    def call(endpoint, typename, fields, write=False):
        calls.append((endpoint, fields))
        s.command_sent, s.sent_at = True, time.monotonic()
        return {"accepted": True, "operation_id": "epoch/runtime/2", "runtime_generation": 2}
    s.call = call
    result = s.execute(request())
    assert s.polls == 2 and len(calls) == 1
    assert result["completion"] == "ready" and result["operation_id"] == "epoch/runtime/2"
    assert result["tf_ready"] is True
    assert calls[0][1]["map_checksum"] == MAP["map_checksum"]


@pytest.mark.parametrize("change,code", [({"manager_epoch": "new"}, "runtime_restarted"),
                                         ({"operation_id": "epoch/runtime/3", "runtime_generation": 3}, "operation_superseded"),
                                         ({"map_checksum": "b"*64}, "map_mismatch")])
def test_ready_from_another_operation_or_map_is_not_success(change, code):
    s = session(updates=[state(**{"operation_id":"epoch/runtime/2", "runtime_generation":2,
                                  "mode":2, "phase":2, "ready":True, **change})])
    s.call = lambda *args, **kwargs: {"accepted": True, "operation_id": "epoch/runtime/2", "runtime_generation": 2}
    with pytest.raises(ProbeError) as caught:
        s.execute(request())
    assert caught.value.code == code


@pytest.mark.parametrize("terminal_mode", [0, 2])
def test_save_checks_catalog_and_accepts_idle_if_localization_degrades_during_admission(terminal_mode):
    before = state(mode=5, phase=2, ready=True, recording_operation_id="recording/1", route_id="route-a", route_point_count=2)
    after = state(operation_id="epoch/runtime/2", runtime_generation=2, mode=terminal_mode,
                  phase=2 if terminal_mode else 0, ready=bool(terminal_mode))
    s = session(before, [after])
    calls = []
    def call(endpoint, typename, fields, write=False):
        calls.append(endpoint)
        if write:
            if terminal_mode == 0:
                # The recording identity is unchanged; ERROR still permits an
                # explicit SAVE but the provider will not retain localization.
                s.samples["runtime"] = {**before, "phase": 5, "ready": False}
                check_runtime_preflight(s.request, s.samples["runtime"], AUTHORITY, ROBOT)
            return {"accepted": True, "operation_id": "epoch/runtime/2", "runtime_generation": 2}
        return {"route_ids": ["route-a"], "map_ids": ["map-a"], "frame_ids": ["omni_map"], "created_at": ["now"],
                "map_versions": ["1"], "map_checksums": ["a"*64], "route_checksums": ["b"*64],
                "point_counts": [2], "distances_m": [.4]}
    s.call = call
    result = s.execute(request("finish_route_recording", recording_operation_id="recording/1", disposition="save"))
    assert result["saved_route"]["route_checksum"] == "b"*64
    assert result["completion"] == ("ready" if terminal_mode else "idle")
    assert calls == ["/omni/mission/runtime/finish_route_recording", "/omni/routes/list"]


def test_worker_failure_reports_identity_and_does_not_cleanup_or_retry(monkeypatch, capsys):
    class FailedSession:
        def __init__(self, timeout): pass
        def execute(self, req): raise ProbeError("timeout", "still preparing")
        def evidence(self): return {"operation_id": "epoch/runtime/9", "command_sent": True}
        def close(self): pass
    monkeypatch.setattr("omni_tools.remote_runtime.RuntimeSession", FailedSession)
    runtime_worker(request(), "RESULT:")
    import json
    error = json.loads(capsys.readouterr().out.removeprefix("RESULT:"))["error"]
    assert error["details"] == {"operation_id": "epoch/runtime/9", "command_sent": True, "retry_safe": False}

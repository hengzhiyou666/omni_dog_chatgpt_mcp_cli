"""Body-frame motion contracts, using simulated feedback only."""
import math
from types import SimpleNamespace

import pytest

from omni_tools import remote_motion as motion
from omni_tools.remote_probe import ProbeError
from test_remote_motion import Clock, FakeSession, NativeFeedbackSession


def request(operation="move", **changes):
    target = {"angle_deg": 90} if operation == "rotate" else {"distance_m": 0.2, "direction": "forward"}
    return {"operation": operation, "robot_id": "dog4", "timeout": 30,
            "measurement_mode": "timed", **target, **changes}


class ActionSession(FakeSession):
    def drive_timed_command(self, duration, client_id, x, y, yaw):
        self.calls.append(("timed_command", duration, x, y, yaw))
        if self.fail:
            raise ProbeError(self.fail, "Movement failed")


@pytest.mark.parametrize("direction,command,estimate", [
    ("forward", (0.35, 0.0, 0.0), 0.20),
    ("backward", (-0.25, 0.0, 0.0), 0.15),
    ("left", (0.0, 0.5, 0.0), 0.25),
    ("right", (0.0, -0.5, 0.0), 0.25),
])
def test_body_frame_move_and_unmeasured_receipt(direction, command, estimate):
    session = ActionSession()
    result = motion.execute_motion(request(direction=direction), session)
    call = next(call for call in session.calls if isinstance(call, tuple) and call[0] == "timed_command")
    assert call[1] == pytest.approx(0.2 / estimate)
    assert call[2:] == command
    assert session.calls[-2:] == ["stop", "release"]
    assert "origin" not in session.calls
    assert result["action"] == result["operation"] == "move"
    assert result["direction"] == direction
    assert result["target_distance_m"] == 0.2
    assert result["actual_distance_m"] is None
    assert result["measurement_mode"] == "timed"
    assert result["completion"] == "estimated_time"
    assert result["stop_confirmed"] is result["control_released"] is True
    assert result["stop_feedback_scope"] == "downstream_command"
    assert result["command_speed_mps"] == max(abs(value) for value in command)
    assert result["estimated_speed_mps"] == estimate


@pytest.mark.parametrize("direction,sign", [(None, -1), ("clockwise", -1), ("counterclockwise", 1)])
def test_rotation_defaults_clockwise_and_reports_estimated_angle(direction, sign):
    session = ActionSession()
    arguments = {} if direction is None else {"direction": direction}
    result = motion.execute_motion(request("rotate", **arguments), session)
    call = next(call for call in session.calls if isinstance(call, tuple) and call[0] == "timed_command")
    assert call[1] == pytest.approx(math.pi / 2 / 0.25)
    assert call[2:] == (0.0, 0.0, sign * 0.35)
    assert result["action"] == result["operation"] == "rotate"
    assert result["direction"] == (direction or "clockwise")
    assert result["target_angle_deg"] == 90
    assert result["actual_angle_deg"] is None
    assert result["command_yaw_rate_radps"] == 0.35
    assert result["estimated_yaw_rate_radps"] == 0.25
    assert result["target_duration_sec"] == call[1]
    assert session.calls[-2:] == ["stop", "release"]


@pytest.mark.parametrize("operation,key,value", [
    (operation, key, value)
    for operation, key, low, high in (("move", "distance_m", 0.1, 2.0), ("rotate", "angle_deg", 1, 360))
    for value in (True, False, None, "1", low - 0.01, high + 0.01, math.inf, math.nan, 10**1000)
])
def test_invalid_motion_targets_never_touch_transport(operation, key, value):
    session = ActionSession()
    with pytest.raises(ProbeError):
        motion.execute_motion(request(operation, **{key: value}), session)
    assert session.calls == []


@pytest.mark.parametrize("operation,changes", [
    ("move", {"direction": "clockwise"}), ("move", {"direction": None}),
    ("rotate", {"direction": "left"}), ("rotate", {"direction": []}),
    ("move", {"measurement_mode": "odometry"}), ("rotate", {"measurement_mode": "odometry"}),
    ("rotate", {"timeout": "30"}), ("move", {"timeout": True}),
])
def test_invalid_axis_or_mode_never_touches_transport(operation, changes):
    session = ActionSession()
    with pytest.raises(ProbeError):
        motion.execute_motion(request(operation, **changes), session)
    assert session.calls == []


@pytest.mark.parametrize("operation", ["move", "rotate"])
@pytest.mark.parametrize("failure", ["cancelled", "control_lost", "locomotion_stale"])
def test_new_motion_reuses_failure_stop_release_and_never_retries(operation, failure):
    session = ActionSession(fail=failure)
    with pytest.raises(ProbeError) as caught:
        motion.execute_motion(request(operation), session)
    assert session.calls.count("loco") == 1
    assert session.calls[-2:] == ["stop", "release"]
    assert caught.value.details["action"] == operation
    assert caught.value.details["retry_safe"] is False
    assert caught.value.details["stop_confirmed"] is True
    assert caught.value.details["control_released"] is True


class AxisFeedbackSession(NativeFeedbackSession):
    def __init__(self, clock, feedback=None):
        super().__init__(clock, ready_at=0)
        self.axes = []
        self.feedback_override = feedback

    def publish_velocity(self, client_id, speed, lateral=0.0, yaw_rate=0.0):
        super().publish_velocity(client_id, speed)
        self.axes.append((speed, lateral, 0.0, 0.0, 0.0, yaw_rate))

    def cycle(self, until=None):
        super().cycle(until)
        self.velocity = self.axes[-1] if self.feedback_override is None else self.feedback_override


@pytest.mark.parametrize("command", [(0.35, 0.0, 0.0), (-0.25, 0.0, 0.0),
                                     (0.0, 0.25, 0.0), (0.0, -0.25, 0.0),
                                     (0.0, 0.0, -0.35), (0.0, 0.0, 0.35)])
def test_timed_loop_matches_each_axis_sign_and_stops_all_axes(monkeypatch, command):
    clock = Clock()
    monkeypatch.setattr(motion.time, "monotonic", lambda: clock.now)
    session = AxisFeedbackSession(clock)
    session.measurement_mode = "timed"
    session.drive_timed_command(0.7, "app-test", *command)
    assert clock.now == pytest.approx(0.7)
    assert session.stop("app-test") is True
    assert session.axes[-1] == (0.0,) * 6
    assert session.deadline == 30


@pytest.mark.parametrize("feedback", [(0.25, 0, 0, 0, 0, 0), (0, -0.25, 0, 0, 0, 0),
                                      (0, 0.25, 0, 0, 0, 0.1), (0,) * 6,
                                      (0, math.nan, 0, 0, 0, 0)])
def test_timed_left_move_rejects_wrong_axis_sign_zero_and_invalid_feedback(monkeypatch, feedback):
    clock = Clock()
    monkeypatch.setattr(motion.time, "monotonic", lambda: clock.now)
    session = AxisFeedbackSession(clock, feedback=feedback)
    with pytest.raises(ProbeError) as caught:
        session.drive_timed_command(1, "app-test", 0, 0.25, 0)
    assert caught.value.code == "motion_unconfirmed"
    assert clock.now == pytest.approx(0.5)
    assert session.deadline == 30


def test_small_rotation_still_requires_matching_downstream_feedback(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(motion.time, "monotonic", lambda: clock.now)
    session = AxisFeedbackSession(clock)
    duration = math.radians(1) / 0.25
    session.drive_timed_command(duration, "app-test", 0, 0, -0.35)
    assert clock.now == pytest.approx(duration)


def test_insufficient_budget_rejects_before_nonzero_command_without_extending_timeout(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(motion.time, "monotonic", lambda: clock.now)
    session = AxisFeedbackSession(clock)
    session.deadline = 1
    with pytest.raises(ProbeError) as caught:
        session.drive_timed_command(1.01, "app-test", 0, 0, -0.35)
    assert caught.value.code == "timeout"
    assert session.axes == []
    assert session.deadline == 1


def test_teleop_publisher_maps_body_axes_and_zeroes_other_components():
    def vector():
        return SimpleNamespace(x=0.0, y=0.0, z=0.0)

    published = []
    session = motion.MotionSession.__new__(motion.MotionSession)
    session.sequence = 0
    session.node = SimpleNamespace(get_clock=lambda: SimpleNamespace(now=lambda: SimpleNamespace(to_msg=lambda: "stamp")))
    session.teleop_type = lambda: SimpleNamespace(header=SimpleNamespace(),
                                                 twist=SimpleNamespace(linear=vector(), angular=vector()))
    session.teleop = SimpleNamespace(publish=published.append)
    session.publish_velocity("app-test", -0.25, 0.25, -0.35)
    message = published[0]
    assert message.client_id == "app-test"
    assert message.header.frame_id == "base_link"
    assert vars(message.twist.linear) == {"x": -0.25, "y": 0.25, "z": 0.0}
    assert vars(message.twist.angular) == {"x": 0.0, "y": 0.0, "z": -0.35}

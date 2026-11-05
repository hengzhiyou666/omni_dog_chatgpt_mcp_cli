import math
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest

from omni_tools import remote_motion as motion
from omni_tools.remote_probe import ProbeError


def request(**updates):
    return {"operation": "move_forward", "robot_id": "dog4", "distance_m": 1.0,
            "timeout": 30, **updates}


class FakeSession:
    def __init__(self, fail=None, *, foreign=False, stopped=True, measured=0.98, release=True):
        self.fail, self.foreign = fail, foreign
        self.stopped, self.measured, self.released = stopped, measured, release
        self.calls = []

    def prepare(self):
        self.calls.append("prepare")

    def require_idle(self):
        if self.fail == "estop_latched":
            raise ProbeError(self.fail, "Estop latched")

    def require_available(self):
        pass

    def authority(self, op, client_id):
        self.calls.append(("authority", op, client_id))
        return {"accepted": True, "active_owner_type": 2 if self.foreign else 1,
                "active_client_id": "mission" if self.foreign else client_id}

    def wait_owned(self, client_id):
        if self.fail == "ownership_timeout":
            raise ProbeError("timeout", "Live ownership was not confirmed")

    def require_owned(self, client_id):
        pass

    def start_locomotion(self, client_id):
        self.calls.append("loco")
        if self.fail == "locomotion_timeout":
            raise ProbeError(self.fail, "No ack")
        return "success:loco"

    def stationary_origin(self, client_id):
        self.calls.append("origin")
        return {"x": 0, "y": 0}

    def drive(self, distance, client_id, origin):
        self.calls.append(("drive", distance))
        if self.fail:
            if self.fail == "unexpected":
                raise RuntimeError("Publisher failed")
            raise ProbeError(self.fail, "Movement failed")

    def stop(self, client_id):
        self.calls.append("stop")
        return self.stopped

    def measured_distance(self, origin):
        return self.measured

    def release(self, client_id):
        self.calls.append("release")
        return self.released

    def diagnostics(self):
        return {"native_locomotion": {"current_action": "FIXED_STAND"},
                "downstream_nonzero_samples": 60,
                "downstream_max_forward_command_mps": 0.1}


def test_one_move_measures_tracking_displacement_stops_then_releases():
    session = FakeSession()
    result = motion.execute_forward(request(), session)
    assert session.calls.count("loco") == 1
    assert session.calls[-2:] == ["stop", "release"]
    assert result["actual_distance_m"] == 0.98
    assert result["target_distance_m"] == 1.0
    assert result["stop_confirmed"] is result["control_released"] is True
    assert result["control_acquired"] is True
    assert result["authority_service"] == "/omni/control/authority"
    assert result["authority_owner_type"] == 1
    acquired_client = next(call[2] for call in session.calls if isinstance(call, tuple) and call[:2] == ("authority", 0))
    assert result["authority_client_id"] == acquired_client
    assert result["odometry_topic"] == "/odometry"
    assert "tracking-point" in result["measurement"]


@pytest.mark.parametrize("failure", ["locomotion_timeout", "timeout", "control_lost", "odometry_stale", "unexpected"])
def test_failure_never_retries_and_always_stops_and_releases(failure):
    session = FakeSession(fail=failure)
    with pytest.raises(ProbeError) as caught:
        motion.execute_forward(request(), session)
    assert session.calls.count("loco") == 1
    assert session.calls[-2:] == ["stop", "release"]
    assert caught.value.code == ("motion_failed" if failure == "unexpected" else failure)
    assert caught.value.details["retry_safe"] is False
    assert caught.value.details["stop_confirmed"] is True
    assert caught.value.details["control_released"] is True
    assert caught.value.details["control_acquired"] is True
    assert caught.value.details["motion_diagnostics"]["downstream_nonzero_samples"] == 60


def test_estop_prevents_authority_or_locomotion():
    session = FakeSession(fail="estop_latched")
    with pytest.raises(ProbeError, match="Estop") as caught:
        motion.execute_forward(request(), session)
    assert session.calls == ["prepare"]
    assert caught.value.details["control_acquired"] is False
    assert caught.value.details["authority_owner_type"] is None


def test_manual_override_is_rejected_without_sending_any_commands():
    session = FakeSession(foreign=True)
    with pytest.raises(ProbeError) as caught:
        motion.execute_forward(request(), session)
    assert caught.value.code == "control_denied"
    assert "loco" not in session.calls and "stop" not in session.calls
    assert session.calls[-1] == "release"
    assert caught.value.details["control_acquired"] is False
    assert caught.value.details["authority_owner_type"] is None


def test_accepted_service_without_observed_ownership_is_not_reported_acquired():
    session = FakeSession(fail="ownership_timeout")
    with pytest.raises(ProbeError) as caught:
        motion.execute_forward(request(), session)
    assert caught.value.code == "timeout"
    assert caught.value.details["control_acquired"] is False
    assert caught.value.details["command_sent"] is False
    assert caught.value.details["control_released"] is True
    assert "loco" not in session.calls


def test_rejected_service_is_not_reported_as_observed_ownership():
    class RejectedSession(FakeSession):
        def authority(self, op, client_id):
            response = super().authority(op, client_id)
            response["accepted"] = False
            return response

        def wait_owned(self, client_id):
            pytest.fail("A rejected acquisition must not wait for ownership")

    session = RejectedSession()
    with pytest.raises(ProbeError) as caught:
        motion.execute_forward(request(), session)
    assert caught.value.code == "control_denied"
    assert caught.value.details["control_acquired"] is False
    assert "loco" not in session.calls


@pytest.mark.parametrize("stopped,measured,code", [(False, 0.98, "stop_unconfirmed"),
                                               (True, None, "distance_unconfirmed"),
                                               (True, 0.8, "distance_unconfirmed")])
def test_no_success_without_stop_and_valid_target_measurement(stopped, measured, code):
    with pytest.raises(ProbeError) as caught:
        motion.execute_forward(request(), FakeSession(stopped=stopped, measured=measured))
    assert caught.value.code == code
    assert caught.value.details["actual_distance_m"] == measured


def test_release_failure_is_reported_without_concealing_confirmed_stop():
    result = motion.execute_forward(request(), FakeSession(release=False))
    assert result["stop_confirmed"] is True
    assert result["control_released"] is False
    assert result["control_acquired"] is True
    assert result["warnings"]


@pytest.mark.parametrize("distance", [True, False, None, "1", 0, 0.09, 2.01, math.inf, math.nan, 10**1000])
def test_invalid_distances_do_not_touch_transport(distance):
    session = FakeSession()
    with pytest.raises(ProbeError):
        motion.execute_forward(request(distance_m=distance), session)
    assert session.calls == []


@pytest.mark.parametrize("changes", [{"operation": "turn"}, {"robot_id": "dog\n4"},
                                     {"timeout": True}, {"timeout": math.inf}, {"timeout": 121}])
def test_other_invalid_requests(changes):
    with pytest.raises(ProbeError):
        motion.validate_motion_request(request(**changes))


def point(x=0, y=0, stamp=1000, *, yaw=0, roll=0, pitch=0, **updates):
    # Test poses use rotations around one axis, with wrap cases around yaw.
    q = (math.sin(roll / 2), 0, 0, math.cos(roll / 2)) if roll else (
        (0, math.sin(pitch / 2), 0, math.cos(pitch / 2)) if pitch else
        (0, 0, math.sin(yaw / 2), math.cos(yaw / 2)))
    return {"x": x, "y": y, "stamp": stamp, "received": stamp - 1000,
            "yaw": yaw, "quaternion": q, "frame": "map", "child": "odom", **updates}


@pytest.mark.parametrize("updates,code", [({"frame": "new_map"}, "odometry_frame_changed"),
                                        ({"child": "base_link"}, "odometry_frame_changed"),
                                        ({"stamp": 999}, "odometry_clock_changed"),
                                        ({"x": 0.2}, "odometry_jump")])
def test_odometry_changes_abort(updates, code):
    with pytest.raises(ProbeError) as caught:
        motion.check_odometry(point(), point(), point(**updates))
    assert caught.value.code == code


@pytest.mark.parametrize("updates,code", [({"yaw": 0.16}, "heading_changed"),
                                        ({"roll": 0.11}, "orientation_changed"),
                                        ({"pitch": 0.11}, "orientation_changed")])
def test_full_relative_orientation_is_bounded(updates, code):
    with pytest.raises(ProbeError) as caught:
        motion.check_odometry(point(), point(), point(**updates))
    assert caught.value.code == code


def test_absolute_slam_yaw_does_not_define_body_forward_and_wrap_is_valid():
    origin = point(yaw=math.pi - 0.01)
    current = point(x=0.01, stamp=1000.2, yaw=-math.pi + 0.01)
    motion.check_odometry(origin, origin, current)
    assert motion.horizontal_distance(origin, current) == 0.01


class Clock:
    now = 0.0


class SimulatedDrive(motion.MotionSession):
    def __init__(self, clock, *, stalled=False, downstream_zero=True, sampled_odometry=False):
        self.clock, self.stalled, self.downstream_zero = clock, stalled, downstream_zero
        self.deadline = 30
        self.current = point()
        self.odometry = deque([self.current], maxlen=40)
        self.speeds = []
        self.velocity = None
        self.velocity_received = -1
        self.physical_speed = 0.0
        self.sampled_odometry = sampled_odometry

    def _maintain(self, client_id):
        if self.clock.now >= self.deadline:
            raise ProbeError("timeout", "Movement timed out")

    def _fresh_odometry(self):
        return self.odometry[-1] if self.sampled_odometry else self.current

    def require_locomotion_ready(self):
        pass  # Distance-loop tests supply readiness separately from native ROS.

    def publish_velocity(self, client_id, speed):
        self.speeds.append(speed)

    def cycle(self):
        self.clock.now += motion.CONTROL_PERIOD
        speed = self.speeds[-1] if self.speeds else 0
        # A zero command is not an instantaneous physical stop: model the
        # ~0.09 m tail observed after real App input at this walking speed.
        self.physical_speed = speed if speed else self.physical_speed * 0.72
        self.current = point(self.current["x"] + (0 if self.stalled else self.physical_speed * motion.CONTROL_PERIOD),
                             stamp=1000 + self.clock.now, received=self.clock.now)
        # Five-Hz odometry, matching dog4's actual producer.
        if not self.odometry or self.clock.now - self.odometry[-1]["received"] >= 0.19:
            self.odometry.append(self.current)
        if self.downstream_zero:
            self.velocity = (speed, 0, 0, 0, 0, 0)
            self.velocity_received = self.clock.now


@pytest.mark.parametrize("sampled_odometry", [False, True])
def test_measured_drive_brakes_before_target_and_measures_stop_in_tolerance(monkeypatch, sampled_odometry):
    clock = Clock()
    monkeypatch.setattr(motion.time, "monotonic", lambda: clock.now)
    session = SimulatedDrive(clock, sampled_odometry=sampled_odometry)
    session.drive(1, "app-test", point())
    assert 0.88 <= session.current["x"] < 1.0
    assert set(session.speeds) == {0.35}
    assert session.speeds[-1] == 0.35
    assert session.stop("app-test") is True
    assert 0.95 <= session.current["x"] <= 1.05
    assert session.speeds[-1] == 0
    assert session.speeds.count(0) >= 5


def test_stalled_motion_times_out_on_progress_not_model_calls(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(motion.time, "monotonic", lambda: clock.now)
    session = SimulatedDrive(clock, stalled=True)
    with pytest.raises(ProbeError) as caught:
        session.drive(1, "app-test", point())
    assert caught.value.code == "motion_stalled"
    assert 3 <= clock.now < 3.2


def test_stopping_requires_new_downstream_zero_even_if_odometry_is_stationary(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(motion.time, "monotonic", lambda: clock.now)
    session = SimulatedDrive(clock, downstream_zero=False)
    session.velocity = (0,) * 6
    session.velocity_received = -1
    assert session.stop("app-test") is False
    assert session.speeds[-1] == 0
    assert session.deadline == 30


def test_stability_requires_multiple_distinct_samples_and_unchanged_frame():
    assert motion.MotionSession._stable([point()], 0, 0.5, 0.015) is None
    stable = [point(stamp=1000 + i / 5) for i in range(4)]
    assert motion.MotionSession._stable(stable, 0, 0.5, 0.015)
    stable[-1] = point(x=0.02, stamp=1000.6)
    assert motion.MotionSession._stable(stable, 0, 0.5, 0.015) is None


def test_stability_uses_latest_complete_window_after_braking():
    samples = [point(x=0, received=1.4),
               point(x=0.02, received=1.6),
               point(x=0.02, received=1.8),
               point(x=0.02, received=2.0)]
    assert motion.MotionSession._stable(samples, 1.25, 0.4, 0.015) == samples[1:]
    assert motion.MotionSession._stable(samples[:-1], 1.25, 0.4, 0.015) is None


class LateSettlingStop(SimulatedDrive):
    def __init__(self, clock, *, moving=False, stale=False, downstream=0.0):
        super().__init__(clock)
        self.moving, self.stale, self.downstream = moving, stale, downstream

    def cycle(self):
        self.clock.now = round(self.clock.now + motion.CONTROL_PERIOD, 9)
        now = self.clock.now
        self.current = point(x=min(now, math.inf if self.moving else 1.6) * 0.1,
                             stamp=1000 + now, received=now)
        if int(round(now * 10)) % 2 == 0:  # Five-Hz feedback.
            self.odometry.append(self.current)
        self.velocity = (self.downstream, 0, 0, 0, 0, 0)
        self.velocity_received = now

    def _fresh_odometry(self):
        if self.stale:
            raise ProbeError("odometry_stale", "Feedback is stale")
        return self.odometry[-1]


def test_stop_confirms_late_settling_without_increasing_timeout_or_tolerance(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(motion.time, "monotonic", lambda: clock.now)
    session = LateSettlingStop(clock)
    assert session.stop("app-test") is True
    assert clock.now == 2.0
    assert session.stop_feedback == {"reason": "confirmed", "elapsed_sec": 2.0}
    assert set(session.speeds) == {0.0}
    assert session.deadline == 30


@pytest.mark.parametrize("options,reason", [
    ({"moving": True}, "odometry_unstable"),
    ({"stale": True}, "odometry_stale"),
    ({"downstream": 0.1}, "downstream_zero_unconfirmed"),
])
def test_stop_never_confirms_moving_stale_or_nonzero_feedback(monkeypatch, options, reason):
    clock = Clock()
    monkeypatch.setattr(motion.time, "monotonic", lambda: clock.now)
    session = LateSettlingStop(clock, **options)
    assert session.stop("app-test") is False
    assert session.stop_feedback["reason"] == reason
    assert clock.now == 2.0
    assert session.speeds[-1] == 0.0
    assert session.deadline == 30


def test_stop_maintains_own_lease_without_reacquiring_authority(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(motion.time, "monotonic", lambda: clock.now)

    class OwnedStop(LateSettlingStop):
        _maintain = motion.MotionSession._maintain

        def remaining(self):
            return self.deadline - self.clock.now

        def require_idle(self):
            pass

        def require_owned(self, client_id):
            assert client_id == "app-test"

        def authority(self, operation, client_id):
            self.renewals.append((operation, client_id, self.clock.now))
            return {"accepted": True, "active_owner_type": 1, "active_client_id": client_id}

    session = OwnedStop(clock)
    session.renew_at, session.renewals = 0, []
    assert session.stop("app-test") is True
    assert session.renewals == [(2, "app-test", 0.0), (2, "app-test", 1.0)]


def test_origin_accepts_settled_window_after_initial_body_shift(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(motion.time, "monotonic", lambda: clock.now)

    class SettlingSession(SimulatedDrive):
        def spin(self):
            self.cycle()
            if self.clock.now >= 0.1:
                self.current["x"] = 0.05

    session = SettlingSession(clock)
    origin = session.stationary_origin("app-test")
    assert origin["x"] == pytest.approx(0.05)
    assert 0.5 <= clock.now < 1


def test_bootstrap_compiles_each_module_in_shared_namespace(monkeypatch):
    package = Path(motion.__file__).parent
    payload = {"request": request(), "marker": "RESULT:",
               "probe_source": (package / "remote_probe.py").read_text(),
               "posture_source": (package / "remote_posture.py").read_text(),
               "source": (package / "remote_motion.py").read_text()}
    captured = {}
    monkeypatch.setattr(motion, "runtime_environment", lambda: {})
    monkeypatch.setattr(motion.os, "execve", lambda exe, args, env: captured.update(args=args, env=env))
    motion.motion_bootstrap(payload)
    script = captured["args"][-1]
    # Compile/evaluate only the source definitions, never instantiate ROS worker.
    definitions, invocation = script.rsplit("n['motion_worker']", 1)
    namespace = {}
    exec(definitions, namespace)
    assert namespace["n"]["MotionSession"].__bases__[0] is namespace["n"]["PostureSession"]
    assert "move_forward" in invocation
    assert captured["env"]["PYTHONDONTWRITEBYTECODE"] == "1"


class NativeFeedbackSession(SimulatedDrive):
    require_locomotion_ready = motion.MotionSession.require_locomotion_ready

    def __init__(self, clock, *, ready_at=0.5, unsafe_at=None, action_after=None,
                 posture_after=None, unsafe_motion=2):
        super().__init__(clock)
        self.ready_at, self.unsafe_at, self.action_after = ready_at, unsafe_at, action_after
        self.posture_after, self.unsafe_motion = posture_after, unsafe_motion
        self.native_locomotion = None
        self.native_locomotion_received = 0
        self.native_locomotion_error = None
        self.native_action_transitions = deque(maxlen=8)
        self.downstream_nonzero_count = 0
        self.downstream_max_forward_mps = 0.0
        self.loco_ack = None
        self.loco_sent = False
        self.commands = []
        self.string_type = SimpleNamespace
        self.locomotion = SimpleNamespace(publish=lambda message: self.commands.append(message.data))
        self.node = SimpleNamespace(get_clock=lambda: SimpleNamespace(
            now=lambda: SimpleNamespace(nanoseconds=int((1000 + clock.now) * 1e9))))
        self.feedback()

    def feedback(self):
        ready = self.clock.now >= self.ready_at
        action = "RL" if ready else "FIXED_STAND"
        if self.action_after and self.clock.now >= self.action_after[0]:
            action = self.action_after[1]
        posture = 1 if ready else 3
        if self.posture_after and self.clock.now >= self.posture_after[0]:
            posture = self.posture_after[1]
        self._native_locomotion_status(SimpleNamespace(
            posture=posture,
            motion=self.unsafe_motion if self.unsafe_at is not None and self.clock.now >= self.unsafe_at else 0,
            current_action=action, stamp_ns=int((1000 + self.clock.now) * 1e9)))

    def cycle(self):
        super().cycle()
        self.feedback()
        # The Bridge acknowledges before the physical FSM transitions.
        if self.loco_sent:
            self.loco_ack = "success:loco"


def test_acknowledgement_cannot_start_forward_before_actual_standing_feedback(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(motion.time, "monotonic", lambda: clock.now)
    session = NativeFeedbackSession(clock, ready_at=0.5)
    assert session.start_locomotion("app-test") == "success:loco"
    assert clock.now >= 0.5
    assert session.commands == ["loco:app-test"]
    assert set(session.speeds) == {0.0}
    assert session._fresh_locomotion()["motion"] == 0  # Normal, not a walking indicator.


def test_ack_without_standing_feedback_never_permits_nonzero_velocity(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(motion.time, "monotonic", lambda: clock.now)
    session = NativeFeedbackSession(clock, ready_at=100)
    with pytest.raises(ProbeError) as caught:
        session.start_locomotion("app-test")
    assert caught.value.code == "locomotion_not_ready"
    assert set(session.speeds) == {0.0}
    assert 5 <= clock.now <= 5.1


@pytest.mark.parametrize("unsafe_motion", [1, 2])
def test_native_recovery_or_emergency_prevents_forward(monkeypatch, unsafe_motion):
    clock = Clock()
    monkeypatch.setattr(motion.time, "monotonic", lambda: clock.now)
    session = NativeFeedbackSession(clock, ready_at=0.5, unsafe_at=0.2, unsafe_motion=unsafe_motion)
    with pytest.raises(ProbeError) as caught:
        session.start_locomotion("app-test")
    assert caught.value.code == "locomotion_unsafe"
    assert set(session.speeds) == {0.0}


def test_native_posture_loss_during_distance_loop_prevents_further_forward(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(motion.time, "monotonic", lambda: clock.now)
    session = NativeFeedbackSession(clock, ready_at=0, posture_after=(0.3, 3))
    with pytest.raises(ProbeError) as caught:
        session.drive(1, "app-test", point())
    assert caught.value.code == "locomotion_not_ready"
    assert clock.now == pytest.approx(0.3)


def test_standing_idle_state_allows_input_without_guessing_action_names(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(motion.time, "monotonic", lambda: clock.now)
    session = NativeFeedbackSession(clock, ready_at=0, action_after=(0, "FIXED_STAND"))
    assert session.start_locomotion("app-test") == "success:loco"
    session.drive(1, "app-test", point())
    assert session.stop("app-test") is True
    assert session.current["x"] >= 0.97
    assert session.native_locomotion["current_action"] == "FIXED_STAND"


def test_replayed_native_status_cannot_refresh_readiness(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(motion.time, "monotonic", lambda: clock.now)
    session = NativeFeedbackSession(clock, ready_at=0)
    message = SimpleNamespace(**session.native_locomotion)
    clock.now = 0.9
    session._native_locomotion_status(message)
    with pytest.raises(ProbeError) as caught:
        session._fresh_locomotion()
    assert caught.value.code == "locomotion_stale"


def test_downstream_diagnostic_counts_commands_not_physical_velocity(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(motion.time, "monotonic", lambda: clock.now)
    session = NativeFeedbackSession(clock)
    for speed in (0.0, 0.1, 0.05, 0.0):
        session._velocity(SimpleNamespace(
            linear=SimpleNamespace(x=speed, y=0.0, z=0.0),
            angular=SimpleNamespace(x=0.0, y=0.0, z=0.0)))
    data = session.diagnostics()
    assert data["downstream_nonzero_samples"] == 2
    assert data["downstream_max_forward_command_mps"] == 0.1
    assert "not measured physical" in data["note"]


def test_action_transition_diagnostics_keep_return_transitions_and_are_bounded(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(motion.time, "monotonic", lambda: clock.now)
    session = NativeFeedbackSession(clock)
    for i in range(12):
        clock.now += 0.05
        session._native_locomotion_status(SimpleNamespace(
            posture=1, motion=0, current_action="RL" if i % 2 else "FIXED_STAND",
            stamp_ns=int((1000 + clock.now) * 1e9)))
    transitions = session.diagnostics()["native_action_transitions"]
    assert len(transitions) == 8
    assert [item["current_action"] for item in transitions] == ["FIXED_STAND", "RL"] * 4

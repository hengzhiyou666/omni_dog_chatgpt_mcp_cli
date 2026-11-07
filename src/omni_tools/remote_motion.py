"""One bounded body-frame movement through Bridge's APP authority.

SLAM's tracking-point displacement measures travel; its absolute yaw is never
treated as body heading. Body-forward velocity always uses typed TeleopCommand.
An explicit timed mode estimates travel without requiring SLAM odometry.
"""
from __future__ import annotations

from collections import deque
import math
import os
import signal
import sys
import time
import uuid

if globals().get("__package__"):
    from .remote_probe import (ENV_KEYS, ProbeError, _emit, _error,
                               runtime_environment, sanitize, utc_now, begin_cleanup,
                               check_cancelled, install_cancellation_handlers,
                               restore_cancellation_handlers)
    from .remote_posture import PostureSession


ODOMETRY_TOPIC = "/odometry"
TELEOP_TOPIC = "/omni/control/teleop"
LOCOMOTION_TOPIC = "/rosdeck/locomotion_command"
LOCOMOTION_STATUS_TOPIC = "/rosdeck/locomotion_status"
NATIVE_LOCOMOTION_STATUS_TOPIC = "/locomotion/status"
VELOCITY_TOPIC = "/vel_cmd"
ODOMETRY_MAX_AGE = 0.8
LOCOMOTION_MAX_AGE = 0.8
CONTROL_PERIOD = 0.1  # Match OmniDeck's default 10 Hz publication rate.
DISTANCE_TOLERANCE = 0.15
STOP_ALLOWANCE_M = 0.12  # Observed ~0.10 m tail travel plus odometry sampling.
TIMED_SPEED_MPS = 0.35  # Same input already verified to produce forward travel.
TIMED_ESTIMATED_SPEED_MPS = 0.20  # Ground-speed estimate from the first timed run.
TIMED_SIDE_SPEED_MPS = 0.25
TIMED_SIDE_ESTIMATED_SPEED_MPS = 0.15  # Initial backward estimate; not calibrated.
TIMED_LATERAL_SPEED_MPS = 0.5  # 0.25 did not move dog4; 0.5 was observed to strafe.
TIMED_LATERAL_ESTIMATED_SPEED_MPS = 0.25  # Initial ground-speed estimate; not calibrated.
TIMED_YAW_RATE_RADPS = 0.35
TIMED_ESTIMATED_YAW_RATE_RADPS = 0.25  # Initial rotation estimate; not calibrated.


def validate_motion_request(request):
    if not isinstance(request, dict) or request.get("operation") not in ("move_forward", "move", "rotate"):
        raise ProbeError("unsupported_operation", "Only bounded movement and rotation are allowed")
    operation = request["operation"]
    mode = request.get("measurement_mode", "odometry" if operation == "move_forward" else "timed")
    if mode not in (("odometry", "timed") if operation == "move_forward" else ("timed",)):
        raise ProbeError("invalid_argument", "measurement_mode must be odometry or timed")
    if operation == "move" and request.get("direction") not in ("forward", "backward", "left", "right"):
        raise ProbeError("invalid_argument", "Invalid movement direction")
    if operation == "rotate" and request.get("direction", "clockwise") not in ("clockwise", "counterclockwise"):
        raise ProbeError("invalid_argument", "Invalid rotation direction")
    target = ("angle_deg", 1, 360) if operation == "rotate" else ("distance_m", 0.1, 2.0)
    for key, lower, upper in (target, ("timeout", 1, 120)):
        value = request.get(key)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not lower <= value <= upper or not math.isfinite(value)):
            raise ProbeError("invalid_argument", "Invalid " + key)
    robot = request.get("robot_id")
    if not isinstance(robot, str) or not robot or len(robot) > 256 or any(ord(c) < 32 for c in robot):
        raise ProbeError("invalid_argument", "Invalid robot_id")


def timed_motion_command(request):
    """Return body X/Y/yaw input and explicit, unmeasured timing metadata."""
    if request["operation"] == "rotate":
        direction = request.get("direction", "clockwise")
        yaw = TIMED_YAW_RATE_RADPS * (-1 if direction == "clockwise" else 1)
        return (0.0, 0.0, yaw), {
            "command_yaw_rate_radps": TIMED_YAW_RATE_RADPS,
            "estimated_yaw_rate_radps": TIMED_ESTIMATED_YAW_RATE_RADPS,
            "target_duration_sec": math.radians(request["angle_deg"]) / TIMED_ESTIMATED_YAW_RATE_RADPS}
    direction = request.get("direction", "forward")
    speed = TIMED_SPEED_MPS if direction == "forward" else TIMED_SIDE_SPEED_MPS
    estimate = TIMED_ESTIMATED_SPEED_MPS if direction == "forward" else TIMED_SIDE_ESTIMATED_SPEED_MPS
    if direction in ("left", "right"):
        speed, estimate = TIMED_LATERAL_SPEED_MPS, TIMED_LATERAL_ESTIMATED_SPEED_MPS
    x, y = {"forward": (speed, 0.0), "backward": (-speed, 0.0),
            "left": (0.0, speed), "right": (0.0, -speed)}[direction]
    return (x, y, 0.0), {"command_speed_mps": speed, "estimated_speed_mps": estimate,
                           "target_duration_sec": request["distance_m"] / estimate}


def motion_bootstrap(payload):
    try:
        validate_motion_request(payload["request"])
        check_cancelled()
        environment = dict(os.environ)
        for key in ENV_KEYS:
            environment.pop(key, None)
        environment.update(runtime_environment())
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["RCUTILS_LOGGING_USE_STDOUT"] = "0"
        script = "n={'__name__':'omni_remote_motion'}; "
        for key, name in (("probe_source", "omni-read-probe"),
                          ("posture_source", "omni-posture"), ("source", "omni-motion")):
            script += "exec(compile(" + repr(payload[key]) + ",'<" + name + ">','exec'),n); "
        script += "n['motion_worker'](" + repr(payload["request"]) + "," + repr(payload["marker"]) + ")"
        os.execve(sys.executable, [sys.executable, "-B", "-u", "-c", script], environment)
    except Exception as error:
        failure = _error(error)
        failure["details"].update(command_sent=False, retry_safe=False)
        request = payload.get("request")
        operation = request.get("operation", "move_forward") if isinstance(request, dict) else "move_forward"
        _emit(payload.get("marker", "OMNI_TOOLS_RESULT:"), {"ok": False, "error": failure}, operation=operation)


def execute_forward(request, session):
    """Compatibility entry point for the original forward-only interface."""
    return execute_motion(request, session)


def execute_motion(request, session):
    """One attempt with unconditional bounded stop/release after locomotion starts."""
    validate_motion_request(request)
    operation = request["operation"]
    rotating = operation == "rotate"
    mode = request.get("measurement_mode", "odometry" if operation == "move_forward" else "timed")
    session.measurement_mode = mode
    timed = mode == "timed"
    target_key = "angle_deg" if rotating else "distance_m"
    target = request[target_key]
    command, timing = timed_motion_command(request) if timed else (None, {})
    client_id = "app-omni-" + uuid.uuid4().hex[:24]
    authority_requested = control_acquired = command_sent = False
    origin = result = failure = None
    stopped = False
    try:
        check_cancelled()
        session.prepare()
        session.require_idle()
        session.require_available()
        authority_requested = True
        response = session.authority(0, client_id)
        if (response.get("accepted") is not True or response.get("active_owner_type") != 1
                or response.get("active_client_id") != client_id):
            raise ProbeError("control_denied", "Movement requires exclusive APP control")
        session.wait_owned(client_id)
        # The service accepted the exact APP client and live ownership was
        # observed. Preserve this historical fact even after releasing it.
        control_acquired = True
        session.require_idle()
        session.require_owned(client_id)
        command_sent = True  # Delivery may be ambiguous if the publisher raises.
        ack = session.start_locomotion(client_id)
        if operation != "move_forward":
            session.drive_timed_command(timing["target_duration_sec"], client_id, *command)
        elif timed:
            session.drive_timed(request["distance_m"], client_id)
        else:
            origin = session.stationary_origin(client_id)
            session.drive(request["distance_m"], client_id, origin)
        result = {"robot_id": request["robot_id"], "action": operation,
                  "target_" + target_key: target, "executed": True,
                  "command_sent": True, "measurement_mode": mode,
                  "completion": "estimated_time" if timed else "measured_displacement",
                  "bridge_ack": ack,
                  "measurement": ("Elapsed time at a fixed body-frame command; distance and angle are not measured"
                                  if timed else "SLAM tracking-point horizontal net displacement"),
                  "observed_at": utc_now(), "warnings": []}
        if timed:
            result.update(timing)
            result["warnings"].append("Travel distance, rotation angle and physical stopping are not independently measured; stop feedback confirms downstream zero commands and healthy native locomotion only.")
            if rotating or (operation == "move" and request["direction"] != "forward"):
                result["warnings"].append("Backward, lateral and rotation speed estimates have not been calibrated on this robot.")
    except Exception as error:
        failure = error if isinstance(error, ProbeError) else ProbeError(
            "motion_failed", "Movement failed; actual outcome is not confirmed",
            {"exception_type": type(error).__name__})
    finally:
        with begin_cleanup():
            if command_sent:
                try:
                    stopped = session.stop(client_id) is True
                except Exception:
                    stopped = False
            measured = None
            if origin is not None:
                try:
                    measured = session.measured_distance(origin)
                except Exception:
                    pass
            if result is not None and (not stopped or (not timed and (measured is None
                                       or abs(measured - request["distance_m"]) > DISTANCE_TOLERANCE))):
                failure = ProbeError("stop_unconfirmed" if not stopped else "distance_unconfirmed",
                                     "Movement completion could not be confirmed")
            released = False
            if authority_requested:
                try:
                    released = session.release(client_id) is True
                except Exception:
                    pass
            details = {"command_sent": command_sent, "stop_confirmed": stopped,
                       "control_acquired": control_acquired,
                       "authority_service": "/omni/control/authority",
                       "authority_client_id": client_id,
                       "authority_owner_type": 1 if control_acquired else None,
                       "control_released": released, "actual_" + target_key: measured,
                       "target_" + target_key: target, "measurement_mode": mode,
                       "odometry_topic": None if timed else ODOMETRY_TOPIC}
            if timed:
                details["stop_feedback_scope"] = "downstream_command"
                details.update(timing)
            if operation != "move_forward":
                details.update(action=operation, operation=operation,
                               direction=request.get("direction", "clockwise"))
            try:
                details["motion_diagnostics"] = session.diagnostics()
            except Exception:
                # Diagnostics must never prevent stop/release or conceal the cause.
                pass
            if failure is not None:
                failure.details.update(details)
                failure.details["retry_safe"] = False
            elif result is not None:
                result.update(details)
                if not released:
                    result["warnings"].append("Control release was not acknowledged; its bounded lease must expire.")
    if failure is not None:
        raise failure
    return result


def motion_worker(request, marker):
    session = lock_file = None
    handlers = None
    try:
        import fcntl
        validate_motion_request(request)
        handlers = install_cancellation_handlers()
        check_cancelled()
        lock_file = open("/tmp/omni-tools-posture.lock", "a")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ProbeError("action_in_progress", "Another Omni movement or posture request is running") from None
        def timed_out(_signum, _frame):
            raise ProbeError("timeout", "Movement exceeded its time limit", {"retry_safe": False})
        signal.signal(signal.SIGALRM, timed_out)
        signal.setitimer(signal.ITIMER_REAL, request["timeout"] + 3.5)
        session = MotionSession(request["timeout"])
        _emit(marker, {"ok": True, "data": execute_motion(request, session)}, operation=request["operation"])
    except Exception as error:
        failure = _error(error)
        if session is None:
            failure["details"].setdefault("command_sent", False)
        operation = request.get("operation", "move_forward") if isinstance(request, dict) else "move_forward"
        _emit(marker, {"ok": False, "error": failure}, operation=operation)
    finally:
        try:
            if session is not None:
                session.close()
        finally:
            if lock_file is not None:
                lock_file.close()
            signal.setitimer(signal.ITIMER_REAL, 0)
            restore_cancellation_handlers(handlers)


def horizontal_distance(first, second):
    return math.hypot(second["x"] - first["x"], second["y"] - first["y"])


def check_odometry(origin, previous, current):
    """Reject changed frames, discontinuities and relative heading drift."""
    if (current["frame"], current["child"]) != (origin["frame"], origin["child"]):
        raise ProbeError("odometry_frame_changed", "Odometry frame changed during movement")
    yaw_delta = math.atan2(math.sin(current["yaw"] - origin["yaw"]),
                           math.cos(current["yaw"] - origin["yaw"]))
    if abs(yaw_delta) > 0.15:
        raise ProbeError("heading_changed", "Relative heading exceeded the straight-movement limit")
    # A fixed sensor's lever arm moves when the body tilts, even without travel.
    # Bound full relative rotation and roll/pitch, not SLAM's absolute heading.
    ox, oy, oz, ow = origin["quaternion"]
    cx, cy, cz, cw = current["quaternion"]
    x = ow * cx - ox * cw - oy * cz + oz * cy
    y = ow * cy + ox * cz - oy * cw - oz * cx
    z = ow * cz - ox * cy + oy * cx - oz * cw
    w = ow * cw + ox * cx + oy * cy + oz * cz
    rotation = 2 * math.acos(min(1.0, abs(w)))
    roll = math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
    pitch = math.asin(max(-1.0, min(1.0, 2 * (w * y - z * x))))
    if rotation > 0.15 or abs(roll) > 0.10 or abs(pitch) > 0.10:
        raise ProbeError("orientation_changed", "Relative body rotation invalidates straight travel measurement")
    elapsed = current["stamp"] - previous["stamp"]
    if elapsed < 0:
        raise ProbeError("odometry_clock_changed", "Odometry timestamp moved backwards")
    if horizontal_distance(previous, current) > min(0.12, max(0.03, 0.35 * elapsed + 0.02)):
        raise ProbeError("odometry_jump", "Odometry discontinuity prevents reliable distance measurement")


class MotionSession(PostureSession):
    measurement_mode = "odometry"

    def __init__(self, timeout):
        super().__init__(timeout)
        from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
        from rosidl_runtime_py.utilities import get_message
        self.odometry = deque(maxlen=40)
        self.odom_error = None
        self.velocity = None
        self.velocity_received = 0
        self.stop_feedback = None
        self.downstream_nonzero_count = 0
        self.downstream_max_forward_mps = 0.0
        self.native_locomotion = None
        self.native_locomotion_received = 0
        self.native_locomotion_error = None
        self.native_action_transitions = deque(maxlen=8)
        self.sequence = 0
        self.loco_sent = False
        self.loco_ack = None
        self.renew_at = 0
        qos = QoSProfile(depth=10, reliability=ReliabilityPolicy.BEST_EFFORT,
                         durability=DurabilityPolicy.VOLATILE)
        self.teleop_type = get_message("omni_robot_interfaces/msg/TeleopCommand")
        self.teleop = self.node.create_publisher(self.teleop_type, TELEOP_TOPIC, 1)
        self.locomotion = self.node.create_publisher(self.string_type, LOCOMOTION_TOPIC, 1)
        self.subscriptions.append(self.node.create_subscription(
            get_message("nav_msgs/msg/Odometry"), ODOMETRY_TOPIC, self._odometry, qos))
        self.subscriptions.append(self.node.create_subscription(
            get_message("geometry_msgs/msg/Twist"), VELOCITY_TOPIC, self._velocity, qos))
        self.subscriptions.append(self.node.create_subscription(
            self.string_type, LOCOMOTION_STATUS_TOPIC, self._locomotion_ack, 10))
        self.subscriptions.append(self.node.create_subscription(
            get_message("software_msgs/msg/LocomotionStatus"), NATIVE_LOCOMOTION_STATUS_TOPIC,
            self._native_locomotion_status, qos))

    def _odometry(self, message):
        try:
            p, q = message.pose.pose.position, message.pose.pose.orientation
            values = (p.x, p.y, q.x, q.y, q.z, q.w)
            norm = sum(value * value for value in values[2:])
            if not all(math.isfinite(value) for value in values) or not 0.9 <= norm <= 1.1:
                raise ValueError("invalid pose")
            stamp = message.header.stamp.sec + message.header.stamp.nanosec / 1e9
            if not math.isfinite(stamp) or stamp <= 0 or not message.header.frame_id or not message.child_frame_id:
                raise ValueError("invalid header")
            if self.odometry and stamp == self.odometry[-1]["stamp"]:
                return  # Replayed timestamps cannot refresh the freshness gate.
            self.odometry.append({"x": p.x, "y": p.y,
                                  "yaw": math.atan2(2 * (q.w * q.z + q.x * q.y),
                                                    norm - 2 * (q.y * q.y + q.z * q.z)),
                                  "quaternion": tuple(value / math.sqrt(norm) for value in values[2:]),
                                  "stamp": stamp, "received": time.monotonic(),
                                  "frame": message.header.frame_id, "child": message.child_frame_id})
        except (AttributeError, TypeError, ValueError):
            self.odom_error = "Malformed odometry cannot be used for distance measurement"

    def _velocity(self, message):
        self.velocity = tuple(getattr(vector, axis) for vector in (message.linear, message.angular)
                              for axis in ("x", "y", "z"))
        self.velocity_received = time.monotonic()
        if all(math.isfinite(value) for value in self.velocity):
            if any(abs(value) > 0.005 for value in self.velocity):
                self.downstream_nonzero_count += 1
            self.downstream_max_forward_mps = max(self.downstream_max_forward_mps, self.velocity[0])

    def _native_locomotion_status(self, message):
        try:
            status = {"posture": message.posture, "motion": message.motion,
                      "current_action": message.current_action, "stamp_ns": message.stamp_ns}
            if (type(status["posture"]) is not int or type(status["motion"]) is not int
                    or not isinstance(status["current_action"], str)
                    or not status["current_action"] or len(status["current_action"]) > 128
                    or type(status["stamp_ns"]) is not int or status["stamp_ns"] <= 0):
                raise ValueError("Invalid locomotion feedback")
            if self.native_locomotion is not None:
                if status["stamp_ns"] == self.native_locomotion["stamp_ns"]:
                    return
                if status["stamp_ns"] < self.native_locomotion["stamp_ns"]:
                    raise ValueError("Locomotion feedback clock moved backwards")
            self.native_locomotion = status
            self.native_locomotion_received = time.monotonic()
            if (not self.native_action_transitions
                    or self.native_action_transitions[-1]["current_action"] != status["current_action"]):
                self.native_action_transitions.append(dict(status))
        except (AttributeError, TypeError, ValueError):
            self.native_locomotion_error = "Invalid native locomotion feedback"

    def _fresh_locomotion(self):
        if self.native_locomotion_error:
            raise ProbeError("locomotion_invalid", self.native_locomotion_error)
        if (self.native_locomotion is None
                or time.monotonic() - self.native_locomotion_received > LOCOMOTION_MAX_AGE):
            raise ProbeError("locomotion_stale", "Fresh native locomotion feedback is required")
        status = self.native_locomotion
        age = (self.node.get_clock().now().nanoseconds - status["stamp_ns"]) / 1e9
        if not -0.25 <= age <= LOCOMOTION_MAX_AGE:
            raise ProbeError("locomotion_stale", "Native locomotion timestamp is outside the freshness limit")
        if status["motion"] in (1, 2):
            raise ProbeError("locomotion_unsafe", "Robot entered recovery or emergency motion state")
        return status

    def _locomotion_is_ready(self, status):
        # MOTION_IDLE=0 is the ordinary FSM state, not a walking detector.
        # Zero input may legitimately retain FIXED_STAND until locomotion
        # receives forward input. Do not require an unverified action name.
        return status["posture"] == 1 and status["motion"] == 0

    def require_locomotion_ready(self):
        if not self._locomotion_is_ready(self._fresh_locomotion()):
            raise ProbeError("locomotion_not_ready", "Robot must be standing in its normal motion state")

    def diagnostics(self):
        return {"native_locomotion_topic": NATIVE_LOCOMOTION_STATUS_TOPIC,
                "native_locomotion": self.native_locomotion,
                "native_action_transitions": list(self.native_action_transitions),
                "downstream_command_topic": VELOCITY_TOPIC,
                "downstream_nonzero_samples": self.downstream_nonzero_count,
                "downstream_max_forward_command_mps": self.downstream_max_forward_mps,
                "stop_feedback": getattr(self, "stop_feedback", None),
                "note": "Downstream commands are not measured physical robot velocity."}

    def _locomotion_ack(self, message):
        if self.loco_sent and self.loco_ack is None and message.data.split(":", 2)[:2] in (
                ["success", "loco"], ["error", "loco"]):
            self.loco_ack = message.data

    def _fresh_odometry(self):
        if self.odom_error:
            raise ProbeError("odometry_invalid", self.odom_error)
        if not self.odometry or time.monotonic() - self.odometry[-1]["received"] > ODOMETRY_MAX_AGE:
            raise ProbeError("odometry_stale", "Fresh SLAM odometry is required")
        sample = self.odometry[-1]
        clock_age = self.node.get_clock().now().nanoseconds / 1e9 - sample["stamp"]
        if not -0.25 <= clock_age <= ODOMETRY_MAX_AGE:
            raise ProbeError("odometry_stale", "Odometry timestamp is outside the robot-clock freshness limit")
        return sample

    def prepare(self):
        super().prepare()
        end = min(self.deadline, time.monotonic() + 4)
        while ((self.measurement_mode == "odometry" and not self.odometry)
               or self.native_locomotion is None or self.teleop.get_subscription_count() < 1
               or self.locomotion.get_subscription_count() < 1):
            if time.monotonic() >= end:
                raise ProbeError("motion_unavailable", "Forward movement feedback or Bridge subscribers are unavailable")
            self.require_idle()
            self.spin()
        if self.measurement_mode == "odometry":
            self._fresh_odometry()
        self._fresh_locomotion()

    def _maintain(self, client_id):
        self.remaining()
        self.require_idle()
        self.require_owned(client_id)
        if time.monotonic() >= self.renew_at:
            response = self.authority(2, client_id)
            if (response.get("accepted") is not True or response.get("active_owner_type") != 1
                    or response.get("active_client_id") != client_id):
                raise ProbeError("control_lost", "Bridge did not renew exclusive movement control")
            self.renew_at = time.monotonic() + 1

    def authority(self, operation, client_id):
        # A wedged renewal must not hold the previous nonzero velocity until
        # the overall task deadline; return promptly to the zero-stop path.
        previous_deadline = self.deadline
        if operation == 2:
            self.deadline = min(self.deadline, time.monotonic() + 0.35)
        try:
            return super().authority(operation, client_id)
        finally:
            self.deadline = previous_deadline

    def cycle(self, until=None):
        end = min(self.deadline, time.monotonic() + CONTROL_PERIOD)
        if until is not None:
            end = min(end, until)
        while time.monotonic() < end:
            self.rclpy.spin_once(self.node, timeout_sec=max(0.0, min(self.remaining(), end - time.monotonic())))

    def start_locomotion(self, client_id):
        if self.loco_sent:
            raise ProbeError("action_in_progress", "Locomotion command has already been sent")
        self._maintain(client_id)
        self.publish_velocity(client_id, 0.0)
        self.loco_sent = True
        command = self.string_type()
        command.data = "loco:" + client_id
        commanded_at = time.monotonic()
        self.locomotion.publish(command)
        end = min(self.deadline, time.monotonic() + 5)
        while True:
            self._maintain(client_id)
            if self.measurement_mode == "odometry":
                self._fresh_odometry()
            status = self._fresh_locomotion()
            if self.loco_ack is not None and self.loco_ack != "success:loco":
                raise ProbeError("locomotion_rejected", "Bridge rejected locomotion entry", {"bridge_ack": self.loco_ack})
            if (self.loco_ack == "success:loco" and self.native_locomotion_received > commanded_at
                    and self._locomotion_is_ready(status)):
                return self.loco_ack
            if time.monotonic() >= end:
                raise ProbeError("locomotion_timeout" if self.loco_ack is None else "locomotion_not_ready",
                                 "Bridge acknowledgement and actual standing feedback are both required")
            self.publish_velocity(client_id, 0.0)
            self.cycle()

    @staticmethod
    def _stable(samples, since, duration, tolerance):
        recent = [item for item in samples if item["received"] >= since]
        # Select the latest complete window, not earlier braking samples. The
        # nanosecond allowance only avoids float rounding at the time boundary.
        for first in range(len(recent) - 3, -1, -1):
            if recent[-1]["received"] - recent[first]["received"] >= duration - 1e-9:
                recent = recent[first:]
                break
        else:
            return None
        if any((item["frame"], item["child"]) != (recent[0]["frame"], recent[0]["child"])
               or horizontal_distance(recent[0], item) > tolerance for item in recent):
            return None
        return recent

    def stationary_origin(self, client_id):
        start = time.monotonic()
        while True:
            self._maintain(client_id)
            latest = self._fresh_odometry()
            samples = self._stable(self.odometry, max(start, time.monotonic() - 0.85), 0.5, 0.015)
            if samples:
                origin = dict(latest)
                origin["x"] = sum(item["x"] for item in samples) / len(samples)
                origin["y"] = sum(item["y"] for item in samples) / len(samples)
                return origin
            if time.monotonic() - start > 4:
                raise ProbeError("odometry_unstable", "Robot tracking point did not settle before movement")
            self.spin()

    def publish_velocity(self, client_id, speed, lateral=0.0, yaw_rate=0.0):
        self.sequence += 1
        message = self.teleop_type()
        message.header.stamp = self.node.get_clock().now().to_msg()
        message.header.frame_id = "base_link"
        message.client_id, message.sequence = client_id, self.sequence
        message.twist.linear.x = float(speed)
        message.twist.linear.y = float(lateral)
        message.twist.angular.z = float(yaw_rate)
        self.teleop.publish(message)

    def drive(self, distance, client_id, origin):
        previous = origin
        best = 0.0
        progressed_at = time.monotonic()
        while True:
            check_cancelled()
            self._maintain(client_id)
            self.require_locomotion_ready()
            current = self._fresh_odometry()
            check_odometry(origin, previous, current)
            progress = horizontal_distance(origin, current)
            if progress > distance + DISTANCE_TOLERANCE:
                raise ProbeError("distance_exceeded", "Measured displacement exceeded the requested distance")
            if progress >= max(distance * 0.5, distance - STOP_ALLOWANCE_M):
                return
            if progress >= best + 0.005:
                best, progressed_at = progress, time.monotonic()
            if time.monotonic() - progressed_at > 3:
                raise ProbeError("motion_stalled", "No measured forward progress for three seconds")
            # Match observed successful OmniDeck forward input (0.34..0.39).
            # Smaller 0.10/0.20 inputs reached the SDK but produced no travel.
            self.publish_velocity(client_id, 0.35)
            previous = current
            self.cycle()

    def measured_distance(self, origin):
        current = self._fresh_odometry()
        if (current["frame"], current["child"]) != (origin["frame"], origin["child"]):
            raise ProbeError("odometry_frame_changed", "Final odometry frame differs from the origin")
        return round(horizontal_distance(origin, current), 4)

    def drive_timed(self, distance, client_id):
        self.drive_timed_command(distance / TIMED_ESTIMATED_SPEED_MPS, client_id, TIMED_SPEED_MPS)

    def drive_timed_command(self, duration, client_id, speed, lateral=0.0, yaw_rate=0.0):
        started = time.monotonic()
        end = started + duration
        if end > self.deadline + 1e-9:
            raise ProbeError("timeout", "Requested movement does not fit the remaining time budget",
                             {"required_duration_sec": duration,
                              "remaining_duration_sec": max(0.0, self.deadline - started)})
        previous_deadline = self.deadline
        self.deadline = end
        expected = (speed, lateral, 0.0, 0.0, 0.0, yaw_rate)
        matched = False
        try:
            while time.monotonic() < end:
                check_cancelled()
                self._maintain(client_id)
                self.require_locomotion_ready()
                if time.monotonic() >= end:
                    break
                if lateral or yaw_rate:
                    self.publish_velocity(client_id, speed, lateral, yaw_rate)
                else:
                    self.publish_velocity(client_id, speed)
                self.cycle(until=end)
                matched = (self.velocity is not None and self.velocity_received > started
                           and time.monotonic() - self.velocity_received <= 0.4
                           and all(math.isfinite(actual) and (
                               abs(actual) > 0.005 and actual * wanted > 0 if wanted
                               else abs(actual) <= 0.005)
                               for actual, wanted in zip(self.velocity, expected)))
                if not matched and time.monotonic() - started >= 0.5:
                    raise ProbeError("motion_unconfirmed", "Downstream command does not confirm the requested axis and direction")
            if not matched:
                raise ProbeError("motion_unconfirmed", "Downstream command did not confirm the requested axis and direction")
        finally:
            self.deadline = previous_deadline

    def stop(self, client_id):
        """Require downstream zero; measured mode additionally requires settled odometry."""
        previous_deadline = self.deadline
        start = time.monotonic()
        self.deadline = start + 2
        self.stop_feedback = {"reason": "awaiting_feedback"}
        try:
            while time.monotonic() < self.deadline:
                self.publish_velocity(client_id, 0.0)
                self._maintain(client_id)
                self.cycle()
                try:
                    if self.measurement_mode == "timed":
                        self.require_locomotion_ready()
                        samples = True
                    else:
                        self._fresh_odometry()
                        samples = self._stable(self.odometry, max(start, time.monotonic() - 0.75), 0.4, 0.015)
                except ProbeError as error:
                    self.stop_feedback["reason"] = error.code
                    continue
                self.stop_feedback["reason"] = "odometry_unstable" if not samples else "downstream_zero_unconfirmed"
                if (time.monotonic() - start >= 0.6 and samples and self.velocity is not None
                        and self.velocity_received >= start
                        and time.monotonic() - self.velocity_received <= 0.4
                        and all(math.isfinite(value) and abs(value) <= 0.005 for value in self.velocity)):
                    self.stop_feedback["reason"] = "confirmed"
                    return True
            return False
        except ProbeError as error:
            self.stop_feedback["reason"] = error.code
            return False
        finally:
            self.stop_feedback["elapsed_sec"] = round(time.monotonic() - start, 3)
            try:
                self.publish_velocity(client_id, 0.0)
            finally:
                self.deadline = previous_deadline

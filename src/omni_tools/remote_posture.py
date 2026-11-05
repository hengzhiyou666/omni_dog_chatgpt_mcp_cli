"""Bounded stand/lie-down commands through the running robot's Bridge.

The SSH adapter streams this module and the unchanged read probe to Python.
Only Bridge's typed authority service and fixed posture topic are writable;
vendor services, velocity commands, mission controls and E-stop resets are not.
"""
from __future__ import annotations

import math
import os
import signal
import sys
import time
import uuid

# When streamed, the read probe is compiled first in the same namespace.
if globals().get("__package__"):
    from .remote_probe import (ENV_KEYS, ProbeError, _emit, _error,
                               runtime_environment, sanitize, utc_now)


POSTURES = frozenset({"stand", "lie_down"})
AUTHORITY_SERVICE = "/omni/control/authority"
COMMAND_TOPIC = "/rosdeck/posture_command"
STATUS_TOPIC = "/rosdeck/posture_status"
STATE_TOPICS = {
    "robot": ("/omni/robot_state", "omni_robot_interfaces/msg/RobotState"),
    "mission": ("/omni/mission/status", "omni_robot_interfaces/msg/MissionStatus"),
    "runtime": ("/omni/mission/runtime/status", "omni_robot_interfaces/msg/AutonomyRuntimeStatus"),
    "authority": ("/omni/control/authority/status", "omni_robot_interfaces/msg/ControlAuthorityStatus"),
}


def validate_posture_request(request):
    if not isinstance(request, dict) or request.get("operation") != "set_posture":
        raise ProbeError("unsupported_operation", "Only fixed posture operations are allowed")
    if not isinstance(request.get("posture"), str) or request["posture"] not in POSTURES:
        raise ProbeError("invalid_argument", "Posture must be stand or lie_down")
    timeout = request.get("timeout")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 1 <= timeout <= 120:
        raise ProbeError("invalid_argument", "Invalid posture timeout")
    robot = request.get("robot_id")
    if not isinstance(robot, str) or not robot or len(robot) > 256 or any(ord(c) < 32 for c in robot):
        raise ProbeError("invalid_argument", "Invalid robot_id")


def posture_bootstrap(payload):
    """Set the live runtime's library paths before importing ROS Python."""
    try:
        validate_posture_request(payload["request"])
        environment = dict(os.environ)
        for key in ENV_KEYS:
            environment.pop(key, None)
        environment.update(runtime_environment())
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["RCUTILS_LOGGING_USE_STDOUT"] = "0"
        # Compile separately: both modules start with future imports.
        script = (
            "n={'__name__':'omni_remote_posture'}; "
            "exec(compile(" + repr(payload["probe_source"]) + ",'<omni-read-probe>','exec'),n); "
            "exec(compile(" + repr(payload["source"]) + ",'<omni-posture>','exec'),n); "
            "n['posture_worker'](" + repr(payload["request"]) + "," + repr(payload["marker"]) + ")"
        )
        os.execve(sys.executable, [sys.executable, "-B", "-u", "-c", script], environment)
    except Exception as error:
        _emit(payload.get("marker", "OMNI_TOOLS_RESULT:"), {"ok": False, "error": _error(error)})


def execute_posture(request, session):
    """One attempt; ambiguous outcomes must never cause an automatic resend."""
    validate_posture_request(request)
    command = request["posture"]
    client_id = "app-omni-" + uuid.uuid4().hex[:24]
    authority_requested = False
    sent = False
    result = None
    failure = None
    try:
        session.prepare()
        session.require_idle()
        session.require_available()
        authority_requested = True
        response = session.authority(0, client_id)
        if not response.get("accepted"):
            raise ProbeError("control_denied", "Bridge did not grant posture control", {
                "reason": response.get("reason_text", ""), "command_sent": False})
        if response.get("active_owner_type") != 1 or response.get("active_client_id") != client_id:
            raise ProbeError("control_denied", "Posture requires exclusive control, not manual override", {
                "command_sent": False})
        session.wait_owned(client_id)
        session.require_idle()
        session.require_owned(client_id)
        # Mark before publishing: a transport exception can leave delivery unknown.
        sent = True
        session.publish_once(command, client_id)
        ack = session.wait_ack(command, client_id)
        result = {
            "robot_id": request["robot_id"], "action": command,
            "requested_posture": "standing" if command == "stand" else "lying",
            "executed": True, "command_sent": True,
            "completion": "vendor_acknowledged", "bridge_ack": ack,
            "physical_posture_confirmed": False, "observed_at": utc_now(),
            "warnings": ["Bridge acknowledged the vendor service; physical posture is not confirmed by telemetry."],
        }
    except Exception as error:
        if not isinstance(error, ProbeError):
            error = ProbeError("posture_failed", "Posture operation failed; actual outcome is not confirmed", {
                "exception_type": type(error).__name__})
        failure = error
        if isinstance(error, ProbeError):
            error.details.setdefault("command_sent", sent)
            if sent:
                error.details.setdefault("physical_posture_confirmed", False)
                error.details.setdefault("retry_safe", False)
    finally:
        if authority_requested:
            try:
                released = session.release(client_id)
            except Exception:
                released = False
            if result is not None:
                result["control_released"] = bool(released)
                if not released:
                    result["warnings"].append("Control release was not acknowledged; its bounded lease must expire.")
            elif isinstance(failure, ProbeError):
                failure.details["control_released"] = bool(released)
    if failure is not None:
        raise failure
    return result


def posture_worker(request, marker):
    session = None
    lock_file = None
    try:
        import fcntl
        validate_posture_request(request)
        lock_file = open("/tmp/omni-tools-posture.lock", "a")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ProbeError("action_in_progress", "Another Omni posture request is running") from None
        def timed_out(_signum, _frame):
            raise ProbeError("timeout", "Posture request exceeded its time limit", {"retry_safe": False})
        signal.signal(signal.SIGALRM, timed_out)
        signal.setitimer(signal.ITIMER_REAL, request["timeout"] + 3)
        session = PostureSession(request["timeout"])
        data = execute_posture(request, session)
        _emit(marker, {"ok": True, "data": data})
    except Exception as error:
        _emit(marker, {"ok": False, "error": _error(error)})
    finally:
        try:
            if session is not None:
                session.close()
        finally:
            if lock_file is not None:
                lock_file.close()
            signal.setitimer(signal.ITIMER_REAL, 0)


class PostureSession:
    """ROS transport with fresh-state gates and an independently bounded cleanup."""

    def __init__(self, timeout):
        try:
            import rclpy
            from rclpy.node import Node
            from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
            from rosidl_runtime_py.convert import message_to_ordereddict
            from rosidl_runtime_py.utilities import get_message, get_service
        except ImportError:
            raise ProbeError("ros_unavailable", "Robot ROS Python modules are unavailable") from None
        self.rclpy = rclpy
        self.convert = message_to_ordereddict
        self.deadline = time.monotonic() + timeout
        self.samples = {}
        self.received = {}
        self.ack = None
        self.sent = False
        self.command = None
        self.subscriptions = []
        rclpy.init(args=["--ros-args", "--disable-external-lib-logs", "--disable-rosout-logs", "--disable-stdout-logs"])
        self.node = Node("omni_tools_posture_" + str(os.getpid()), enable_rosout=False,
                         start_parameter_services=False, use_global_arguments=False)
        qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT,
                         durability=DurabilityPolicy.VOLATILE)
        for key, (topic, type_name) in STATE_TOPICS.items():
            def observe(message, key=key):
                self.samples[key] = sanitize(self.convert(message))
                self.received[key] = time.monotonic()
            self.subscriptions.append(self.node.create_subscription(get_message(type_name), topic, observe, qos))
        self.string_type = get_message("std_msgs/msg/String")
        self.subscriptions.append(self.node.create_subscription(self.string_type, STATUS_TOPIC, self._acknowledge, 10))
        self.publisher = self.node.create_publisher(self.string_type, COMMAND_TOPIC, 1)
        self.authority_type = get_service("omni_robot_interfaces/srv/ControlAuthority")
        self.client = self.node.create_client(self.authority_type, AUTHORITY_SERVICE)

    def remaining(self):
        value = self.deadline - time.monotonic()
        if value <= 0:
            raise ProbeError("timeout", "Posture acknowledgement timed out", {"retry_safe": False})
        return value

    def spin(self):
        self.rclpy.spin_once(self.node, timeout_sec=min(0.05, self.remaining()))

    def prepare(self):
        end = min(self.deadline, time.monotonic() + 4)
        while len(self.samples) < len(STATE_TOPICS) or self.publisher.get_subscription_count() < 1:
            if time.monotonic() >= end:
                raise ProbeError("status_unavailable", "Fresh robot, mission, runtime and authority status are required")
            self.spin()

    def _fresh(self, key):
        if key not in self.samples or time.monotonic() - self.received.get(key, 0) > 2.5:
            raise ProbeError("status_stale", "Robot control state is not fresh", {"state": key})
        return self.samples[key]

    def require_idle(self):
        robot, mission, runtime = (self._fresh(key) for key in ("robot", "mission", "runtime"))
        if robot.get("estop_latched") is not False:
            raise ProbeError("estop_latched", "Posture is blocked by the emergency-stop state")
        if robot.get("charging") is not False:
            raise ProbeError("robot_busy", "Posture is blocked while charging or charge state is unknown")
        if (robot.get("operational_mode") != 0 or robot.get("mission_state") not in (0, 4, 5, 6, 7)
                or mission.get("state") not in (0, 4, 5, 6, 7)
                or runtime.get("mode") != 0 or runtime.get("desired_mode") != 0
                or runtime.get("phase") != 0 or runtime.get("mission_active") is not False):
            raise ProbeError("robot_busy", "Posture requires idle runtime and no active or paused task")

    def require_available(self):
        authority = self._fresh("authority")
        if (authority.get("state") != 1 or authority.get("base_owner_type") != 0
                or authority.get("base_client_id") != ""
                or authority.get("manual_override_active") is not False):
            raise ProbeError("control_busy", "Another controller owns or is switching robot control")

    def require_owned(self, client_id):
        authority = self._fresh("authority")
        if (authority.get("state") != 3 or authority.get("base_owner_type") != 1
                or authority.get("base_client_id") != client_id
                or authority.get("manual_override_active") is not False):
            raise ProbeError("control_lost", "Exclusive posture control is no longer held")

    def authority(self, operation, client_id):
        if not self.client.wait_for_service(timeout_sec=min(1, self.remaining())):
            raise ProbeError("service_unavailable", "Bridge authority service is unavailable")
        request = self.authority_type.Request()
        request.op, request.owner_type = operation, 1
        request.client_id, request.lease_sec = client_id, 5.0
        request.reason = "omni_tools_posture"
        future = self.client.call_async(request)
        while not future.done():
            self.spin()
        if future.exception() is not None or future.result() is None:
            raise ProbeError("service_failed", "Bridge authority request returned no result")
        return sanitize(self.convert(future.result()))

    def wait_owned(self, client_id):
        while True:
            authority = self._fresh("authority")
            if (authority.get("state") == 3 and authority.get("base_owner_type") == 1
                    and authority.get("base_client_id") == client_id):
                self.require_owned(client_id)
                return
            self.require_idle()
            self.spin()

    def publish_once(self, command, client_id):
        if self.sent:
            raise ProbeError("action_in_progress", "Posture command has already been sent")
        self.command = command
        self.ack = None
        self.sent = True
        message = self.string_type()
        message.data = command + ":" + client_id
        self.publisher.publish(message)

    def _acknowledge(self, message):
        if not self.sent or self.ack is not None:
            return
        fields = message.data.split(":", 2)
        if len(fields) >= 2 and fields[0] in ("success", "error") and fields[1] == self.command:
            self.ack = message.data

    def wait_ack(self, command, client_id):
        renew_at = time.monotonic() + 1
        while True:
            self.require_idle()
            self.require_owned(client_id)
            if self.ack is not None:
                if self.ack == "success:" + command:
                    return self.ack
                raise ProbeError("posture_rejected", "Bridge rejected the posture command", {"bridge_ack": self.ack})
            if time.monotonic() >= renew_at:
                response = self.authority(2, client_id)
                if (not response.get("accepted") or response.get("active_owner_type") != 1
                        or response.get("active_client_id") != client_id):
                    raise ProbeError("control_lost", "Bridge did not renew exclusive posture control")
                renew_at = time.monotonic() + 1
            self.spin()

    def release(self, client_id):
        previous = self.deadline
        self.deadline = time.monotonic() + 1
        try:
            return self.authority(1, client_id).get("accepted") is True
        finally:
            self.deadline = previous

    def close(self):
        self.node.destroy_node()
        self.rclpy.shutdown()

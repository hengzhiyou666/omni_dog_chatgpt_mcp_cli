"""Fixed Mission runtime operations; accepted requests are awaited, never retried."""
from __future__ import annotations

from collections import deque
import math
import os
import re
import signal
import sys
import time
import uuid

if globals().get("__package__"):
    from .remote_probe import (ENV_KEYS, ProbeError, _emit, _error,
                               catalog_rows, runtime_environment, sanitize, utc_now)


RUNTIME_OPERATIONS = frozenset({"start_localization", "start_route_recording",
                                "finish_route_recording", "set_runtime_idle"})


def validate_runtime_request(request):
    if (not isinstance(request, dict) or not isinstance(request.get("operation"), str)
            or request["operation"] not in RUNTIME_OPERATIONS):
        raise ProbeError("unsupported_operation", "Unsupported runtime operation")
    def text(key, operation_id=False):
        value = request.get(key)
        if (not isinstance(value, str) or not value or len(value) > 256
                or any(ord(c) < 32 for c in value)
                or (not operation_id and (value in {".", ".."} or "/" in value or "\\" in value))):
            raise ProbeError("invalid_argument", "Invalid " + key)
    text("robot_id")
    timeout = request.get("timeout")
    if type(timeout) not in {int, float} or not 1 <= timeout <= 120:
        raise ProbeError("invalid_argument", "Invalid runtime timeout")
    if request["operation"] in {"start_localization", "start_route_recording"}:
        text("map_id")
        if type(request.get("map_version")) is not int or not 1 <= request["map_version"] <= 4294967295:
            raise ProbeError("invalid_argument", "Invalid map_version")
        if not isinstance(request.get("map_checksum"), str) or not re.fullmatch(r"[0-9a-fA-F]{64}", request["map_checksum"]):
            raise ProbeError("invalid_argument", "Invalid map_checksum")
        for key in ("initial_x", "initial_y", "initial_z", "initial_yaw"):
            value = request.get(key)
            if type(value) not in {int, float} or not -3.402823466e38 <= value <= 3.402823466e38:
                raise ProbeError("invalid_argument", "Invalid " + key)
    if request["operation"] == "start_route_recording":
        text("route_id")
    if request["operation"] == "finish_route_recording":
        text("recording_operation_id", operation_id=True)
        if request.get("disposition") not in ("save", "discard"):
            raise ProbeError("invalid_argument", "disposition must be save or discard")


def runtime_bootstrap(payload):
    try:
        validate_runtime_request(payload["request"])
        environment = dict(os.environ)
        for key in ENV_KEYS:
            environment.pop(key, None)
        environment.update(runtime_environment())
        environment.update(PYTHONDONTWRITEBYTECODE="1", RCUTILS_LOGGING_USE_STDOUT="0")
        script = "n={'__name__':'omni_remote_runtime'}; "
        for key, name in (("probe_source", "omni-read-probe"), ("source", "omni-runtime")):
            script += "exec(compile(" + repr(payload[key]) + ",'<" + name + ">','exec'),n); "
        script += "n['runtime_worker'](" + repr(payload["request"]) + "," + repr(payload["marker"]) + ")"
        os.execve(sys.executable, [sys.executable, "-B", "-u", "-c", script], environment)
    except Exception as error:
        _emit(payload.get("marker", "OMNI_TOOLS_RESULT:"), {"ok": False, "error": _error(error)})


def check_runtime_preflight(request, runtime, authority, robot):
    """Gate fixed transitions without taking a control lease or discarding data."""
    if runtime.get("mission_active") or robot.get("mission_state") in (1, 2, 3):
        raise ProbeError("mission_active", "An active mission owns the runtime")
    if robot.get("estop_latched"):
        raise ProbeError("estop_latched", "Robot E-stop is latched")
    if (robot.get("control_authority") != 0 or authority.get("base_owner_type") != 0
            or authority.get("manual_override_active") or authority.get("state") != 1):
        raise ProbeError("control_busy", "Runtime operations require available control authority")
    if runtime.get("phase") == 6:
        raise ProbeError("runtime_conflict", "External runtime conflict must be resolved first")
    if runtime.get("mode") == 1:
        raise ProbeError("unfinished_session", "Finish mapping with explicit SAVE or DISCARD first")
    if request["operation"] == "finish_route_recording":
        if runtime.get("recording_operation_id") != request["recording_operation_id"]:
            raise ProbeError("recording_mismatch", "Recording operation does not name the current session")
        if request["disposition"] == "save" and runtime.get("route_point_count", 0) < 2:
            raise ProbeError("insufficient_route_points", "Record at least two route points before saving")
    else:
        if runtime.get("recording_operation_id") or runtime.get("route_has_unsaved_data") or runtime.get("mode") == 5:
            raise ProbeError("unfinished_session", "Finish mapping/recording with explicit SAVE or DISCARD first")
        if runtime.get("phase") in (1, 3, 4):
            raise ProbeError("runtime_busy", "A runtime transition is already running")
        if runtime.get("phase") == 5 and request["operation"] != "set_runtime_idle":
            raise ProbeError("runtime_error", "Recover the runtime to idle before starting another operation")


def runtime_worker(request, marker):
    session = None
    try:
        validate_runtime_request(request)
        def timed_out(_signum, _frame):
            raise ProbeError("timeout", "Runtime operation exceeded its time limit")
        signal.signal(signal.SIGALRM, timed_out)
        signal.setitimer(signal.ITIMER_REAL, request["timeout"] + 1)
        session = RuntimeSession(request["timeout"])
        _emit(marker, {"ok": True, "data": session.execute(request)})
    except Exception as error:
        if not isinstance(error, ProbeError):
            error = ProbeError("runtime_failed", "Runtime operation failed", {"exception_type": type(error).__name__})
        error.details.update(session.evidence() if session is not None else {"command_sent": False})
        error.details["retry_safe"] = False
        _emit(marker, {"ok": False, "error": _error(error)})
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        if session is not None:
            try:
                session.close()
            except Exception:
                pass


class RuntimeSession:
    def __init__(self, timeout):
        try:
            import rclpy
            from rclpy.node import Node
            from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
            from rosidl_runtime_py.convert import message_to_ordereddict
            from rosidl_runtime_py.utilities import get_message, get_service
        except ImportError:
            raise ProbeError("ros_unavailable", "Robot ROS Python modules are unavailable") from None
        self.rclpy, self.convert, self.get_service = rclpy, message_to_ordereddict, get_service
        self.deadline = time.monotonic() + timeout
        self.samples, self.received = {}, {}
        self.odometry = deque(maxlen=3)
        self.command_sent = False
        self.operation_id = self.manager_epoch = ""
        self.request_id = ""
        self.sent_at = 0.0
        self.generation = None
        rclpy.init(args=["--ros-args", "--disable-external-lib-logs", "--disable-rosout-logs", "--disable-stdout-logs"])
        self.node = Node("omni_tools_runtime_" + str(os.getpid()), enable_rosout=False,
                         start_parameter_services=False, use_global_arguments=False)
        live = QoSProfile(depth=10, reliability=ReliabilityPolicy.BEST_EFFORT, durability=DurabilityPolicy.VOLATILE)
        latched = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.subscriptions = []
        for key, topic, typename, qos in [
            ("runtime", "/omni/mission/runtime/status", "omni_robot_interfaces/msg/AutonomyRuntimeStatus", live),
            ("robot", "/omni/robot_state", "omni_robot_interfaces/msg/RobotState", live),
            ("authority", "/omni/control/authority/status", "omni_robot_interfaces/msg/ControlAuthorityStatus", live),
            ("tf", "/omni/tf_manager/ready", "std_msgs/msg/Bool", latched),
            ("odom", "/omni/tf_manager/body_odom_global", "nav_msgs/msg/Odometry", live),
        ]:
            def observe(message, key=key):
                now = time.monotonic()
                self.samples[key] = sanitize(self.convert(message))
                self.received[key] = now
                if key == "odom":
                    self.odometry.append((now, self.samples[key]))
            self.subscriptions.append(self.node.create_subscription(get_message(typename), topic, observe, qos))

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ProbeError("timeout", "Runtime operation exceeded its time limit")
        return remaining

    def wait(self, predicate):
        while True:
            value = predicate()
            if value:
                return value
            self.rclpy.spin_once(self.node, timeout_sec=min(0.1, self.remaining()))

    @staticmethod
    def stamp(message):
        value = message.get("header", message).get("stamp", {})
        return value.get("sec", 0) + value.get("nanosec", 0) / 1e9

    def fresh(self, key):
        return (key in self.samples and time.monotonic() - self.received[key] <= 2.5
                and abs(time.time() - self.stamp(self.samples[key])) <= 2.5)

    def evidence(self):
        return {"command_sent": self.command_sent, "operation_id": self.operation_id,
                "request_id": getattr(self, "request_id", ""),
                "manager_epoch": self.manager_epoch, "runtime_status": self.samples.get("runtime"),
                "tf_ready": self.samples.get("tf", {}).get("data"),
                "odom_age_seconds": (time.time() - self.stamp(self.samples["odom"])) if "odom" in self.samples else None}

    def call(self, endpoint, typename, fields, write=False):
        service_type = self.get_service(typename)
        client = self.node.create_client(service_type, endpoint)
        try:
            if not client.wait_for_service(timeout_sec=min(4.0, self.remaining())):
                raise ProbeError("service_unavailable", "Robot service is unavailable", {"service": endpoint})
            if write:
                # Check again after service discovery so an intervening request is not silently taken over.
                self.rclpy.spin_once(self.node, timeout_sec=0)
                self.preflight(self.request)
                self.command_sent, self.sent_at = True, time.monotonic()
            request = service_type.Request()
            for key, value in fields.items():
                if key in {"requested_at", "deadline"}:
                    target = getattr(request, key)
                    target.sec, target.nanosec = divmod(value, 1000000000)
                else:
                    setattr(request, key, value)
            future = client.call_async(request)
            self.wait(future.done)
            if future.exception() is not None or future.result() is None:
                raise ProbeError("service_failed", "Robot service returned no result", {"service": endpoint})
            return sanitize(self.convert(future.result()))
        finally:
            self.node.destroy_client(client)

    def preflight(self, request):
        self.wait(lambda: all(self.fresh(k) for k in ("runtime", "robot", "authority")))
        runtime = self.samples["runtime"]
        if self.manager_epoch and (runtime.get("manager_epoch") != self.manager_epoch
                                  or runtime.get("operation_id") != self.baseline_operation):
            raise ProbeError("runtime_changed", "Another request changed the runtime; inspect its current operation")
        check_runtime_preflight(request, runtime, self.samples["authority"], self.samples["robot"])
        return dict(runtime)

    def canonical_ready(self):
        if (not self.samples.get("tf", {}).get("data")
                or self.node.count_publishers("/omni/tf_manager/ready") < 1 or not self.fresh("odom")
                or len(self.odometry) < 3 or any(t < self.sent_at for t, _ in self.odometry)):
            return False
        stamps = []
        for _, sample in self.odometry:
            if sample.get("header", {}).get("frame_id") != "omni_map" or sample.get("child_frame_id") != "omni_base_link":
                return False
            pose = sample.get("pose", {}).get("pose", {})
            values = [pose.get("position", {}).get(k) for k in ("x", "y", "z")]
            values += [pose.get("orientation", {}).get(k) for k in ("x", "y", "z", "w")]
            if any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
                return False
            stamps.append(self.stamp(sample))
        return all(b > a for a, b in zip(stamps, stamps[1:]))

    def terminal(self, target_mode, map_identity):
        if not self.fresh("runtime"):
            return False
        status = self.samples["runtime"]
        if status.get("manager_epoch") != self.manager_epoch:
            raise ProbeError("runtime_restarted", "Mission Manager restarted during the operation")
        if status.get("runtime_generation", 0) > self.generation:
            raise ProbeError("operation_superseded", "Another runtime operation superseded this request")
        if status.get("operation_id") != self.operation_id or status.get("runtime_generation") != self.generation:
            return False
        if status.get("phase") in (5, 6):
            raise ProbeError("runtime_failed", status.get("reason_text") or "Runtime operation failed",
                             {"reason_code": status.get("reason_code")})
        # SAVE keeps localization only if it is still READY when the server
        # admits the request. The same recording may degrade after preflight.
        if self.request["operation"] == "finish_route_recording" and self.request["disposition"] == "save":
            if status.get("mode") not in (0, 2):
                return False
            target_mode = status["mode"]
        if (status.get("mode") != target_mode or status.get("phase") != (0 if target_mode == 0 else 2)
                or bool(status.get("ready")) != bool(target_mode)):
            return False
        if target_mode:
            if any(status.get(k) != v for k, v in map_identity.items()):
                raise ProbeError("map_mismatch", "Ready runtime does not match the requested map")
            if not self.canonical_ready():
                return False
        if target_mode == 5:
            if (status.get("recording_operation_id") != self.operation_id or status.get("route_id") != self.request["route_id"]
                    or status.get("route_recording_state") != 2 or status.get("route_point_count", 0) < 1):
                return False
        elif self.request["operation"] == "finish_route_recording":
            if status.get("recording_operation_id") or status.get("route_has_unsaved_data") or status.get("route_recording_state") != 0:
                return False
        return dict(status)

    def execute(self, request):
        self.request = request
        before = self.preflight(request)
        self.manager_epoch, self.baseline_operation = before["manager_epoch"], before["operation_id"]
        token = uuid.uuid4().hex
        self.request_id = "omni-runtime-" + token
        now = time.time_ns()
        fields = {"request_id": self.request_id, "sequence": 1,
                  "source": "cli:omni_tools:" + token, "requested_at": now,
                  "deadline": now + int(self.remaining() * 1e9)}
        map_identity = {k: before.get(k) for k in ("map_id", "map_version", "map_checksum")}
        finishing = request["operation"] == "finish_route_recording"
        if finishing:
            target_mode = 2 if request["disposition"] == "save" and before.get("phase") == 2 and before.get("mode") == 5 and before.get("ready") else 0
            fields.update(recording_operation_id=request["recording_operation_id"], disposition=1 if request["disposition"] == "save" else 2)
            endpoint, typename = "/omni/mission/runtime/finish_route_recording", "omni_robot_interfaces/srv/FinishRouteRecording"
        else:
            target_mode = {"start_localization": 2, "start_route_recording": 5, "set_runtime_idle": 0}[request["operation"]]
            fields.update(desired_mode=target_mode, timeout_sec=float(self.remaining()))
            if target_mode:
                map_identity = {k: request[k] for k in ("map_id", "map_version", "map_checksum")}
                map_identity["map_checksum"] = map_identity["map_checksum"].lower()
                fields.update(map_identity)
                fields.update({k: float(request[k]) for k in ("initial_x", "initial_y", "initial_z", "initial_yaw")})
            if target_mode == 5:
                fields["route_id"] = request["route_id"]
            endpoint, typename = "/omni/mission/runtime/set_mode", "omni_robot_interfaces/srv/SetAutonomyMode"
        response = self.call(endpoint, typename, fields, write=True)
        if not response.get("accepted"):
            raise ProbeError("runtime_rejected", response.get("reason_text") or "Runtime request was rejected",
                             {"reason_code": response.get("reason_code")})
        self.operation_id, self.generation = response.get("operation_id", ""), response.get("runtime_generation")
        if not self.operation_id or type(self.generation) is not int:
            raise ProbeError("invalid_robot_response", "Accepted runtime request has no operation identity")
        status = self.wait(lambda: self.terminal(target_mode, map_identity))
        result = {"robot_id": request["robot_id"], "completion": "idle" if status["mode"] == 0 else "ready",
                  "ready": status["ready"], "recording_operation_id": status.get("recording_operation_id", ""),
                  "observed_at": utc_now(), **self.evidence()}
        if finishing and request["disposition"] == "save":
            catalog = self.call("/omni/routes/list", "omni_robot_interfaces/srv/ListRoutes", {})
            routes, _ = catalog_rows(catalog, "routes")
            saved = next((r for r in routes if r["route_id"] == before["route_id"]), None)
            if (not saved or any(str(saved.get(k)) != str(v) for k, v in map_identity.items())
                    or not saved.get("route_checksum") or saved.get("point_count", 0) < 2):
                raise ProbeError("route_save_unconfirmed", "Saved route was not confirmed in the route catalog")
            result["saved_route"] = saved
        return result

    def close(self):
        self.node.destroy_node()
        self.rclpy.shutdown()

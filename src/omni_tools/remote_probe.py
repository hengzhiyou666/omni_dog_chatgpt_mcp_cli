"""Self-contained ROS read probe, streamed over SSH without remote installation.

Keep imports from the robot ROS installation inside RosReader. This module is
also imported by offline tests on hosts that do not have ROS installed.
"""
from __future__ import annotations

import json
import math
import os
import signal
import sys
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path


ENV_KEYS = frozenset({
    "AMENT_PREFIX_PATH", "COLCON_PREFIX_PATH", "CMAKE_PREFIX_PATH", "LD_LIBRARY_PATH",
    "PYTHONPATH", "ROS_DISTRO", "ROS_VERSION", "ROS_PYTHON_VERSION", "ROS_DOMAIN_ID",
    "ROS_LOCALHOST_ONLY", "RMW_IMPLEMENTATION", "CYCLONEDDS_URI",
    "FASTRTPS_DEFAULT_PROFILES_FILE", "FASTDDS_DEFAULT_PROFILES_FILE",
    "ROS_SECURITY_ENABLE", "ROS_SECURITY_STRATEGY", "ROS_SECURITY_KEYSTORE",
    "ROS_SECURITY_ENCLAVE_OVERRIDE", "RMW_FASTRTPS_USE_QOS_FROM_XML",
    "RMW_IMPLEMENTATION_SERIALIZATION_FORMAT", "RMW_IMPLEMENTATION_TYPE",
    "ZENOH_ROUTER_CHECK_ATTEMPTS", "ZENOH_SESSION_CONFIG_URI",
})
TOPICS = {
    "robot_state": ("/omni/robot_state", "omni_robot_interfaces/msg/RobotState"),
    "mission_status": ("/omni/mission/status", "omni_robot_interfaces/msg/MissionStatus"),
    "runtime_status": ("/omni/mission/runtime/status", "omni_robot_interfaces/msg/AutonomyRuntimeStatus"),
    "navigation_status": ("/omni/mission/navigation/status", "omni_robot_interfaces/msg/NavigationStatus"),
    "slam_status": ("/omni/slam/status", "omni_tf_manager/msg/SlamStatus"),
    "control_authority_status": ("/omni/control/authority/status", "omni_robot_interfaces/msg/ControlAuthorityStatus"),
}
LOCALIZATION_TOPICS = {
    "tf_ready_status": ("/omni/tf_manager/ready", "std_msgs/msg/Bool"),
    "body_odom_global": ("/omni/tf_manager/body_odom_global", "nav_msgs/msg/Odometry"),
}
READ_SERVICES = {
    "list_maps": ("/omni/maps/list", "omni_robot_interfaces/srv/ListMaps"),
    "list_routes": ("/omni/routes/list", "omni_robot_interfaces/srv/ListRoutes"),
    "get_inspection_result": ("/omni/mission/results", "omni_robot_interfaces/srv/GetCheckpointResults"),
}
OPERATIONS = frozenset(READ_SERVICES) | {"get_robot_status", "get_task"}


class ProbeError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or {}


def sanitize(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Mapping):
        return {str(key): sanitize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    # rosidl sometimes represents numeric arrays with array.array / NumPy.
    if hasattr(value, "tolist"):
        return sanitize(value.tolist())
    return value


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def runtime_environment(proc_root=Path("/proc")):
    candidates = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes().split(b"\0")
            if not command or Path(os.fsdecode(command[0])).name != "mission_manager_node":
                continue
            fields = (entry / "environ").read_bytes().split(b"\0")
            env = {}
            for field in fields:
                name, sep, value = field.partition(b"=")
                key = os.fsdecode(name)
                if sep and key in ENV_KEYS:
                    env[key] = os.fsdecode(value)
            candidates.append(env)
        except (OSError, ValueError):
            continue  # Process exited, or its environment is not readable.
    if not candidates:
        raise ProbeError("runtime_unavailable", "No readable Mission Manager runtime was found")
    if len(candidates) != 1:
        raise ProbeError("ambiguous_runtime", "Multiple Mission Manager runtimes were found")
    return candidates[0]


def validate_request(request):
    if not isinstance(request, dict) or request.get("operation") not in OPERATIONS:
        raise ProbeError("unsupported_operation", "Only the documented read-only queries are allowed")
    timeout = request.get("timeout")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 1 <= timeout <= 120:
        raise ProbeError("invalid_argument", "Invalid query timeout")
    if not isinstance(request.get("robot_id"), str) or not request["robot_id"] or len(request["robot_id"]) > 256:
        raise ProbeError("invalid_argument", "Invalid robot_id")
    if request["operation"] in {"get_task", "get_inspection_result"}:
        task_id = request.get("task_id")
        if not isinstance(task_id, str) or not task_id or len(task_id) > 256 or any(ord(c) < 32 for c in task_id):
            raise ProbeError("invalid_argument", "Invalid task_id")


def _emit(marker, envelope):
    encoded = json.dumps(sanitize(envelope), ensure_ascii=False, allow_nan=False)
    if len(encoded.encode("utf-8")) > 3 * 1024 * 1024:
        encoded = json.dumps({"ok": False, "error": {"code": "response_too_large",
                              "message": "Robot query exceeded the response limit", "details": {}}})
    print(marker + encoded, flush=True)


def _error(error):
    if isinstance(error, ProbeError):
        return {"code": error.code, "message": error.message, "details": error.details}
    # Never leak process environments, configuration contents or raw ROS logs.
    return {"code": "robot_query_failed", "message": "Robot read-only query failed",
            "details": {"exception_type": type(error).__name__}}


def bootstrap(payload):
    """Re-exec once so the runtime's dynamic-library path is set before imports."""
    try:
        request = payload["request"]
        validate_request(request)
        environment = dict(os.environ)
        # Do not retain an unrelated shell's ROS overlay or domain settings.
        for key in ENV_KEYS:
            environment.pop(key, None)
        environment.update(runtime_environment())
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["RCUTILS_LOGGING_USE_STDOUT"] = "0"
        source = payload["source"]
        script = source + "\nworker(" + repr(request) + ", " + repr(payload["marker"]) + ")\n"
        os.execve(sys.executable, [sys.executable, "-B", "-u", "-c", script], environment)
    except Exception as error:
        _emit(payload.get("marker", "OMNI_TOOLS_RESULT:"), {"ok": False, "error": _error(error)})


def worker(request, marker):
    reader = None
    try:
        validate_request(request)
        def timed_out(_signum, _frame):
            raise ProbeError("timeout", "Robot query exceeded its time limit")
        signal.signal(signal.SIGALRM, timed_out)
        signal.setitimer(signal.ITIMER_REAL, request["timeout"] + 1)
        reader = RosReader(request["timeout"])
        data = perform(request, reader)
        _emit(marker, {"ok": True, "data": data})
    except Exception as error:
        _emit(marker, {"ok": False, "error": _error(error)})
    finally:
        try:
            if reader is not None:
                reader.close()
        except Exception:
            pass  # The bounded query result is already emitted; no raw logs.
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)


class RosReader:
    def __init__(self, timeout):
        self.deadline = time.monotonic() + timeout
        try:
            import rclpy
            from rclpy.node import Node
            from rosidl_runtime_py.convert import message_to_ordereddict
            from rosidl_runtime_py.utilities import get_message, get_service
        except ImportError:
            raise ProbeError("ros_unavailable", "The running robot's ROS Python modules are unavailable") from None
        self.rclpy = rclpy
        self.convert = message_to_ordereddict
        self.get_message, self.get_service = get_message, get_service
        rclpy.init(args=["--ros-args", "--disable-external-lib-logs", "--disable-rosout-logs",
                        "--disable-stdout-logs"])
        self.node = Node("omni_tools_readonly_probe_" + str(os.getpid()), enable_rosout=False,
                         cli_args=["--ros-args", "--disable-external-lib-logs", "--disable-rosout-logs",
                                   "--disable-stdout-logs"],
                         start_parameter_services=False, use_global_arguments=False)

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ProbeError("timeout", "Robot query exceeded its time limit")
        return remaining

    def close(self):
        self.node.destroy_node()
        self.rclpy.shutdown()

    def topics(self, keys):
        from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
        # VOLATILE requires a new publication instead of accepting a retained
        # sample as proof of a live process. BEST_EFFORT accepts either publisher.
        qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT,
                         durability=DurabilityPolicy.VOLATILE)
        ready_qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                               durability=DurabilityPolicy.TRANSIENT_LOCAL)
        samples, observations, subscriptions = {}, {}, []
        for key in keys:
            topic, type_name = (TOPICS | LOCALIZATION_TOPICS)[key]
            observations[key] = {"topic": topic, "available": False}
            try:
                message_type = self.get_message(type_name)
                def callback(message, key=key):
                    samples[key] = sanitize(self.convert(message))
                    observations[key].update(available=True, received_at=utc_now(),
                                             _received_monotonic=time.monotonic())
                # TF ready publishes only on change. Its retained Bool must be
                # interpreted together with the freshly stamped odometry.
                retained = key == "tf_ready_status"
                observations[key]["may_be_retained"] = retained
                subscriptions.append(self.node.create_subscription(
                    message_type, topic, callback, ready_qos if retained else qos))
            except (ImportError, ModuleNotFoundError, AttributeError, ValueError):
                observations[key]["reason"] = "message_type_unavailable"
        sample_deadline = min(self.deadline, time.monotonic() + 4.0)
        while time.monotonic() < sample_deadline:
            if len(samples) == len(subscriptions):
                break
            self.rclpy.spin_once(self.node, timeout_sec=min(0.1, sample_deadline - time.monotonic()))
        for key, observation in observations.items():
            received = observation.pop("_received_monotonic", None)
            if received is None:
                observation.setdefault("reason", "no_live_sample_within_timeout")
                continue
            observation["received_age_seconds"] = round(time.monotonic() - received, 3)
            observation["received_after_subscription"] = True
            sample = samples[key]
            stamp = sample.get("stamp", sample.get("header", {}).get("stamp", {}))
            source_time = stamp.get("sec", 0) + stamp.get("nanosec", 0) / 1e9
            observation["source_stamp_age_seconds"] = round(time.time() - source_time, 3) if source_time else None
            observation["source_stamp_clock"] = "ROS stamp compared with robot wall clock; simulation clocks may differ"
        for subscription in subscriptions:
            self.node.destroy_subscription(subscription)
        return samples, observations

    def service(self, operation, fields=None):
        # This is deliberately not a general ROS service bridge.
        if operation not in READ_SERVICES:
            raise ProbeError("unsupported_operation", "ROS service is not in the read-only allowlist")
        endpoint, type_name = READ_SERVICES[operation]
        try:
            service_type = self.get_service(type_name)
        except (ImportError, ModuleNotFoundError, AttributeError):
            raise ProbeError("interface_unavailable", "Robot query interface is unavailable", {"service": endpoint}) from None
        client = self.node.create_client(service_type, endpoint)
        try:
            if not client.wait_for_service(timeout_sec=self.remaining()):
                raise ProbeError("service_unavailable", "Robot query service is unavailable", {"service": endpoint})
            request = service_type.Request()
            if operation == "get_inspection_result":
                request.mission_id = fields["mission_id"]
            future = client.call_async(request)
            while not future.done():
                self.rclpy.spin_once(self.node, timeout_sec=min(0.1, self.remaining()))
            if future.exception() is not None or future.result() is None:
                raise ProbeError("service_failed", "Robot query service returned no result", {"service": endpoint})
            return sanitize(self.convert(future.result()))
        finally:
            self.node.destroy_client(client)


def catalog_rows(response, kind):
    """Validate parallel arrays instead of silently truncating with zip()."""
    fields = {
        "maps": {"map_ids": "map_id", "map_versions": "map_version", "map_checksums": "map_checksum",
                 "created_at": "created_at", "size_bytes": "size_bytes"},
        "routes": {"route_ids": "route_id", "map_ids": "map_id", "frame_ids": "frame_id",
                   "created_at": "created_at", "map_versions": "map_version", "map_checksums": "map_checksum",
                   "route_checksums": "route_checksum", "point_counts": "point_count", "distances_m": "distance_m"},
    }[kind]
    primary = "map_ids" if kind == "maps" else "route_ids"
    if not isinstance(response.get(primary), list):
        raise ProbeError("invalid_catalog", "Robot asset catalog is missing its ID array")
    length = len(response[primary])
    missing = []
    for field in fields:
        if field not in response:
            # Older deployed Route interfaces predate the additive metadata.
            if kind == "routes" and field in {"map_versions", "map_checksums", "route_checksums", "point_counts", "distances_m"}:
                missing.append(field)
                continue
            raise ProbeError("invalid_catalog", "Robot asset catalog is missing required fields", {"field": field})
        if not isinstance(response[field], list) or len(response[field]) != length:
            raise ProbeError("invalid_catalog", "Robot asset catalog has inconsistent parallel arrays", {"field": field})
    rows = [{singular: response[field][i] if field in response else None for field, singular in fields.items()}
            for i in range(length)]
    warnings = ["The deployed route interface does not provide: " + ", ".join(missing)] if missing else []
    if not rows:
        warnings.append("An empty catalog may also indicate an upstream catalog failure; it does not prove that no assets exist.")
    return rows, warnings


def perform(request, reader):
    operation, robot_id = request["operation"], request["robot_id"]
    base = {"robot_id": robot_id, "observed_at": utc_now(), "read_only": True}
    if operation in {"list_maps", "list_routes"}:
        kind = "maps" if operation == "list_maps" else "routes"
        rows, warnings = catalog_rows(reader.service(operation), kind)
        return {**base, kind: rows, "count": len(rows), "warnings": warnings}
    if operation == "get_inspection_result":
        response = reader.service(operation, {"mission_id": request["task_id"]})
        results = response.get("results")
        if not isinstance(results, list) or any(not isinstance(item, dict) or item.get("mission_id") != request["task_id"] for item in results):
            raise ProbeError("invalid_robot_response", "Inspection results do not match the requested mission")
        return {**base, "task_id": request["task_id"], "results": results,
                "count": len(results), "mission_existence": "confirmed_by_evidence" if results else "unknown",
                "warnings": [] if results else ["No evidence records were returned. This does not establish whether the mission exists or has completed."]}
    if operation == "get_robot_status":
        samples, observations = reader.topics(tuple(TOPICS) + tuple(LOCALIZATION_TOPICS))
        if not any(key in samples for key in TOPICS):
            raise ProbeError("status_unavailable", "No live robot status topics were observed",
                             {"runtime_process_found": True, "observations": observations})
        return {**base, **{key: samples.get(key) for key in TOPICS | LOCALIZATION_TOPICS}, "observations": observations,
                "runtime_process_found": True,
                "complete": all(observations.get(key, {}).get("available", False) for key in TOPICS)}
    if operation == "get_task":
        samples, observations = reader.topics(("mission_status", "navigation_status", "runtime_status"))
        task_id = request["task_id"]
        for key, identity, kind in (("mission_status", "mission_id", "mission"),
                                    ("navigation_status", "operation_id", "navigation"),
                                    ("runtime_status", "operation_id", "runtime")):
            sample = samples.get(key, {})
            if sample.get(identity) == task_id:
                return {**base, "task_id": task_id, "found": True, "kind": kind,
                        "scope": "live_snapshot", "status": sample, "observation": observations[key]}
        return {**base, "task_id": task_id, "found": False, "scope": "live_snapshot",
                "observation": "not_observed", "observations": observations,
                "warnings": ["The requested task was not in a live snapshot. It may be historical, unknown, or temporarily unobservable; no completion state is inferred."]}
    raise ProbeError("unsupported_operation", "Only read-only operations are allowed")

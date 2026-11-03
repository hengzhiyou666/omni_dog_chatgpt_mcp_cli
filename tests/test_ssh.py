import json
import subprocess
from types import SimpleNamespace

import pytest

from omni_tools.backends.ssh import SSHBackend
from omni_tools.config import Settings
from omni_tools.errors import OmniError
from omni_tools.remote_probe import (ProbeError, READ_SERVICES, catalog_rows, perform,
                                     runtime_environment, sanitize, validate_request)


def reply(payload, data=None, error=None, banner=""):
    envelope = {"ok": False, "error": error} if error else {"ok": True, "data": data or {"read_only": True}}
    return SimpleNamespace(returncode=0, stdout=banner + payload["marker"] + json.dumps(envelope) + "\n", stderr="")


def test_user_data_stays_on_stdin_and_host_keys_are_not_changed(monkeypatch):
    task_id = "epoch/navigation/1;$(touch nowhere)"
    seen = []
    def run(command, **kwargs):
        seen.append((command, kwargs))
        payload = json.loads(kwargs["input"])
        assert payload["request"]["task_id"] == task_id
        assert task_id not in " ".join(command)
        assert kwargs.get("shell", False) is False
        assert "StrictHostKeyChecking=yes" in command
        assert "UpdateHostKeys=no" in command
        return reply(payload, {"task_id": task_id, "found": False}, banner="ROS banner\n")
    monkeypatch.setattr(subprocess, "run", run)
    result = SSHBackend(Settings()).get_task(task_id, "dog4")
    assert result["task_id"] == task_id
    assert seen[0][1]["timeout"] == 25


def test_other_robot_cannot_reuse_configured_ssh_connection(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("SSH must not run"))
    with pytest.raises(OmniError) as caught:
        SSHBackend(Settings()).get_robot_status("another_robot")
    assert caught.value.code == "robot_not_configured"


def test_transport_timeout_is_structured(monkeypatch):
    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired("ssh", 25)
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(OmniError) as caught:
        SSHBackend(Settings()).list_maps("dog4")
    assert caught.value.code == "timeout"


def test_raw_ssh_logs_are_not_exposed(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=255, stdout="", stderr="private password=secret"))
    with pytest.raises(OmniError) as caught:
        SSHBackend(Settings()).list_routes("dog4")
    assert caught.value.code == "ssh_failed"
    assert "secret" not in str(caught.value.as_dict())


def test_remote_error_is_not_mistaken_for_success(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: reply(json.loads(kw["input"]), error={
        "code": "service_unavailable", "message": "Query service is absent", "details": {"service": "/omni/maps/list"}}))
    with pytest.raises(OmniError) as caught:
        SSHBackend(Settings()).list_maps("dog4")
    assert caught.value.code == "service_unavailable"


@pytest.mark.parametrize("malformation", ["duplicates", "nan", "wrong_shape"])
def test_invalid_marker_response_is_rejected(monkeypatch, malformation):
    def run(*args, **kwargs):
        payload = json.loads(kwargs["input"])
        result = reply(payload)
        if malformation == "duplicates":
            result.stdout += result.stdout
        elif malformation == "nan":
            result.stdout = payload["marker"] + '{"ok":true,"data":{"battery":NaN}}'
        else:
            result.stdout = payload["marker"] + '[]'
        return result
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(OmniError) as caught:
        SSHBackend(Settings()).get_robot_status("dog4")
    assert caught.value.code == "invalid_robot_response"


def test_nonfinite_telemetry_becomes_json_null():
    result = sanitize({"battery": float("nan"), "nested": [float("inf"), -float("inf"), 12.4]})
    assert json.dumps(result, allow_nan=False) == '{"battery": null, "nested": [null, null, 12.4]}'


def test_environment_only_reads_the_actual_mission_process_and_allowlist(tmp_path):
    process = tmp_path / "123"
    process.mkdir()
    (process / "cmdline").write_bytes(b"/userdata/1_heng/install/omni_mission_manager/lib/omni_mission_manager/mission_manager_node\0--ros-args\0")
    (process / "environ").write_bytes(
        b"ROS_DOMAIN_ID=0\0PYTHONPATH=/opt/ros\0PASSWORD=private\0TOKEN=private\0"
        b"RMW_IMPLEMENTATION_TYPE=zenoh\0ZENOH_ROUTER_CHECK_ATTEMPTS=3\0"
        b"ZENOH_SESSION_CONFIG_URI=/app/zenoh/session.json5\0ZENOH_PRIVATE_TOKEN=private\0")
    decoy = tmp_path / "124"
    decoy.mkdir()
    (decoy / "cmdline").write_bytes(b"python3\0mission_manager_node\0")
    assert runtime_environment(tmp_path) == {
        "ROS_DOMAIN_ID": "0", "PYTHONPATH": "/opt/ros", "RMW_IMPLEMENTATION_TYPE": "zenoh",
        "ZENOH_ROUTER_CHECK_ATTEMPTS": "3", "ZENOH_SESSION_CONFIG_URI": "/app/zenoh/session.json5"}


def test_environment_refuses_ambiguous_ros_runtimes(tmp_path):
    for pid in (1, 2):
        process = tmp_path / str(pid)
        process.mkdir()
        (process / "cmdline").write_bytes(b"mission_manager_node\0")
        (process / "environ").write_bytes(b"ROS_DOMAIN_ID=0\0")
    with pytest.raises(ProbeError) as caught:
        runtime_environment(tmp_path)
    assert caught.value.code == "ambiguous_runtime"


def test_mismatched_parallel_catalog_is_not_silently_truncated():
    with pytest.raises(ProbeError) as caught:
        catalog_rows({"map_ids": ["a"], "map_versions": [], "map_checksums": ["hash"],
                      "created_at": ["today"], "size_bytes": [123]}, "maps")
    assert caught.value.code == "invalid_catalog"


def test_older_route_interface_reports_missing_metadata():
    rows, warnings = catalog_rows({"route_ids": ["r"], "map_ids": ["m"],
                                   "frame_ids": ["map"], "created_at": ["today"]}, "routes")
    assert rows[0]["route_checksum"] is None
    assert warnings


def test_missing_task_is_not_reported_as_complete():
    reader = SimpleNamespace(topics=lambda keys: ({"mission_status": {"mission_id": "another", "state": 4}}, {}))
    result = perform({"operation": "get_task", "robot_id": "dog4", "task_id": "old_task"}, reader)
    assert result["found"] is False
    assert result["observation"] == "not_observed"
    assert "status" not in result


def test_zero_live_status_samples_returns_unavailable():
    reader = SimpleNamespace(topics=lambda keys: ({}, {key: {"available": False} for key in keys}))
    with pytest.raises(ProbeError) as caught:
        perform({"operation": "get_robot_status", "robot_id": "dog4"}, reader)
    assert caught.value.code == "status_unavailable"
    assert caught.value.details["runtime_process_found"] is True


def test_partial_live_status_remains_explicit():
    reader = SimpleNamespace(topics=lambda keys: (
        {"robot_state": {"operational_mode": 0}},
        {key: {"available": key == "robot_state"} for key in keys}))
    result = perform({"operation": "get_robot_status", "robot_id": "dog4"}, reader)
    assert result["robot_state"] == {"operational_mode": 0}
    assert result["runtime_status"] is None
    assert result["complete"] is False


def test_navigation_operation_identity_is_preserved():
    identity = "epoch/navigation/3"
    reader = SimpleNamespace(topics=lambda keys: (
        {"navigation_status": {"operation_id": identity, "manager_epoch": "epoch", "state": 3}},
        {"navigation_status": {"available": True}}))
    result = perform({"operation": "get_task", "robot_id": "dog4", "task_id": identity}, reader)
    assert result["kind"] == "navigation"
    assert result["status"]["manager_epoch"] == "epoch"


def test_empty_inspection_evidence_does_not_prove_task_exists():
    reader = SimpleNamespace(service=lambda *args: {"results": []})
    result = perform({"operation": "get_inspection_result", "robot_id": "dog4", "task_id": "m"}, reader)
    assert result["mission_existence"] == "unknown"
    assert result["warnings"]


def test_cross_mission_evidence_is_rejected():
    reader = SimpleNamespace(service=lambda *args: {"results": [{"mission_id": "wrong"}]})
    with pytest.raises(ProbeError):
        perform({"operation": "get_inspection_result", "robot_id": "dog4", "task_id": "requested"}, reader)


def test_mutations_are_not_remote_probe_operations():
    assert set(READ_SERVICES) == {"list_maps", "list_routes", "get_inspection_result"}
    for operation in ("navigate", "dispatch_mission", "cancel", "/omni/control/authority"):
        with pytest.raises(ProbeError) as caught:
            validate_request({"operation": operation, "timeout": 12, "robot_id": "dog4"})
        assert caught.value.code == "unsupported_operation"

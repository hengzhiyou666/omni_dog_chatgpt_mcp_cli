"""Rejected runtime writes and previews never reach a robot."""
from unittest.mock import Mock

import pytest

from omni_tools.config import Settings
from omni_tools.errors import OmniError
from omni_tools.service import OmniService


MAP = {"map_id": "map-demo", "map_version": 1, "map_checksum": "a" * 64}
CALLS = [
    ("start_localization", MAP),
    ("start_route_recording", {"route_id": "route-demo", **MAP}),
    ("finish_route_recording", {"recording_operation_id": "epoch/runtime/1", "disposition": "discard"}),
    ("set_runtime_idle", {}),
]


def service(**overrides):
    settings = dict(allow_writes=True, allowed_robots=("dog4",))
    settings.update(overrides)
    backend = Mock()
    return OmniService(Settings(**settings), backend), backend


@pytest.mark.parametrize("method,parameters", CALLS)
def test_runtime_preview_is_local_even_when_writes_disabled(method, parameters):
    api, backend = service(allow_writes=False, allowed_robots=())
    result = getattr(api, method)(**parameters)
    assert result["data"]["preview"] is True
    assert result["data"]["execution_backend"] == "ssh"
    assert backend.mock_calls == []


@pytest.mark.parametrize("method,parameters", CALLS)
@pytest.mark.parametrize("overrides,code", [
    ({"allow_writes": False}, "writes_disabled"),
    ({"allowed_robots": ()}, "robot_not_allowed"),
    ({"backend": "http", "base_url": "http://127.0.0.1:1", "token": "test"}, "unsupported_backend"),
])
def test_runtime_execution_requires_ssh_write_permission(method, parameters, overrides, code):
    api, backend = service(**overrides)
    with pytest.raises(OmniError) as error:
        getattr(api, method)(**parameters, execute=True)
    assert error.value.code == code
    assert backend.mock_calls == []


@pytest.mark.parametrize("method,parameters", CALLS)
def test_runtime_execute_must_be_boolean(method, parameters):
    api, backend = service()
    with pytest.raises(OmniError, match="execute must be a boolean"):
        getattr(api, method)(**parameters, execute=1)
    assert backend.mock_calls == []


@pytest.mark.parametrize("field,value", [
    ("map_id", "../map"), ("map_version", True), ("map_version", 0), ("map_version", 4294967296),
    ("map_checksum", "a" * 63), ("map_checksum", "g" * 64),
    ("initial_x", float("nan")), ("initial_y", float("inf")),
    ("initial_z", True), ("initial_yaw", 10**1000),
    ("initial_x", 3.5e38), ("initial_y", 1e300),
])
def test_invalid_map_identity_and_pose_never_connect(field, value):
    api, backend = service()
    with pytest.raises(OmniError) as error:
        api.start_localization(**{**MAP, field: value}, execute=True)
    assert error.value.code == "invalid_argument"
    assert backend.mock_calls == []


@pytest.mark.parametrize("parameters", [
    {"recording_operation_id": "", "disposition": "discard"},
    {"recording_operation_id": "epoch/runtime/1\n", "disposition": "discard"},
    {"recording_operation_id": "epoch/runtime/1", "disposition": "delete"},
])
def test_finish_requires_explicit_valid_recording_identity_and_disposition(parameters):
    api, backend = service()
    with pytest.raises(OmniError):
        api.finish_route_recording(**parameters, execute=True)
    assert backend.mock_calls == []


def test_recording_route_id_is_validated_before_backend():
    api, backend = service()
    with pytest.raises(OmniError):
        api.start_route_recording("../route", **MAP, execute=True)
    assert backend.mock_calls == []

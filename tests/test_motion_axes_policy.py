"""Body-relative movement and rotation remain offline until explicitly enabled."""
from unittest.mock import Mock

import pytest

from omni_tools.config import Settings
from omni_tools.errors import OmniError
from omni_tools.service import OmniService


def service(**overrides):
    config = dict(allow_writes=True, allowed_robots=("dog4",))
    config.update(overrides)
    backend = Mock()
    receipt = {"completion": "estimated_time", "actual_distance_m": None,
               "actual_angle_deg": None, "stop_confirmed": True,
               "stop_feedback_scope": "downstream_command", "control_released": True}
    backend.move.return_value = backend.rotate.return_value = receipt
    return OmniService(Settings(**config), backend), backend


@pytest.mark.parametrize("backend_name", ["ssh", "local", "http"])
def test_motion_previews_never_reach_backend(backend_name):
    api, backend = service(backend=backend_name, allow_writes=False, allowed_robots=(),
                           base_url="http://127.0.0.1:1", token="test")
    for op, parameters in [("move", {"direction": "backward", "distance_m": 0.2}),
                           ("rotate", {"direction": "counterclockwise", "angle_deg": 15.0})]:
        data = getattr(api, op)(**parameters)["data"]
        assert data["operation"] == op
        assert data["executed"] is False and data["preview"] is True
        assert data["parameters"] == {"robot_id": "dog4", **parameters, "measurement_mode": "timed"}
        assert data["current_backend_supports_execution"] == (backend_name != "http")
    assert backend.mock_calls == []


@pytest.mark.parametrize("operation,field,bad", [
    (op, field, bad)
    for op, field, bad_values in [
        ("move", "distance_m", [True, None, "1", float("nan"), float("inf"), 0.099, 2.001, 10**1000]),
        ("rotate", "angle_deg", [False, None, "90", float("nan"), float("inf"), 0.999, 360.001, 10**1000]),
    ] for bad in bad_values
])
def test_invalid_magnitude_never_reaches_backend(operation, field, bad):
    api, backend = service()
    arguments = {field: bad, "execute": True}
    if operation == "move":
        arguments["direction"] = "forward"
    with pytest.raises(OmniError, match=field):
        getattr(api, operation)(**arguments)
    assert backend.mock_calls == []


@pytest.mark.parametrize("operation", ["move", "rotate"])
@pytest.mark.parametrize("direction", [None, True, 1, [], {}, "", "FORWARD", "auto"])
def test_invalid_direction_is_rejected(operation, direction):
    api, backend = service()
    with pytest.raises(OmniError, match="direction"):
        getattr(api, operation)(direction=direction, execute=True)
    assert backend.mock_calls == []


@pytest.mark.parametrize("operation", ["move", "rotate"])
@pytest.mark.parametrize("execute", [0, 1, None, "true"])
def test_execute_requires_boolean(operation, execute):
    api, backend = service()
    with pytest.raises(OmniError, match="execute"):
        getattr(api, operation)(direction="forward" if operation == "move" else "clockwise", execute=execute)
    assert backend.mock_calls == []


@pytest.mark.parametrize("operation", ["move", "rotate"])
@pytest.mark.parametrize("overrides,code", [
    ({"allow_writes": False}, "writes_disabled"),
    ({"allowed_robots": ()}, "robot_not_allowed"),
    ({"backend": "http", "base_url": "http://127.0.0.1:1", "token": "test"}, "unsupported_backend"),
])
def test_execution_retains_existing_write_gate(operation, overrides, code):
    api, backend = service(**overrides)
    with pytest.raises(OmniError) as caught:
        getattr(api, operation)(direction="forward" if operation == "move" else "clockwise", execute=True)
    assert caught.value.code == code
    assert backend.mock_calls == []


@pytest.mark.parametrize("direction", ["forward", "backward", "left", "right"])
def test_move_execution_preserves_receipt(direction):
    api, backend = service()
    data = api.move(direction, 0.2, execute=True)["data"]
    backend.move.assert_called_once_with(direction=direction, distance_m=0.2, robot_id="dog4")
    assert data["operation"] == "move" and data["executed"] and not data["preview"]
    assert data["measurement_mode"] == "timed"
    assert data["actual_distance_m"] is None
    assert data["stop_confirmed"] and data["control_released"]


def test_default_rotation_is_clockwise_and_preserves_estimate():
    api, backend = service()
    data = api.rotate(execute=True)["data"]
    backend.rotate.assert_called_once_with(angle_deg=90.0, direction="clockwise", robot_id="dog4")
    assert data["operation"] == "rotate" and data["executed"] and not data["preview"]
    assert data["completion"] == "estimated_time" and data["actual_angle_deg"] is None


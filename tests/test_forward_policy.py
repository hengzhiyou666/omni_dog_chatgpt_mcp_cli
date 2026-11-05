"""A preview never connects, and rejected movement never reaches the executor."""
from unittest.mock import Mock

import pytest

from omni_tools.config import Settings
from omni_tools.errors import OmniError
from omni_tools.service import OmniService


def service(**overrides):
    config = dict(backend="ssh", allow_writes=True, allowed_robots=("dog4",))
    config.update(overrides)
    backend = Mock()
    backend.move_forward.return_value = {"measured_distance_m": 0.99,
                                         "stop_confirmed": True, "control_released": True}
    return OmniService(Settings(**config), backend), backend


def test_preview_never_connects_and_reports_bounded_parameters():
    api, backend = service(allow_writes=False, allowed_robots=())
    result = api.move_forward(1.0)
    assert result["data"]["preview"] is True
    assert result["data"]["parameters"] == {"robot_id": "dog4", "distance_m": 1.0}
    assert backend.mock_calls == []


@pytest.mark.parametrize("distance", [True, False, None, "1", float("nan"), float("inf"), -1, 0, 0.099, 2.001, 10**1000])
def test_invalid_distance_never_connects(distance):
    api, backend = service()
    with pytest.raises(OmniError, match="distance_m"):
        api.move_forward(distance, execute=True)
    assert backend.mock_calls == []


@pytest.mark.parametrize("overrides,code", [
    ({"allow_writes": False}, "writes_disabled"),
    ({"allowed_robots": ()}, "robot_not_allowed"),
    ({"allowed_robots": ("DOG4",)}, "robot_not_allowed"),
    ({"backend": "http", "base_url": "http://127.0.0.1:1", "token": "test"}, "unsupported_backend"),
])
def test_rejected_execution_does_not_reach_backend(overrides, code):
    api, backend = service(**overrides)
    with pytest.raises(OmniError) as error:
        api.move_forward(execute=True)
    assert error.value.code == code
    assert backend.mock_calls == []


@pytest.mark.parametrize("execute", [0, 1, None, "true"])
def test_execute_is_strict(execute):
    api, backend = service()
    with pytest.raises(OmniError):
        api.move_forward(execute=execute)
    assert backend.mock_calls == []


def test_execution_preserves_measured_evidence():
    api, backend = service()
    result = api.move_forward(1, execute=True)
    backend.move_forward.assert_called_once_with(1.0, "dog4")
    assert result["data"]["measured_distance_m"] == 0.99
    assert result["data"]["stop_confirmed"] is True


def test_uncertain_result_is_not_retried_or_replaced_with_success():
    api, backend = service()
    backend.move_forward.side_effect = OmniError("motion_outcome_unknown", "Do not retry")
    with pytest.raises(OmniError):
        api.move_forward(execute=True)
    assert backend.move_forward.call_count == 1

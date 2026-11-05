"""Posture previews and gates cannot accidentally enter either execution backend."""
from unittest.mock import Mock

import pytest

from omni_tools.config import Settings
from omni_tools.errors import OmniError
from omni_tools.service import OmniService


def service(**overrides):
    config = dict(backend="ssh", allow_writes=True, allowed_robots=("dog4",))
    config.update(overrides)
    backend = Mock()
    backend.set_posture.return_value = {"bridge_acknowledged": True}
    return OmniService(Settings(**config), backend), backend


@pytest.mark.parametrize("method", ["stand_up", "lie_down"])
def test_preview_is_local_even_with_writes_disabled(method):
    api, backend = service(allow_writes=False, allowed_robots=())
    result = getattr(api, method)()
    assert result["data"]["preview"] is True
    assert result["data"]["executed"] is False
    assert result["data"]["execution_backend"] == "ssh"
    assert backend.mock_calls == []


@pytest.mark.parametrize("method,command", [("stand_up", "stand"), ("lie_down", "lie_down")])
def test_both_actions_use_the_same_guarded_backend(method, command):
    api, backend = service()
    result = getattr(api, method)(execute=True)
    backend.set_posture.assert_called_once_with(command, "dog4")
    assert result["data"]["executed"] is True
    assert result["data"]["physical_posture_confirmed"] is False


@pytest.mark.parametrize("method", ["stand_up", "lie_down"])
@pytest.mark.parametrize("overrides,code", [
    ({"allow_writes": False}, "writes_disabled"),
    ({"allowed_robots": ()}, "robot_not_allowed"),
    ({"allowed_robots": ("DOG4", "dog4-other")}, "robot_not_allowed"),
    ({"backend": "http", "base_url": "http://127.0.0.1:1", "token": "test"}, "unsupported_backend"),
])
def test_denied_execution_never_enters_backend(method, overrides, code):
    api, backend = service(**overrides)
    with pytest.raises(OmniError) as error:
        getattr(api, method)(execute=True)
    assert error.value.code == code
    assert backend.mock_calls == []


@pytest.mark.parametrize("method", ["stand_up", "lie_down"])
@pytest.mark.parametrize("execute", [1, 0, None, "true", [], {}])
def test_execute_requires_actual_boolean(method, execute):
    api, backend = service()
    with pytest.raises(OmniError) as error:
        getattr(api, method)(execute=execute)
    assert error.value.code == "invalid_argument"
    assert backend.mock_calls == []


@pytest.mark.parametrize("method", ["stand_up", "lie_down"])
def test_unknown_result_does_not_become_success(method):
    api, backend = service()
    backend.set_posture.side_effect = OmniError("posture_outcome_unknown", "Do not retry blindly")
    with pytest.raises(OmniError) as error:
        getattr(api, method)(execute=True)
    assert error.value.code == "posture_outcome_unknown"
    assert backend.set_posture.call_count == 1

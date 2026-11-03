"""V2 write gates and previews tested only with an in-memory backend spy."""

import json
import math
from unittest.mock import Mock

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from omni_tools.cli import run
from omni_tools.config import Settings
from omni_tools.errors import OmniError
from omni_tools.mcp_server import create_server
from omni_tools.service import OmniService


READ_TOOLS = {"get_robot_status", "list_maps", "list_routes", "get_task", "get_inspection_result"}
WRITE_TOOLS = {"start_inspection", "navigate_to", "cancel_task"}
OPERATIONS = [
    ("start_inspection", {"route_id": "route-1"}),
    ("navigate_to", {"map_id": "map-1", "map_version": 1, "x": 1.25, "y": -2.5, "yaw": 0.5}),
    ("cancel_task", {"task_id": "task-1"}),
]


def settings(**overrides):
    values = {
        "backend": "http", "base_url": "http://127.0.0.1:1", "token": "test-token",
        "robot_id": "demo", "allow_writes": True, "allowed_robots": ("demo",),
    }
    values.update(overrides)
    return Settings(**values)


def make_service(**overrides):
    backend = Mock()
    for name in WRITE_TOOLS:
        getattr(backend, name).return_value = {"accepted": True, "task_id": "task-1", "status": "pending"}
    return OmniService(settings(**overrides), backend=backend), backend


@pytest.mark.parametrize(("operation", "arguments"), OPERATIONS)
@pytest.mark.parametrize(
    ("overrides", "error_code"),
    [
        ({"backend": "ssh"}, "unsupported_backend"),
        ({"allow_writes": False}, "writes_disabled"),
        ({"allowed_robots": ()}, "robot_not_allowed"),
        ({"allowed_robots": ("another-robot",)}, "robot_not_allowed"),
        # Membership must be exact, never a substring/prefix or case-folded match.
        ({"allowed_robots": ("demo-other", "DEMO")}, "robot_not_allowed"),
    ],
)
def test_denied_write_never_reaches_backend(operation, arguments, overrides, error_code):
    service, backend = make_service(**overrides)
    with pytest.raises(OmniError) as error:
        getattr(service, operation)(**arguments, execute=True)
    assert error.value.code == error_code
    assert backend.mock_calls == []


@pytest.mark.parametrize(("operation", "arguments"), OPERATIONS)
def test_preview_never_reads_or_writes_backend_even_with_all_gates_off(operation, arguments):
    service, backend = make_service(backend="ssh", allow_writes=False, allowed_robots=())
    result = getattr(service, operation)(**arguments)
    assert result["ok"] is True
    assert result["data"]["executed"] is False
    assert backend.mock_calls == []


@pytest.mark.parametrize(("operation", "arguments"), OPERATIONS)
def test_allowed_execution_calls_backend_exactly_once(operation, arguments):
    service, backend = make_service()
    result = getattr(service, operation)(**arguments, execute=True)
    assert result["ok"] is True
    assert len(backend.mock_calls) == 1
    assert backend.mock_calls[0][0] == operation
    assert "demo" in backend.mock_calls[0].args or "demo" in backend.mock_calls[0].kwargs.values()


@pytest.mark.parametrize(("operation", "arguments"), OPERATIONS)
def test_explicit_robot_cannot_bypass_allowlist(operation, arguments):
    service, backend = make_service()
    with pytest.raises(OmniError) as error:
        getattr(service, operation)(**arguments, robot_id="other", execute=True)
    assert error.value.code == "robot_not_allowed"
    assert backend.mock_calls == []


@pytest.mark.parametrize(("operation", "arguments"), OPERATIONS)
@pytest.mark.parametrize("robot", ["", 0, False, [], "bad\nrobot"])
def test_explicit_invalid_robot_never_falls_back_to_default(operation, arguments, robot):
    service, backend = make_service()
    with pytest.raises(OmniError) as error:
        getattr(service, operation)(**arguments, robot_id=robot, execute=True)
    assert error.value.code == "invalid_argument"
    assert backend.mock_calls == []


@pytest.mark.parametrize(("operation", "arguments"), OPERATIONS)
@pytest.mark.parametrize("execute", [1, 0, "true", "false", None, []])
def test_execute_requires_actual_boolean(operation, arguments, execute):
    service, backend = make_service()
    with pytest.raises(OmniError) as error:
        getattr(service, operation)(**arguments, execute=execute)
    assert error.value.code == "invalid_argument"
    assert backend.mock_calls == []


@pytest.mark.parametrize("field", ["x", "y", "yaw"])
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, True, "1.0", None])
def test_navigation_rejects_nonfinite_or_wrong_type_coordinates_before_backend(field, value):
    service, backend = make_service()
    args = {"map_id": "map-1", "map_version": 1, "x": 0, "y": 0, "yaw": 0}
    args[field] = value
    with pytest.raises(OmniError) as error:
        service.navigate_to(**args, execute=True)
    assert error.value.code == "invalid_argument"
    assert backend.mock_calls == []


@pytest.mark.parametrize("version", [True, -1, 0, 4294967296, 1.5, "1", None])
def test_navigation_rejects_invalid_map_version_before_backend(version):
    service, backend = make_service()
    with pytest.raises(OmniError) as error:
        service.navigate_to("map-1", version, 0, 0, execute=True)
    assert error.value.code == "invalid_argument"
    assert backend.mock_calls == []


@pytest.mark.parametrize(("operation", "arguments", "field"), [
    ("start_inspection", {"route_id": "route-1"}, "route_id"),
    ("navigate_to", {"map_id": "map-1", "map_version": 1, "x": 0, "y": 0}, "map_id"),
    ("cancel_task", {"task_id": "task-1"}, "task_id"),
])
@pytest.mark.parametrize("value", ["", "bad\nidentifier", None, 42])
def test_invalid_resource_identifier_never_reaches_backend(operation, arguments, field, value):
    service, backend = make_service()
    args = {**arguments, field: value}
    with pytest.raises(OmniError) as error:
        getattr(service, operation)(**args, execute=True)
    assert error.value.code == "invalid_argument"
    assert backend.mock_calls == []


@pytest.mark.parametrize("kind", ["", "unknown", "INSPECTION", None, 1])
def test_cancellation_kind_is_validated_before_backend(kind):
    service, backend = make_service()
    with pytest.raises(OmniError) as error:
        service.cancel_task("task-1", kind=kind, execute=True)
    assert error.value.code == "invalid_argument"
    assert backend.mock_calls == []


@pytest.fixture
def clean_omni_environment(monkeypatch):
    import os
    for key in os.environ:
        if key.startswith("OMNI_"):
            monkeypatch.delenv(key)
    return monkeypatch


def test_writes_disabled_and_allowlist_empty_by_default(clean_omni_environment):
    config = Settings.from_env()
    assert config.allow_writes is False
    assert config.allowed_robots == ()


@pytest.mark.parametrize("overrides", [
    {"allow_writes": "1"}, {"allow_writes": 1},
    {"allowed_robots": "demo"}, {"allowed_robots": ["demo"]},
    {"allowed_robots": ("*",)}, {"allowed_robots": ("demo*",)},
])
def test_invalid_direct_policy_configuration_is_rejected(overrides):
    with pytest.raises(OmniError) as error:
        settings(**overrides)
    assert error.value.code == "configuration_error"


@pytest.mark.parametrize("value", ["true", "false", "yes", "on", "2", "", " 1 "])
def test_ambiguous_write_environment_is_rejected(clean_omni_environment, value):
    clean_omni_environment.setenv("OMNI_ALLOW_WRITES", value)
    with pytest.raises(OmniError) as error:
        Settings.from_env()
    assert error.value.code == "configuration_error"


@pytest.mark.parametrize(("value", "enabled"), [("0", False), ("1", True)])
def test_explicit_environment_flag_and_robot_list(clean_omni_environment, value, enabled):
    clean_omni_environment.setenv("OMNI_ALLOW_WRITES", value)
    clean_omni_environment.setenv("OMNI_ALLOWED_ROBOTS", "demo,other")
    config = Settings.from_env()
    assert config.allow_writes is enabled
    assert config.allowed_robots == ("demo", "other")


@pytest.mark.parametrize(("operation", "command"), [
    ("start_inspection", ["inspection", "start", "--route", "route-1"]),
    ("navigate_to", ["navigation", "start", "--map", "map-1", "--map-version", "1", "--x", "1", "--y", "2"]),
    ("cancel_task", ["task", "cancel", "task-1", "--kind", "navigation"]),
])
def test_cli_preview_then_explicit_execution(capsys, operation, command):
    service, backend = make_service()
    command = [*command, "--robot", "demo", "--json"]
    assert run(command, service=service) == 0
    assert json.loads(capsys.readouterr().out)["data"]["executed"] is False
    assert backend.mock_calls == []
    assert run([*command, "--execute"], service=service) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    getattr(backend, operation).assert_called_once()
    assert len(backend.mock_calls) == 1


def test_cli_execute_cannot_override_disabled_writes(capsys):
    service, backend = make_service(allow_writes=False)
    assert run(["inspection", "start", "--route", "route-1", "--execute", "--json"], service=service) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "writes_disabled"
    assert backend.mock_calls == []


async def test_mcp_write_tools_are_distinct_from_five_read_only_tools():
    service, backend = make_service()
    server = create_server(service=service)
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert set(tools) == READ_TOOLS | WRITE_TOOLS
    for name in READ_TOOLS:
        assert tools[name].annotations.readOnlyHint is True
        assert tools[name].annotations.destructiveHint is False
    for name in WRITE_TOOLS:
        assert tools[name].annotations.readOnlyHint is False
        execute = tools[name].inputSchema["properties"]["execute"]
        assert execute["type"] == "boolean"
        assert execute["default"] is False
    assert backend.mock_calls == []


async def test_mcp_default_preview_and_denied_execution_never_call_backend():
    service, backend = make_service(allow_writes=False)
    server = create_server(service=service)
    preview = await server.call_tool("start_inspection", {"route_id": "route-1"})
    assert preview.isError is False
    assert preview.structuredContent["data"]["executed"] is False
    assert backend.mock_calls == []
    denied = await server.call_tool("start_inspection", {"route_id": "route-1", "execute": True})
    assert denied.isError is True
    assert denied.structuredContent["error"]["code"] == "writes_disabled"
    assert backend.mock_calls == []


@pytest.mark.parametrize(("field", "value"), [
    ("execute", "true"), ("execute", 1), ("execute", "false"),
    ("map_version", True), ("map_version", "1"), ("x", True), ("x", "1.0"),
])
async def test_mcp_does_not_coerce_wrong_types_into_executable_navigation(field, value):
    service, backend = make_service()
    server = create_server(service=service)
    args = {"map_id": "map-1", "map_version": 1, "x": 1.0, "y": 2.0, "execute": True}
    args[field] = value
    try:
        result = await server.call_tool("navigate_to", args)
    except ToolError:
        # SDK schema validation can reject the call before entering the tool.
        pass
    else:
        assert result.isError is True
    assert backend.mock_calls == []


@pytest.mark.parametrize(("kind", "method"), [("inspection", "get_task"), ("navigation", "get_navigation_task")])
def test_task_status_reads_still_work_when_writes_are_disabled(kind, method):
    service, backend = make_service(allow_writes=False, allowed_robots=())
    getattr(backend, method).return_value = {"task_id": "task-1", "status": "pending"}
    result = service.get_task("task-1", kind=kind)
    assert result["ok"] is True
    getattr(backend, method).assert_called_once_with("task-1", "demo")
    assert len(backend.mock_calls) == 1


def test_terminal_cancellation_does_not_claim_a_command_was_executed():
    service, backend = make_service()
    backend.cancel_task.return_value = {
        "task_id": "task-1", "robot_id": "demo", "status": "completed",
        "cancel_requested": False, "already_terminal": True,
    }
    result = service.cancel_task("task-1", execute=True)
    assert result["ok"] is True
    assert result["data"]["executed"] is False
    assert result["data"]["already_terminal"] is True
    assert "no cancellation command was sent" in result["data"]["completion_note"]
    backend.cancel_task.assert_called_once_with("task-1", "demo", kind="inspection")


def test_huge_integer_coordinate_returns_structured_error_without_backend_access():
    service, backend = make_service()
    with pytest.raises(OmniError) as error:
        service.navigate_to("map-1", 1, 10 ** 1000, 0, execute=True)
    assert error.value.as_dict()["code"] == "invalid_argument"
    assert backend.mock_calls == []

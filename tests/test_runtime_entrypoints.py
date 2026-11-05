"""CLI/MCP route to the same runtime service without robot connections."""
import json
from unittest.mock import Mock

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from omni_tools.cli import run
from omni_tools.config import Settings
from omni_tools.errors import OmniError
from omni_tools.mcp_server import create_server
from omni_tools.service import OmniService


MAP = {"map_id": "map-demo", "map_version": 1, "map_checksum": "a" * 64}
POSE = {"initial_x": 0.25, "initial_y": -0.5, "initial_z": 0.0, "initial_yaw": 1.0}
MAP_FLAGS = ["--map", "map-demo", "--map-version", "1", "--map-checksum", "a" * 64,
             "--initial-x", "0.25", "--initial-y", "-0.5", "--initial-yaw", "1"]
CASES = [
    ("start_localization", {**MAP, **POSE}, ["localization", "start", *MAP_FLAGS]),
    ("start_route_recording", {"route_id": "route-demo", **MAP, **POSE},
     ["routes", "record", "start", "--route", "route-demo", *MAP_FLAGS]),
    ("finish_route_recording", {"recording_operation_id": "epoch/runtime/4", "disposition": "save"},
     ["routes", "record", "save", "--operation", "epoch/runtime/4"]),
    ("finish_route_recording", {"recording_operation_id": "epoch/runtime/4", "disposition": "discard"},
     ["routes", "record", "discard", "--operation", "epoch/runtime/4"]),
    ("set_runtime_idle", {}, ["runtime", "idle"]),
]


def service():
    backend = Mock()
    for name, _, _ in CASES:
        getattr(backend, name).return_value = {"operation_id": "epoch/runtime/5", "completion": "runtime_terminal"}
    return OmniService(Settings(allow_writes=True, allowed_robots=("dog4",)), backend), backend


@pytest.mark.parametrize("method,parameters,command", CASES)
@pytest.mark.parametrize("execute", [False, True])
def test_cli_preserves_runtime_arguments_and_execution(method, parameters, command, execute, capsys):
    api, backend = service()
    flags = ["--robot", "dog4", "--json"] + (["--execute"] if execute else [])
    assert run(command + flags, service=api) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["data"]["executed"] is execute
    if execute:
        getattr(backend, method).assert_called_once_with(**parameters, robot_id="dog4")
        assert result["data"]["operation_id"] == "epoch/runtime/5"
    else:
        assert result["data"]["parameters"] == {**parameters, "robot_id": "dog4"}
        assert backend.mock_calls == []


@pytest.mark.parametrize("method,parameters,command", CASES)
async def test_mcp_runtime_preview_and_execution_share_service(method, parameters, command):
    api, backend = service()
    server = create_server(service=api)
    result = await server.call_tool(method, parameters)
    assert result.isError is False
    assert result.structuredContent["data"]["preview"] is True
    assert backend.mock_calls == []
    result = await server.call_tool(method, {**parameters, "robot_id": "dog4", "execute": True})
    assert result.isError is False
    assert result.structuredContent["data"]["operation_id"] == "epoch/runtime/5"
    getattr(backend, method).assert_called_once_with(**parameters, robot_id="dog4")
    assert json.loads(result.content[0].text) == result.structuredContent


@pytest.mark.parametrize("field,value", [("execute", "true"), ("execute", 1),
                                         ("map_version", True), ("map_version", "1"),
                                         ("initial_x", "0.5")])
async def test_mcp_runtime_rejects_wrong_types_before_backend(field, value):
    api, backend = service()
    try:
        result = await create_server(service=api).call_tool("start_localization", {**MAP, field: value})
    except ToolError:
        pass
    else:
        assert result.isError is True
    assert backend.mock_calls == []


def test_runtime_cli_preserves_unknown_outcome_details(capsys):
    api, backend = service()
    backend.set_runtime_idle.side_effect = OmniError("runtime_timeout", "Not confirmed", {
        "operation_id": "epoch/runtime/5", "retry_safe": False})
    assert run(["runtime", "idle", "--execute", "--json"], service=api) == 2
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["details"] == {"operation_id": "epoch/runtime/5", "retry_safe": False}
    assert backend.set_runtime_idle.call_count == 1

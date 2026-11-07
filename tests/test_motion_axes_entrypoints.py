"""Body-relative motion entrypoints; no robot or network is used."""
import json
from unittest.mock import Mock

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from omni_tools.cli import run
from omni_tools.config import Settings
from omni_tools.mcp_server import create_server
from omni_tools.service import OmniService


@pytest.mark.parametrize("direction", ["forward", "backward", "left", "right"])
def test_move_cli_preserves_direction_distance_robot_and_execution(direction, capsys):
    service = Mock()
    service.move.return_value = {"ok": True}
    assert run(["motion", "move", "--direction", direction, "--distance", "0.2",
                "--robot", "dog4", "--execute", "--json"], service=service) == 0
    service.move.assert_called_once_with(direction, 0.2, "dog4", True)
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_move_cli_defaults_to_one_meter_preview(capsys):
    service = Mock()
    service.move.return_value = {"ok": True}
    assert run(["motion", "move", "--direction", "backward", "--json"], service=service) == 0
    service.move.assert_called_once_with("backward", 1.0, None, False)
    capsys.readouterr()


@pytest.mark.parametrize("options,expected", [
    ([], (90.0, "clockwise", None, False)),
    (["--angle", "15", "--direction", "counterclockwise", "--robot", "dog4", "--execute"],
     (15.0, "counterclockwise", "dog4", True)),
])
def test_rotate_cli_preserves_parameters_and_defaults(options, expected, capsys):
    service = Mock()
    service.rotate.return_value = {"ok": True}
    assert run(["motion", "rotate", *options, "--json"], service=service) == 0
    service.rotate.assert_called_once_with(*expected)
    capsys.readouterr()


@pytest.mark.parametrize("arguments", [
    ["move"], ["move", "--direction", "up"],
    ["move", "--direction", "left", "--distance", "invalid"],
    ["rotate", "--direction", "left"], ["rotate", "--angle", "invalid"],
    ["move", "--direction", "left", "--measurement-mode", "odometry"],
    ["rotate", "--measurement-mode", "timed"],
])
def test_cli_rejects_invalid_options_before_dispatch(arguments, capsys):
    service = Mock()
    assert run(["motion", *arguments], service=service) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_argument"
    assert service.mock_calls == []


@pytest.mark.parametrize("operation,value", [
    ("move", "0.099"), ("move", "2.001"), ("move", "nan"),
    ("rotate", "0"), ("rotate", "361"), ("rotate", "inf"),
])
def test_cli_rejects_invalid_bounds_without_backend_calls(operation, value, capsys):
    backend = Mock()
    service = OmniService(Settings(), backend)
    options = (["--direction", "backward", "--distance", value] if operation == "move"
               else ["--angle", value])
    assert run(["motion", operation, *options, "--execute", "--json"], service=service) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_argument"
    assert backend.mock_calls == []


async def test_motion_mcp_schemas_are_explicit_write_tools():
    service = Mock()
    tools = {tool.name: tool for tool in await create_server(service=service).list_tools()}
    for name, number, default, directions in [
        ("move", "distance_m", 1.0, ["forward", "backward", "left", "right"]),
        ("rotate", "angle_deg", 90.0, ["clockwise", "counterclockwise"]),
    ]:
        tool = tools[name]
        fields = tool.inputSchema["properties"]
        assert tool.annotations.readOnlyHint is False
        assert tool.annotations.destructiveHint is True
        assert tool.annotations.idempotentHint is False
        assert fields[number]["type"] == "number"
        assert fields[number]["default"] == default
        assert fields["direction"]["enum"] == directions
        assert fields["execute"]["type"] == "boolean"
        assert fields["execute"]["default"] is False
        assert "measurement_mode" not in fields
    assert "direction" in tools["move"].inputSchema["required"]
    assert tools["rotate"].inputSchema["properties"]["direction"]["default"] == "clockwise"
    assert service.mock_calls == []


@pytest.mark.parametrize("operation,arguments,expected", [
    ("move", {"direction": "backward"}, ("backward", 1.0, None, False)),
    ("move", {"direction": "left", "distance_m": 0.2, "robot_id": "dog4", "execute": True},
     ("left", 0.2, "dog4", True)),
    ("rotate", {}, (90.0, "clockwise", None, False)),
    ("rotate", {"angle_deg": 15.0, "direction": "counterclockwise", "robot_id": "dog4", "execute": True},
     (15.0, "counterclockwise", "dog4", True)),
])
async def test_motion_mcp_preserves_parameters_and_defaults(operation, arguments, expected):
    service = Mock()
    getattr(service, operation).return_value = {"ok": True}
    result = await create_server(service=service).call_tool(operation, arguments)
    assert result.isError is False
    getattr(service, operation).assert_called_once_with(*expected)


@pytest.mark.parametrize("operation,number", [("move", "distance_m"), ("rotate", "angle_deg")])
@pytest.mark.parametrize("field,value", [
    ("execute", "true"), ("execute", 1), ("execute", None),
    ("number", "1.0"), ("number", True), ("number", None),
    ("direction", "up"), ("direction", True), ("direction", None),
])
async def test_mcp_rejects_wrong_types_before_dispatch(operation, number, field, value):
    service = Mock()
    arguments = {"direction": "forward" if operation == "move" else "clockwise"}
    arguments[number if field == "number" else field] = value
    with pytest.raises(ToolError):
        await create_server(service=service).call_tool(operation, arguments)
    assert service.mock_calls == []


@pytest.mark.parametrize("operation,arguments", [
    ("move", {"direction": "right", "distance_m": 0.099}),
    ("move", {"direction": "right", "distance_m": 2.001}),
    ("rotate", {"angle_deg": 0.0}), ("rotate", {"angle_deg": 361.0}),
])
async def test_mcp_rejects_invalid_bounds_without_backend_calls(operation, arguments):
    backend = Mock()
    result = await create_server(service=OmniService(Settings(), backend)).call_tool(
        operation, {**arguments, "execute": True})
    assert result.isError is True
    assert result.structuredContent["error"]["code"] == "invalid_argument"
    assert backend.mock_calls == []


@pytest.mark.parametrize("operation", ["move", "rotate"])
async def test_cli_and_mcp_preview_never_call_backend(operation, capsys):
    backend = Mock()
    service = OmniService(Settings(), backend)
    options = ["--direction", "right"] if operation == "move" else []
    assert run(["motion", operation, *options, "--json"], service=service) == 0
    assert json.loads(capsys.readouterr().out)["data"]["preview"] is True
    arguments = {"direction": "right"} if operation == "move" else {}
    result = await create_server(service=service).call_tool(operation, arguments)
    assert result.isError is False
    assert result.structuredContent["data"]["preview"] is True
    assert backend.mock_calls == []

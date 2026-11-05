"""Forward-motion CLI and MCP dispatch tests; no robot or network is used."""
import json

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from omni_tools.cli import run
from omni_tools.errors import OmniError
from omni_tools.mcp_server import create_server


class StubForwardService:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def move_forward(self, distance_m=1.0, robot_id=None, execute=False):
        self.calls.append((distance_m, robot_id, execute))
        if self.error is not None:
            raise self.error
        return {"ok": True, "robot_id": robot_id or "dog4", "backend": "ssh",
                "data": {"operation": "move_forward", "distance_m": distance_m,
                         "executed": execute, "preview": not execute}}


@pytest.mark.parametrize("arguments", [["motion", "forward"], ["向前"]])
def test_forward_cli_defaults_to_one_meter_preview(arguments, capsys):
    service = StubForwardService()
    assert run(arguments + ["--json"], service=service) == 0
    result = json.loads(capsys.readouterr().out)
    assert service.calls == [(1.0, None, False)]
    assert result["data"]["preview"] is True
    assert result["data"]["distance_m"] == 1.0


@pytest.mark.parametrize("arguments", [["motion", "forward"], ["向前"]])
@pytest.mark.parametrize("execute", [False, True])
def test_forward_cli_preserves_distance_robot_and_execution(arguments, execute, capsys):
    service = StubForwardService()
    options = ["--distance", "0.75", "--robot", "dog4", "--json"]
    if execute:
        options.append("--execute")
    assert run(arguments + options, service=service) == 0
    result = json.loads(capsys.readouterr().out)
    assert service.calls == [(0.75, "dog4", execute)]
    assert result["data"]["executed"] is execute


def test_forward_cli_rejects_unparseable_distance_without_dispatch(capsys):
    service = StubForwardService()
    assert run(["motion", "forward", "--distance", "invalid", "--execute"], service=service) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_argument"
    assert service.calls == []


def test_forward_cli_preserves_feedback_failure_details(capsys):
    service = StubForwardService(OmniError("feedback_lost", "Motion stopped", {"completed": False}))
    assert run(["向前", "--execute", "--json"], service=service) == 2
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "feedback_lost"
    assert error["details"]["completed"] is False


async def test_forward_mcp_schema_is_an_explicit_write_tool():
    service = StubForwardService()
    tools = {tool.name: tool for tool in await create_server(service=service).list_tools()}
    tool = tools["move_forward"]
    assert tool.annotations.readOnlyHint is False
    assert tool.annotations.destructiveHint is True
    assert tool.annotations.idempotentHint is False
    assert tool.inputSchema["properties"]["distance_m"]["type"] == "number"
    assert tool.inputSchema["properties"]["distance_m"]["default"] == 1.0
    assert tool.inputSchema["properties"]["execute"]["type"] == "boolean"
    assert tool.inputSchema["properties"]["execute"]["default"] is False
    assert "body-forward" in tool.description
    assert "0.1..2" in tool.description
    assert "feedback" in tool.description
    assert service.calls == []


async def test_forward_mcp_default_preview_and_explicit_execution_dispatch():
    service = StubForwardService()
    server = create_server(service=service)
    preview = await server.call_tool("move_forward", {})
    assert preview.isError is False
    assert preview.structuredContent["data"]["preview"] is True
    assert service.calls == [(1.0, None, False)]
    result = await server.call_tool("move_forward", {
        "distance_m": 0.75, "robot_id": "dog4", "execute": True,
    })
    assert result.isError is False
    assert result.structuredContent["data"]["executed"] is True
    assert service.calls[-1] == (0.75, "dog4", True)
    assert json.loads(result.content[0].text) == result.structuredContent


@pytest.mark.parametrize(("field", "value"), [
    ("execute", "true"), ("execute", 1), ("execute", None),
    ("distance_m", "1.0"), ("distance_m", True), ("distance_m", None),
])
async def test_forward_mcp_rejects_wrong_parameter_types_before_dispatch(field, value):
    service = StubForwardService()
    arguments = {"distance_m": 1.0, "execute": True, field: value}
    try:
        result = await create_server(service=service).call_tool("move_forward", arguments)
    except ToolError:
        pass
    else:
        assert result.isError is True
    assert service.calls == []


async def test_forward_mcp_reports_feedback_failure_as_error():
    service = StubForwardService(OmniError("feedback_lost", "Motion stopped", {"completed": False}))
    result = await create_server(service=service).call_tool("move_forward", {"execute": True})
    assert result.isError is True
    assert result.structuredContent["ok"] is False
    assert result.structuredContent["error"]["code"] == "feedback_lost"
    assert result.structuredContent["error"]["details"]["completed"] is False

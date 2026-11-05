"""Posture entry points dispatch consistently without any robot connections."""
import json

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from omni_tools.cli import build_parser, run
from omni_tools.errors import OmniError
from omni_tools.mcp_server import create_server


CLI_CASES = [
    (["posture", "stand"], "stand_up"),
    (["posture", "lie"], "lie_down"),
    (["站立"], "stand_up"),
    (["起立"], "stand_up"),
    (["站起来"], "stand_up"),
    (["坐下"], "lie_down"),
    (["卧下"], "lie_down"),
    (["趴下"], "lie_down"),
]


class StubPostureService:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def _posture(self, operation, robot_id, execute):
        self.calls.append((operation, robot_id, execute))
        if self.error is not None:
            raise self.error
        return {"ok": True, "backend": "ssh", "robot_id": robot_id or "dog4",
                "data": {"operation": operation, "executed": execute, "preview": not execute}}

    def stand_up(self, robot_id=None, execute=False):
        return self._posture("stand_up", robot_id, execute)

    def lie_down(self, robot_id=None, execute=False):
        return self._posture("lie_down", robot_id, execute)


@pytest.mark.parametrize(("arguments", "method"), CLI_CASES)
@pytest.mark.parametrize("execute", [False, True])
def test_cli_posture_aliases_share_service_dispatch(arguments, method, execute, capsys):
    service = StubPostureService()
    options = ["--robot", "dog4", "--json"] + (["--execute"] if execute else [])
    assert run(arguments + options, service=service) == 0
    payload = json.loads(capsys.readouterr().out)
    assert service.calls == [(method, "dog4", execute)]
    assert payload["data"]["operation"] == method
    assert payload["data"]["executed"] is execute


@pytest.mark.parametrize(("arguments", "method"), CLI_CASES)
def test_cli_posture_defaults_to_preview_and_configured_robot(arguments, method, capsys):
    service = StubPostureService()
    assert run(arguments, service=service) == 0
    payload = json.loads(capsys.readouterr().out)
    assert service.calls == [(method, None, False)]
    assert payload["data"]["preview"] is True


def test_posture_commands_keep_existing_global_configuration_options():
    args = build_parser().parse_args([
        "--backend", "ssh", "--ssh-host", "dog4", "--timeout", "15",
        "卧下", "--execute", "--json",
    ])
    assert (args.backend, args.ssh_host, args.timeout) == ("ssh", "dog4", 15)
    assert args.posture_method == "lie_down"
    assert args.execute is True


def test_cli_posture_invalid_action_does_not_call_service(capsys):
    service = StubPostureService()
    assert run(["posture", "sit", "--execute"], service=service) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_argument"
    assert service.calls == []


def test_cli_posture_failure_is_structured(capsys):
    service = StubPostureService(OmniError("writes_disabled", "Execution is disabled"))
    assert run(["站立", "--execute", "--json"], service=service) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "writes_disabled"


async def test_mcp_posture_tools_advertise_explicit_execution_and_chinese_aliases():
    service = StubPostureService()
    server = create_server(service=service)
    tools = {tool.name: tool for tool in await server.list_tools()}
    for name, aliases in [
        ("stand_up", ["站立", "起立", "站起来"]),
        ("lie_down", ["坐下", "卧下", "趴下"]),
    ]:
        tool = tools[name]
        assert tool.annotations.readOnlyHint is False
        assert tool.annotations.destructiveHint is True
        assert tool.annotations.idempotentHint is False
        assert tool.inputSchema["properties"]["execute"]["type"] == "boolean"
        assert tool.inputSchema["properties"]["execute"]["default"] is False
        assert all(alias in tool.description for alias in aliases)
    assert service.calls == []


@pytest.mark.parametrize("method", ["stand_up", "lie_down"])
async def test_mcp_posture_preview_and_execution_dispatch(method):
    service = StubPostureService()
    server = create_server(service=service)
    preview = await server.call_tool(method, {})
    assert preview.isError is False
    assert preview.structuredContent["data"]["preview"] is True
    assert service.calls == [(method, None, False)]
    result = await server.call_tool(method, {"robot_id": "dog4", "execute": True})
    assert result.isError is False
    assert result.structuredContent["data"]["executed"] is True
    assert service.calls[-1] == (method, "dog4", True)
    assert json.loads(result.content[0].text) == result.structuredContent


@pytest.mark.parametrize("method", ["stand_up", "lie_down"])
@pytest.mark.parametrize("execute", ["true", "false", 1, None])
async def test_mcp_posture_rejects_non_boolean_execution(method, execute):
    service = StubPostureService()
    server = create_server(service=service)
    try:
        result = await server.call_tool(method, {"execute": execute})
    except ToolError:
        pass
    else:
        assert result.isError is True
    assert service.calls == []


async def test_mcp_posture_failure_is_structured():
    service = StubPostureService(OmniError("robot_busy", "Robot is executing another task"))
    result = await create_server(service=service).call_tool("lie_down", {"execute": True})
    assert result.isError is True
    assert result.structuredContent["ok"] is False
    assert result.structuredContent["error"]["code"] == "robot_busy"

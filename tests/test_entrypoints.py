import json
import math

import pytest

from omni_tools.cli import run
from omni_tools.config import Settings
from omni_tools.errors import OmniError
from omni_tools.mcp_server import create_server
from omni_tools.service import OmniService


class FakeBackend:
    def get_robot_status(self, robot_id):
        return {"battery_percentage": math.nan, "mode": "idle"}

    def list_maps(self, robot_id):
        return {"maps": [{"map_id": "map-demo"}]}

    def list_routes(self, robot_id):
        return {"routes": []}

    def get_task(self, task_id, robot_id):
        return {"task_id": task_id, "status": "unknown"}

    def get_inspection_result(self, task_id, robot_id):
        raise OmniError("not_available", "Results not available")


@pytest.fixture
def service():
    return OmniService(Settings(), FakeBackend())


def test_cli_returns_valid_json_and_preserves_unknown(service, capsys):
    assert run(["robot", "status", "dog4", "--json"], service=service) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["data"]["battery_percentage"] is None
    assert result["backend"] == "ssh"


def test_cli_error_is_structured_and_nonzero(service, capsys):
    assert run(["inspection", "result", "mission-1", "--json"], service=service) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "not_available"


def test_invalid_input_never_calls_backend(service, capsys):
    assert run(["robot", "status", "../../dog4", "--json"], service=service) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_argument"


@pytest.mark.parametrize("kwargs", [{"ssh_host": "-oProxyCommand=bad"},
                                       {"timeout": math.nan}, {"timeout": 0},
                                       {"backend": "http", "base_url": "http://example.org", "token": "secret"}])
def test_invalid_config_rejected(kwargs):
    with pytest.raises(OmniError):
        Settings(**kwargs)


async def test_mcp_tools_have_read_only_annotations_and_structured_errors(service):
    server = create_server(service=service)
    tools = await server.list_tools()
    reads = {"get_robot_status", "list_maps", "list_routes", "get_task", "get_inspection_result"}
    writes = {"start_inspection", "navigate_to", "cancel_task"}
    assert {t.name for t in tools} == reads | writes
    assert all(t.annotations.readOnlyHint and not t.annotations.destructiveHint for t in tools if t.name in reads)
    assert all(not t.annotations.readOnlyHint and t.annotations.destructiveHint for t in tools if t.name in writes)
    result = await server.call_tool("get_robot_status", {"robot_id": "dog4"})
    assert not result.isError
    assert result.structuredContent["data"]["battery_percentage"] is None
    error = await server.call_tool("get_inspection_result", {"task_id": "missing"})
    assert error.isError
    assert error.structuredContent["error"]["code"] == "not_available"

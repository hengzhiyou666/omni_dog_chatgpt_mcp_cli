"""Public CLI/MCP through real service+HTTP adapter; transport stays in memory."""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from omni_tools.backends.http import HTTPBackend
from omni_tools.cli import run
from omni_tools.config import Settings
from omni_tools.mcp_server import create_server
from omni_tools.service import OmniService


@pytest.fixture
def integrated_service():
    requests = []
    task = {"task_id": "task-demo", "robot_id": "demo", "route_id": "route-demo", "status": "pending"}

    def handle(request):
        requests.append((request.method, request.url.path))
        path = request.url.path.removeprefix("/api/v1")
        if request.method == "POST" and path == "/patrol/tasks":
            assert json.loads(request.content)["route_id"] == "route-demo"
            data = task.copy()
        elif request.method == "POST" and path == "/patrol/tasks/task-demo/start":
            task["status"] = "dispatching"
            data = task.copy()
        elif request.method == "GET" and path == "/patrol/tasks/task-demo":
            data = task.copy()
        elif request.method == "POST" and path == "/patrol/tasks/task-demo/cancel":
            task["status"] = "cancelling"
            data = None
        elif request.method == "POST" and path == "/robots/demo/navigation/plans":
            body = json.loads(request.content)
            data = {**body, "plan_id": "plan-demo", "robot_id": "demo", "status": "ready",
                    "executable": True, "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()}
        elif request.method == "POST" and path == "/robots/demo/navigation/goals":
            assert json.loads(request.content) == {"plan_id": "plan-demo"}
            data = {"goal_id": "goal-demo", "plan_id": "plan-demo", "robot_id": "demo",
                    "map_id": "map-demo", "map_version": 1, "status": "pending"}
        else:
            pytest.fail(f"Unexpected request: {request.method} {path}")
        return httpx.Response(200, json={"code": 0, "data": data})

    settings = Settings(backend="http", robot_id="demo", base_url="https://offline.example",
                        token="test-token", allow_writes=True, allowed_robots=("demo",))
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        yield OmniService(settings, HTTPBackend(settings, client=client)), requests


def test_cli_start_reaches_adapter_and_preserves_dispatching(integrated_service, capsys):
    service, requests = integrated_service
    assert run(["inspection", "start", "--robot", "demo", "--route", "route-demo", "--execute", "--json"], service=service) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["task_id"] == "task-demo"
    assert data["status"] == "dispatching"
    assert requests == [("POST", "/api/v1/patrol/tasks"), ("POST", "/api/v1/patrol/tasks/task-demo/start")]


async def test_mcp_navigation_preserves_pending_not_arrived(integrated_service):
    service, requests = integrated_service
    result = await create_server(service=service).call_tool("navigate_to", {
        "robot_id": "demo", "map_id": "map-demo", "map_version": 1, "x": 1.0, "y": 2.0, "execute": True})
    assert not result.isError
    assert result.structuredContent["data"]["goal_id"] == "goal-demo"
    assert result.structuredContent["data"]["status"] == "pending"
    assert requests == [("POST", "/api/v1/robots/demo/navigation/plans"), ("POST", "/api/v1/robots/demo/navigation/goals")]


async def test_mcp_cancel_preserves_cancelling_not_stopped(integrated_service):
    service, requests = integrated_service
    result = await create_server(service=service).call_tool("cancel_task", {
        "robot_id": "demo", "task_id": "task-demo", "execute": True})
    assert not result.isError
    assert result.structuredContent["data"]["status"] == "cancelling"
    assert requests == [("GET", "/api/v1/patrol/tasks/task-demo"),
                        ("POST", "/api/v1/patrol/tasks/task-demo/cancel"),
                        ("GET", "/api/v1/patrol/tasks/task-demo")]

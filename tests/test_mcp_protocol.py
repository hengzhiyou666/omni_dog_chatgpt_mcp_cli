"""Exercise the actual stdio protocol without contacting a robot or the internet."""

import json
import os
import socket
import subprocess
import sys
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import anyio
import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client


TOOLS = {
    "get_robot_status", "list_maps", "list_routes", "get_task", "get_inspection_result",
}
ROBOT_STATUS = {
    "robot_id": "demo", "online": True, "battery_valid": False,
    "battery": {"percent": None},
}

# Loaded by the real MCP subprocess before any application imports. In addition
# to an explicit HTTP configuration, this prevents an accidental SSH fallback,
# external DNS lookup or internet connection from reaching a real machine.
NETWORK_GUARD = r'''
import ipaddress
import os
import socket
import sys

def loopback(host):
    if isinstance(host, bytes):
        host = host.decode("ascii")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False

def deny(event):
    with open(os.environ["OMNI_TEST_NETWORK_VIOLATIONS"], "a") as stream:
        stream.write(event + "\n")
    raise RuntimeError("Offline MCP test blocked " + event)

def audit(event, args):
    if event == "socket.getaddrinfo" and not loopback(args[0]):
        deny(event)
    if event == "socket.connect":
        sock, address = args
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not loopback(address[0]):
            deny(event)
    if event in ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn"):
        deny(event)

sys.addaudithook(audit)
'''


@pytest.fixture
def mock_http():
    state = SimpleNamespace(
        status=200,
        body={"code": 0, "message": "success", "data": ROBOT_STATUS},
        requests=[],
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state.requests.append((self.command, self.path, self.headers.get("Authorization")))
            body = json.dumps(state.body).encode()
            self.send_response(state.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    state.url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
        assert not worker.is_alive()


@pytest.fixture
def stdio_parameters(mock_http, tmp_path):
    (tmp_path / "sitecustomize.py").write_text(NETWORK_GUARD)
    violations = tmp_path / "network-violations.txt"
    # Do not copy the developer's OMNI_*, credentials or proxy environment.
    env = {
        "OMNI_BACKEND": "http",
        "OMNI_BASE_URL": mock_http.url,
        "OMNI_TOKEN": "test-token",
        "OMNI_ROBOT_ID": "demo",
        "OMNI_TIMEOUT": "2",
        "OMNI_TEST_NETWORK_VIOLATIONS": str(violations),
        "PYTHONPATH": os.pathsep.join((str(tmp_path), str(Path(__file__).resolve().parents[1] / "src"))),
        "PYTHONUNBUFFERED": "1",
        "NO_PROXY": "*",
    }
    yield StdioServerParameters(
        command=sys.executable,
        args=["-m", "omni_tools.mcp_server"],
        env=env,
    )
    assert not violations.exists(), "MCP subprocess attempted non-loopback traffic or subprocess execution"


async def test_sdk_stdio_initialization_discovery_and_status(stdio_parameters, mock_http):
    with anyio.fail_after(25):
        async with stdio_client(stdio_parameters) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=15)) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.name == "Omni Tools"
                assert initialized.capabilities.tools is not None

                discovered = await session.list_tools()
                assert {tool.name for tool in discovered.tools} == TOOLS
                assert len(discovered.tools) == 5
                for tool in discovered.tools:
                    assert tool.inputSchema["type"] == "object"
                    assert tool.annotations.readOnlyHint is True
                    assert tool.annotations.destructiveHint is False

                # An empty argument object exercises OMNI_ROBOT_ID resolution.
                result = await session.call_tool("get_robot_status", {})
                assert result.isError is False
                payload = result.structuredContent
                assert payload["ok"] is True
                assert payload["backend"] == "http"
                assert payload["robot_id"] == "demo"
                assert payload["data"] == ROBOT_STATUS
                assert payload["data"]["battery"]["percent"] is None
                text = [item.text for item in result.content if item.type == "text"]
                assert len(text) == 1
                assert json.loads(text[0]) == payload

    assert mock_http.requests == [("GET", "/api/v1/robots/demo/status", "Bearer test-token")]


@pytest.mark.parametrize(
    ("status", "upstream_code", "expected_error"),
    [(503, 50301, "upstream_http_error"), (200, 41001, "upstream_rejected")],
)
async def test_sdk_stdio_upstream_failures_are_tool_errors(
    stdio_parameters, mock_http, status, upstream_code, expected_error,
):
    mock_http.status = status
    mock_http.body = {"code": upstream_code, "message": "private upstream detail", "data": None}
    with anyio.fail_after(25):
        async with stdio_client(stdio_parameters) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=15)) as session:
                await session.initialize()
                result = await session.call_tool("get_robot_status", {"robot_id": "demo"})
                assert result.isError is True
                payload = result.structuredContent
                assert payload["ok"] is False
                assert payload["error"]["code"] == expected_error
                assert payload["error"]["details"]["http_status"] == status
                assert payload["error"]["details"]["upstream_code"] == upstream_code
                serialized = json.dumps(result.model_dump(mode="json"))
                assert "test-token" not in serialized
                assert "private upstream detail" not in serialized

    assert mock_http.requests == [("GET", "/api/v1/robots/demo/status", "Bearer test-token")]


@pytest.fixture
def http_mcp_process(stdio_parameters, tmp_path):
    # The application accepts a concrete port; reserve an unused loopback port
    # briefly rather than relying on a fixed port that another test may occupy.
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    log_path = tmp_path / "http-mcp.log"
    with log_path.open("w") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "omni_tools.mcp_server", "--transport", "streamable-http",
             "--port", str(port)],
            env=stdio_parameters.env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            yield process, f"http://127.0.0.1:{port}", log_path
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


async def test_sdk_streamable_http_initialization_discovery_and_status(http_mcp_process, mock_http):
    process, base_url, log_path = http_mcp_process
    # The client cannot inherit a developer's proxy settings either.
    async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
        with anyio.fail_after(25):
            while True:
                assert process.poll() is None, log_path.read_text()
                try:
                    # A 404 at / still proves the HTTP application has started.
                    await client.get(base_url + "/")
                    break
                except httpx.ConnectError:
                    await anyio.sleep(0.05)

            async with streamable_http_client(base_url + "/mcp", http_client=client) as (read, write, _):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=15)) as session:
                    initialized = await session.initialize()
                    assert initialized.serverInfo.name == "Omni Tools"
                    assert initialized.capabilities.tools is not None
                    discovered = await session.list_tools()
                    assert len(discovered.tools) == 5
                    assert {tool.name for tool in discovered.tools} == TOOLS
                    result = await session.call_tool("get_robot_status", {"robot_id": "demo"})
                    assert result.isError is False
                    assert result.structuredContent["ok"] is True
                    assert result.structuredContent["data"] == ROBOT_STATUS

    assert mock_http.requests == [("GET", "/api/v1/robots/demo/status", "Bearer test-token")]

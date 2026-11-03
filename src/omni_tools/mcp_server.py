"""Official MCP SDK entry point: stdio or loopback Streamable HTTP."""
import argparse
import asyncio
import sys

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from .config import Settings
from .errors import OmniError
from .service import OmniService
from .util import json_text


def create_server(settings: Settings | None = None, *, service=None, port: int = 8766):
    service = service or OmniService(settings or Settings.from_env())
    server = FastMCP("Omni Tools", host="127.0.0.1", port=port,
                     stateless_http=True, json_response=True,
                     instructions="Query Omni robot state. Unknown data is not success. "
                                  "SSH task IDs are native ROS IDs; HTTP task IDs are cloud IDs.")
    annotations = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                  idempotentHint=True, openWorldHint=True)

    async def call(method, *args):
        try:
            value = await asyncio.to_thread(getattr(service, method), *args)
            return CallToolResult(content=[TextContent(type="text", text=json_text(value))],
                                  structuredContent=value, isError=False)
        except OmniError as exc:
            value = {"ok": False, "error": exc.as_dict()}
            return CallToolResult(content=[TextContent(type="text", text=json_text(value))],
                                  structuredContent=value, isError=True)

    @server.tool(annotations=annotations)
    async def get_robot_status(robot_id: str | None = None) -> CallToolResult:
        """Read robot battery, runtime and connectivity; unknown battery remains null."""
        return await call("get_robot_status", robot_id)

    @server.tool(annotations=annotations)
    async def list_maps(robot_id: str | None = None) -> CallToolResult:
        """List available maps with their IDs and versions; never activate a map."""
        return await call("list_maps", robot_id)

    @server.tool(annotations=annotations)
    async def list_routes(robot_id: str | None = None) -> CallToolResult:
        """List inspection routes and scope information; never execute a route."""
        return await call("list_routes", robot_id)

    @server.tool(annotations=annotations)
    async def get_task(task_id: str, robot_id: str | None = None) -> CallToolResult:
        """Read task status. SSH only observes live tasks; unobserved does not mean completed."""
        return await call("get_task", task_id, robot_id)

    @server.tool(annotations=annotations)
    async def get_inspection_result(task_id: str, robot_id: str | None = None) -> CallToolResult:
        """Read inspection results/evidence. Empty evidence does not prove task completion."""
        return await call("get_inspection_result", task_id, robot_id)

    return server


def main():
    parser = argparse.ArgumentParser(description="Omni MCP: read-only robot queries")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    try:
        create_server(port=args.port).run(transport=args.transport)
    except OmniError as exc:
        print(json_text({"ok": False, "error": exc.as_dict()}), file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()

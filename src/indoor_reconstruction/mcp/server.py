"""MCP 2.x transport adapter; all domain rules remain in scene_api."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

import mcp.server.stdio
import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server

from indoor_reconstruction import __version__
from indoor_reconstruction.repository import load_repo_module


def _adapter():
    return load_repo_module(
        "_indoor_reconstruction_scene_mcp_adapter", "scene-core/mcp_server.py"
    )


def create_server(scene_path: Path, identity: dict[str, Any]) -> Server[Any]:
    adapter = _adapter()

    async def list_tools(_ctx: Any, _params: Any) -> types.ListToolsResult:
        tools = []
        for spec in sorted(adapter.public_tools(identity), key=lambda item: item["name"]):
            tools.append(types.Tool(
                name=spec["name"],
                description=spec["description"],
                input_schema=spec["inputSchema"],
                output_schema={
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                    "additionalProperties": True,
                },
            ))
        return types.ListToolsResult(tools=tools)

    async def call_tool(_ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        result = adapter.call_tool(scene_path, identity, params.name, params.arguments or {})
        return types.CallToolResult.model_validate(result)

    return Server(
        "semantic-scene-v2",
        version=__version__,
        instructions=adapter.AGENT_GUIDE,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


async def _serve(server: Server[Any]) -> None:
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(
                notification_options=NotificationOptions(),
                experimental_capabilities={},
            ),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Official MCP SDK stdio server for Semantic Scene V2")
    parser.add_argument("--scene", required=True, type=Path)
    parser.add_argument("--identity", required=True, type=Path)
    args = parser.parse_args(argv)
    adapter = _adapter()
    try:
        identity = adapter.api.execution_identity_api.load_identity(args.identity.resolve())
    except adapter.api.execution_identity_api.IdentityError as error:
        print(str(error), file=sys.stderr)
        return 2
    asyncio.run(_serve(create_server(args.scene.resolve(), identity)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

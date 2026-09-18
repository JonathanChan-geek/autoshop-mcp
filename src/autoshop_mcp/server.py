"""Real MCP stdio transport using the official SDK 2.2; offline editing and native compilation."""
import asyncio
import json
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types
from .core import TOOL_SPECS, run_tool


async def list_tools(context, params):
    mapping = {"str": "string", "bool": "boolean", "list": "array"}
    tools = []
    for spec in TOOL_SPECS:
        props, required = {}, []
        for name, desc in spec["params"].items():
            props[name] = {"type": mapping[desc["type"]], "description": desc["help"]}
            if desc["type"] == "list":
                props[name]["items"] = {"type": "string"}
            if desc.get("required"):
                required.append(name)
        tools.append(types.Tool(name=spec["name"], description=spec["summary"],
                               inputSchema={"type": "object", "properties": props,
                                            "required": required, "additionalProperties": False}))
    return types.ListToolsResult(tools=tools)


async def call_tool(context, params):
    result = await asyncio.to_thread(run_tool, params.name, params.arguments or {})
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
                                isError=not result.get("ok", False))


async def serve():
    app = Server("autoshop-native", version="0.2.0", on_list_tools=list_tools, on_call_tool=call_tool)
    async with stdio_server() as (reader, writer):
        await app.run(reader, writer, app.create_initialization_options())


def main():
    asyncio.run(serve())


if __name__ == "__main__":
    main()

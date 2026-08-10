import asyncio
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP


server = FastMCP('ovid-core-test')


@server.tool(description='Report whether the configured credential reached the server.')
def credential_loaded() -> bool:
    return os.environ.get('MCP_TEST_SECRET') == 'mcp-secret'


@server.tool(description='Tool excluded by the client configuration.')
def excluded() -> bool:
    return True


@server.tool(description='Wait until the client cancels the call.')
async def wait_forever() -> None:
    Path(os.environ['MCP_MARKER']).write_text('started', encoding='utf-8')
    await asyncio.Event().wait()


if __name__ == '__main__':
    server.run(transport='stdio')

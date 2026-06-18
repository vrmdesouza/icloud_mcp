"""Entry point for the iCloud MCP server."""

import logging

from icloud_mcp.server import mcp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

mcp.run(transport="stdio")

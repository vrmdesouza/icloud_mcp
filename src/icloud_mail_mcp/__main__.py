"""Entry point for the iCloud Mail MCP server."""

import logging

from icloud_mail_mcp.server import mcp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

mcp.run(transport="stdio")

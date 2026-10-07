"""The shared FastMCP instance and result base class.

Each tool lives in its own module under `nakomis_dragonfruit_mcp/tools/` and
registers itself on `mcp` with `@mcp.tool()`; `server.py` imports them all.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

mcp = FastMCP("nakomis-dragonfruit-mcp")


class ToolResult(BaseModel):
    """Every tool result carries `warnings`: anything the caller must hear."""

    warnings: list[str] = Field(default_factory=list)

#!/usr/bin/env python3
"""nakomis-dragonfruit-mcp: headless resin print preparation with DragonFruit's engine.

Unofficial. Not affiliated with or endorsed by the Open Resin Alliance or the
DragonFruit project.

Every tool wraps `dragonfruit-cli` (or our own `dragonfruit-mcp-tools`) as a
subprocess and returns a typed result with a `warnings` list: anything the
caller must hear goes there rather than being dropped.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from nakomis_dragonfruit_mcp import cli

mcp = FastMCP("nakomis-dragonfruit-mcp")


class ToolResult(BaseModel):
    warnings: list[str] = Field(default_factory=list)


class EngineInfo(ToolResult):
    cli_path: str
    version: str
    formats: list[str] = Field(description="Output extensions the engine can write")
    slice_defaults: dict = Field(description="Engine defaults; never relied on for a real printer")


@mcp.tool()
def engine_info() -> EngineInfo:
    """Report which dragonfruit-cli is in use and the print formats it can write."""
    result = cli.run(cli.DRAGONFRUIT_CLI, ["info"], parse_json=True)
    info = result.data
    # Upstream dev moves fast: a changed schema should say so, not KeyError.
    if not isinstance(info, dict) or not {"version", "supported_formats"} <= info.keys():
        raise cli.CliError(f"unexpected `dragonfruit-cli info` output: {result.stdout[:200]!r}")
    warnings = []
    if "slice_defaults" not in info:
        warnings.append("`dragonfruit-cli info` no longer reports slice_defaults")
    return EngineInfo(
        cli_path=str(result.exe),
        version=info["version"],
        formats=info["supported_formats"],
        slice_defaults=info.get("slice_defaults", {}),
        warnings=warnings,
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

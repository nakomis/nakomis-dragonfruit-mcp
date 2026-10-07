"""engine_info: which dragonfruit-cli is in use and what it can write."""

from __future__ import annotations

from pydantic import Field

from nakomis_dragonfruit_mcp import cli
from nakomis_dragonfruit_mcp.app import ToolResult, mcp


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

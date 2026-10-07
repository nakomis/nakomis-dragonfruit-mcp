"""hollow and drill_holes: save resin by hollowing a model, then let the cavity drain."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from nakomis_dragonfruit_mcp import cli
from nakomis_dragonfruit_mcp.app import ToolResult, mcp

_HOLLOW_KEYS = {"before", "after", "wall_mm", "voxel_mm", "timing_ms", "warnings"}
_PUNCH_KEYS = {"before", "after", "holes", "timing_ms", "warnings"}


class MeshSummary(BaseModel):
    triangles: int
    volume_ml: float
    shells: int = Field(description="Connected surfaces: the skin, plus one per sealed cavity")
    cavities: int = Field(description="Sealed internal voids; any left means trapped resin")
    watertight: bool


class Hole(BaseModel):
    """A drain hole as placed: model coordinates in mm, direction a unit vector."""

    x: float
    y: float
    z: float
    radius_mm: float
    direction: list[float]
    length_mm: float
    note: str = ""


class HollowResult(ToolResult):
    input_path: str
    output_path: str
    wall_mm: float
    voxel_mm: float = Field(description="Voxel size actually used, after rounding to the grid")
    solid_volume_ml: float
    hollow_volume_ml: float
    resin_saved_ml: float
    resin_saved_pct: float
    before: MeshSummary
    after: MeshSummary
    timing_ms: dict[str, float]


class DrillResult(ToolResult):
    input_path: str
    output_path: str
    holes: list[Hole]
    cavities_before: int
    cavities_after: int
    drains: bool = Field(description="True when no sealed cavity is left")
    volume_before_ml: float
    volume_after_ml: float
    after: MeshSummary
    timing_ms: dict[str, float]


def _summary(raw: dict[str, Any]) -> MeshSummary:
    return MeshSummary(
        triangles=raw["triangles"],
        volume_ml=raw["volume_ml"],
        shells=raw["shells"],
        cavities=raw["cavities"],
        watertight=raw["watertight"],
    )


def _output_path(stl_path: Path, out_path: str | Path | None, suffix: str) -> Path:
    if out_path is not None:
        return Path(out_path).expanduser()
    return stl_path.with_name(f"{stl_path.stem}{suffix}.stl")


def _check(stl_path: Path) -> Path:
    path = stl_path.expanduser()
    if not path.is_file():
        raise cli.CliError(f"model not found: {path}")
    return path


def _schema(data: Any, keys: set[str], what: str, stdout: str) -> dict[str, Any]:
    # Our own tool, but a stale bin/ is easy to have: say so rather than KeyError.
    if not isinstance(data, dict) or not keys <= data.keys():
        raise cli.CliError(
            f"unexpected `dragonfruit-mcp-tools {what}` output (stale bin/? run scripts/build.sh): "
            f"{stdout[:200]!r}"
        )
    return data


@mcp.tool()
def hollow(
    stl_path: str | Path,
    wall_mm: float = 2.0,
    out_path: str | Path | None = None,
    voxel_mm: float | None = None,
    options_json: str | Path | None = None,
) -> HollowResult:
    """Hollow a model, leaving a sealed cavity inside a wall of `wall_mm`.

    Uses DragonFruit's voxel hollowing with the desktop app's defaults (0.65 mm
    voxels). Parts thinner than twice the wall stay solid. The cavity is sealed, so
    the result carries a warning until drain holes are added with `drill_holes`.
    """
    src = _check(Path(stl_path))
    out = _output_path(src, out_path, ".hollow")
    args = ["hollow", str(src), "-o", str(out), "--wall-mm", f"{wall_mm:g}", "--json"]
    if voxel_mm is not None:
        args += ["--voxel-mm", f"{voxel_mm:g}"]
    if options_json is not None:
        args += ["--options-json", str(options_json)]
    result = cli.run(cli.MCP_TOOLS, args, parse_json=True)
    data = _schema(result.data, _HOLLOW_KEYS, "hollow", result.stdout)
    before, after = _summary(data["before"]), _summary(data["after"])
    saved = before.volume_ml - after.volume_ml
    return HollowResult(
        input_path=str(src),
        output_path=str(out),
        wall_mm=data["wall_mm"],
        voxel_mm=data["voxel_mm"],
        solid_volume_ml=before.volume_ml,
        hollow_volume_ml=after.volume_ml,
        resin_saved_ml=saved,
        resin_saved_pct=saved / before.volume_ml * 100 if before.volume_ml else 0.0,
        before=before,
        after=after,
        timing_ms=data["timing_ms"],
        warnings=list(data["warnings"]),
    )


@mcp.tool()
def drill_holes(
    stl_path: str | Path,
    holes: list[dict[str, Any]] | None = None,
    auto_base: bool = False,
    out_path: str | Path | None = None,
    radius_mm: float = 2.0,
) -> DrillResult:
    """Punch drain holes through a (hollowed) model so the cavity can drain.

    Give `holes` as dicts of x, y, z in model millimetres, with optional radius,
    direction (default straight down) and length (default through everything on
    the axis), or set `auto_base` to put two holes at the lowest point of the
    cavity, on opposite sides, for an upright print. Warns if a sealed cavity is
    left, which would mean the holes missed it.
    """
    if bool(holes) == auto_base:
        raise cli.CliError("give either `holes` or `auto_base=True`, not both or neither")
    src = _check(Path(stl_path))
    out = _output_path(src, out_path, ".drilled")
    args = ["punch", str(src), "-o", str(out), "--radius-mm", f"{radius_mm:g}", "--json"]
    args += ["--auto-base"] if auto_base else ["--holes", json.dumps(holes)]
    result = cli.run(cli.MCP_TOOLS, args, parse_json=True)
    data = _schema(result.data, _PUNCH_KEYS, "punch", result.stdout)
    before, after = _summary(data["before"]), _summary(data["after"])
    return DrillResult(
        input_path=str(src),
        output_path=str(out),
        holes=[Hole(**h) for h in data["holes"]],
        cavities_before=before.cavities,
        cavities_after=after.cavities,
        drains=after.cavities == 0,
        volume_before_ml=before.volume_ml,
        volume_after_ml=after.volume_ml,
        after=after,
        timing_ms=data["timing_ms"],
        warnings=list(data["warnings"]),
    )

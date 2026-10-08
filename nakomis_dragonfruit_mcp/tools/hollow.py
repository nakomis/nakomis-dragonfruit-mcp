"""hollow and drill_holes: save resin by hollowing a model, then let the cavity drain."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from nakomis_dragonfruit_mcp import cli
from nakomis_dragonfruit_mcp.app import ToolResult, mcp

_HOLLOW_KEYS = {"before", "after", "wall_mm", "voxel_mm", "timing_ms", "warnings"}
_PUNCH_KEYS = {
    "before",
    "after",
    "holes",
    "hole_checks",
    "holes_sidecar",
    "cavities_found",
    "timing_ms",
    "warnings",
}


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
    axis: str = Field(description='"-z", "+x" and so on for an axis-aligned hole, else "custom"')
    length_mm: float
    purpose: str = Field(description='"suction relief", "vent", "manual", ...')
    cavity: int | None = Field(description="Which cavity an automatic hole serves")
    extension_mm: float = Field(
        default=0.0,
        description="How far an automatic hole's start was pushed into the cavity so that its "
        "full diameter breaks into open space (a dome narrowing to an apex needs this)",
    )
    note: str = ""


class HoleCheck(BaseModel):
    """The narrowest open cross-section along a hole, measured on the drilled mesh."""

    hole: int = Field(description="Index into `holes`")
    checked: bool = Field(description="False for a hole not along an axis, which is not measured")
    hole_area_mm2: float = Field(description="The hole's nominal area, pi r^2")
    min_open_area_mm2: float = Field(
        description="Open area of the narrowest section, within a disc of 0.9 r"
    )
    min_open_fraction: float = Field(description="That area as a fraction of the 0.9 r disc")
    at_mm: float = Field(description="Where along the hole, from its start, that section is")


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
    hole_checks: list[HoleCheck] = Field(
        description="Per hole, the minimum open cross-section along its axis, from slicing the "
        "drilled mesh; a hole below 90% of its disc also adds a warning"
    )
    holes_sidecar: str = Field(
        description="`<output>.holes.json`: the holes (start point, direction, radius, length, "
        "purpose, cavity) in the output STL's own coordinates, for tools that act on them"
    )
    cavities_found: int = Field(description="Sealed cavities in the input, before drilling")
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
    wall_mm: float | None = None,
    out_path: str | Path | None = None,
    voxel_mm: float | None = None,
    options_json: str | Path | None = None,
) -> HollowResult:
    """Hollow a model, leaving a sealed cavity inside a wall (all lengths in mm).

    ALWAYS FOLLOW WITH `drill_holes`: the cavity is sealed, so an undrilled print traps
    uncured resin and suction-cups against the film. The result warns about this until
    you have drilled.

    Uses DragonFruit's voxel hollowing with the desktop app's defaults: 2.0 mm wall and
    0.65 mm voxels. Parts thinner than twice the wall stay solid. The output STL is
    written next to the input as `<name>.hollow.stl` unless `out_path` is given.

    `options_json` is a path to a JSON file of DragonFruit `HollowOptions` in camelCase
    (for example `{"shellThicknessMm": 2.5, "mode": "cavity"}`). Precedence: `wall_mm`
    and `voxel_mm` override the file when given; with neither the file's own values (or
    the app defaults) apply.
    """
    src = _check(Path(stl_path))
    out = _output_path(src, out_path, ".hollow")
    args = ["hollow", str(src), "-o", str(out), "--json"]
    if wall_mm is not None:
        args += ["--wall-mm", f"{wall_mm:g}"]
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
    auto_drain: bool = False,
    out_path: str | Path | None = None,
    radius_mm: float = 2.0,
    xy: tuple[float, float] | None = None,
    down_axis: str = "-z",
) -> DrillResult:
    """Punch drain holes through a hollowed model (all lengths in mm).

    Give exactly one of `holes` or `auto_drain=True`; they are mutually exclusive.
    Output is written next to the input as `<name>.drilled.stl` unless `out_path` is set.

    `holes`: a list of dicts in model coordinates, e.g.
    `{"x": 0, "y": -6, "z": 3, "radius": 2, "direction": [0, 0, -1], "length": 4}`.
    x, y, z is where the hole starts (put it inside the cavity); `radius` defaults to
    `radius_mm`; `direction` (default straight down) runs from the start towards the
    outside; `length` defaults to straight through everything on the axis. Unknown keys
    are an error.

    `auto_drain=True` places holes for a bottom-up MSLA print, where the part hangs from
    the build plate. Checklist it follows, and what you must still check:
    - The cavity end nearest the plate (the base, -Z, printed first) closes first as a
      cup opening towards the film, so peeling it pulls suction. A hole through the floor
      at that end is the essential suction relief ("suction relief" in the result).
    - A second hole at the far end, through the roof ("vent"), lets air in as liquid
      drains and lets IPA be flushed through.
    - Every separate cavity gets its own pair: compare `cavities_found` with the holes.
    - Holes are `radius_mm` (default 2, so 4 mm across; keep at least 2 to 3 mm across).
    - They go where the skin is flat and square to the hole, clear of detail with a
      margin of at least 1 mm around the footprint (so away from text and edges), by
      the shortest vertical path through the wall. Base, crown and back are preferred
      because they are rarely seen, but the tool only judges flatness: look at where
      `holes` say they exit.
    - It cannot see supports or the raft. If the base has supports, pass `xy` (the
      position across the down axis: x, y for -z) to move the holes clear of them, and
      keep them at least radius + 1 mm inside the base outline.
    - If no clear vertical path exists it falls back to a horizontal pair through the
      side wall and says so in `warnings`; rough skin gets a vertical hole with a
      warning. Read the warnings.
    `down_axis` says which way the plate is (default "-z"; also "+z", "+/-x", "+/-y").

    Each automatic hole starts inside the cavity, far enough that the cavity's width there
    contains the hole's full diameter (`extension_mm` says how far it moved; capped at 10 mm,
    with a warning if the cavity is too narrow). The drilled mesh is then sliced across every
    axis-aligned hole's axis: `hole_checks` gives the narrowest open section, and a warning
    appears if it is below 90% of the hole.

    Also writes `<output>.holes.json` beside the STL (path in `holes_sidecar`):
    `{"holes": [{x, y, z, radius_mm, direction, axis, length_mm, purpose, cavity}],
    "source_stl": <output STL>}` in the STL's own coordinates, x, y, z being where each hole
    starts and `direction` pointing outwards through the wall.

    Warns, with the count, if any sealed cavity is left.
    """
    if bool(holes) == auto_drain:
        raise cli.CliError("give either `holes` or `auto_drain=True`, not both or neither")
    if xy is not None and not auto_drain:
        raise cli.CliError("`xy` only applies with `auto_drain=True`")
    src = _check(Path(stl_path))
    out = _output_path(src, out_path, ".drilled")
    args = ["punch", str(src), "-o", str(out), "--radius-mm", f"{radius_mm:g}", "--json"]
    if auto_drain:
        args += ["--auto-drain", "--down-axis", down_axis]
        if xy is not None:
            args += ["--xy", f"{xy[0]:g}", f"{xy[1]:g}"]
    else:
        args += ["--holes", json.dumps(holes)]
    result = cli.run(cli.MCP_TOOLS, args, parse_json=True)
    data = _schema(result.data, _PUNCH_KEYS, "punch", result.stdout)
    if not Path(data["holes_sidecar"]).is_file():
        raise cli.CliError(f"dragonfruit-mcp-tools did not write {data['holes_sidecar']}")
    before, after = _summary(data["before"]), _summary(data["after"])
    return DrillResult(
        input_path=str(src),
        output_path=str(out),
        holes=[Hole(**h) for h in data["holes"]],
        hole_checks=[HoleCheck(**c) for c in data["hole_checks"]],
        holes_sidecar=data["holes_sidecar"],
        cavities_found=data["cavities_found"],
        cavities_before=before.cavities,
        cavities_after=after.cavities,
        drains=after.cavities == 0,
        volume_before_ml=before.volume_ml,
        volume_after_ml=after.volume_ml,
        after=after,
        timing_ms=data["timing_ms"],
        warnings=list(data["warnings"]),
    )

"""auto_support_and_slice: DragonFruit's auto-supports, raft and slicer, headlessly.

Upstream has no command for this: `scene slice` slices without supports, and
auto-placement only runs in the app (and its bench script). Our own
`ts/autosupport-slice.ts` joins them with the app's own modules, and our Rust
tool supplies the overhang scan the app runs as a Tauri command.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from nakomis_dragonfruit_mcp import cli
from nakomis_dragonfruit_mcp.app import ToolResult, mcp

SCRIPT = cli.REPO_ROOT / "ts" / "autosupport-slice.ts"
DEFAULT_PRINTER_PRESET = "elegoo-mars-5-ultra-ctb"

# Placement and island detection take seconds; slicing a tall part at 50 µm
# takes minutes on an old machine.
TIMEOUT_S = 45 * 60

_REQUIRED_KEYS = {
    "islands",
    "islands_by_source",
    "placed_by_type",
    "contacts",
    "raft",
    "model_triangles",
    "support_triangles",
    "plate_transform",
    "layer_frame",
    "timings_ms",
    "warnings",
}


class PlateTransform(BaseModel):
    translate_mm: list[float] = Field(
        description="Added to every STL vertex to put the model on the plate (no rotation, scale 1)"
    )
    rotation: list[float] | None = None
    scale: float = 1


class LayerFrame(BaseModel):
    """How a layer image maps to the plate frame the STLs are in."""

    source_width_px: int
    source_height_px: int
    width_px: int
    height_px: int
    build_width_mm: float
    build_depth_mm: float
    layer_height_mm: float
    mirror_x: bool = Field(description="Layer images are mirrored in X relative to the plate frame")
    mirror_y: bool


class SupportedSlice(ToolResult):
    output: str | None = Field(description="The sliced print, supports and raft included")
    printer_preset: str
    printer_name: str
    material: str
    lift_mm: float
    islands: int
    islands_by_source: dict[str, int] = Field(
        description="voxel (slice growth) and overhang (mesh normals); mesh minima are not run"
    )
    supports_by_type: dict[str, int]
    contacts: int
    roots: int
    islands_uncovered: int
    raft: str
    model_triangles: int
    support_triangles: int
    layers: int | None
    plate_transform: PlateTransform
    layer_frame: LayerFrame
    plate_stl: str | None = Field(description="The model alone, as placed for the slice")
    supported_stl: str | None = Field(description="Model, supports and raft, as sliced")
    timings_ms: dict[str, int]


@mcp.tool()
def auto_support_and_slice(
    stl_path: str,
    printer_preset: str = DEFAULT_PRINTER_PRESET,
    out_path: str | None = None,
    lift_mm: float | None = None,
    density: float | None = None,
    raft: bool = True,
    export_plate_stl: bool = False,
    export_supported_stl: bool = False,
) -> SupportedSlice:
    """Auto-support an STL with DragonFruit's own placement, add a raft, and slice it.

    The model is centred on the plate and lifted (default 7 mm, the app's own
    auto-lift) so supports fit under it. `density` scales supports per area
    (2 = twice as many). The print goes to `out_path`, or beside the STL as
    `<name>-supported.<format>`; the printer preset decides the format.
    `export_plate_stl` writes the model alone, exactly as placed for the slice,
    beside the print; `export_supported_stl` writes the model with its supports
    and raft. Upstream's overhang perimeter tracing is not deterministic, so
    the same model can come out a few supports different between runs.
    """
    stl = Path(stl_path).expanduser().resolve()
    if not stl.is_file():
        raise cli.CliError(f"{stl} not found")
    if density is not None and density <= 0:
        raise ValueError("density must be positive")
    if lift_mm is not None and lift_mm < 0:
        raise ValueError("lift_mm must not be negative")

    out = Path(out_path).expanduser().resolve() if out_path else None
    # The extra STLs go beside the print (or the STL, before the print is named).
    stem_dir = out.parent if out else stl.parent
    stem = out.stem if out else f"{stl.stem}-supported"
    args = [
        "--stl",
        str(stl),
        "--cli",
        str(cli.find_binary(cli.DRAGONFRUIT_CLI)),
        "--tools",
        str(cli.find_binary(cli.MCP_TOOLS)),
        "--printer",
        printer_preset,
        "--raft",
        "solid" if raft else "off",
    ]
    if out:
        args += ["--out", str(out)]
    if lift_mm is not None:
        args += ["--lift-mm", str(lift_mm)]
    if density is not None:
        args += ["--density", str(density)]
    if export_plate_stl:
        args += ["--plate-stl", str(stem_dir / f"{stem}-plate.stl")]
    if export_supported_stl:
        args += ["--supported-stl", str(stem_dir / f"{stem}-with-supports.stl")]

    result = cli.run_ts(args, script=SCRIPT, parse_json=True, timeout=TIMEOUT_S)
    data = result.data
    if not isinstance(data, dict) or not data.keys() >= _REQUIRED_KEYS:
        raise cli.CliError(f"unexpected autosupport-slice output: {result.stdout[:200]!r}")

    warnings = list(data["warnings"])
    slice_info = data.get("slice") or {}
    if data.get("output") and not slice_info:
        warnings.append("the slicer reported nothing about the print it wrote")
    if data["contacts"] == 0:
        warnings.append("no supports were placed: the print will have none")
    printer = data.get("printer", {})
    return SupportedSlice(
        output=data.get("output"),
        printer_preset=printer.get("preset_id", printer_preset),
        printer_name=printer.get("name", ""),
        material=printer.get("material", ""),
        lift_mm=data.get("lift_mm", lift_mm or 0),
        islands=data["islands"],
        islands_by_source=data["islands_by_source"],
        supports_by_type=data["placed_by_type"],
        contacts=data["contacts"],
        roots=data.get("roots", 0),
        islands_uncovered=data.get("islands_uncovered", 0),
        raft=data["raft"],
        model_triangles=data["model_triangles"],
        support_triangles=data["support_triangles"],
        layers=slice_info.get("layers"),
        plate_transform=PlateTransform(**data["plate_transform"]),
        layer_frame=LayerFrame(**data["layer_frame"]),
        plate_stl=data.get("plate_stl"),
        supported_stl=data.get("supported_stl"),
        timings_ms=data["timings_ms"],
        warnings=warnings,
    )

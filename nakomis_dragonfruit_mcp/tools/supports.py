"""auto_support_and_slice: DragonFruit's auto-supports, raft and slicer, headlessly.

Upstream has no command for this: `scene slice` slices without supports, and
auto-placement only runs in the app (and its bench script). Our own
`ts/autosupport-slice.ts` joins them with the app's own modules, and our Rust
tool supplies the overhang scan the app runs as a Tauri command.
"""

from __future__ import annotations

import functools
import json
import math
import tempfile
from pathlib import Path
from typing import Any

import anyio
from pydantic import BaseModel, Field

from nakomis_dragonfruit_mcp import cli, goo_preview
from nakomis_dragonfruit_mcp import stl as stl_io
from nakomis_dragonfruit_mcp.app import ToolResult, mcp
from nakomis_dragonfruit_mcp.printers import SliceRun
from nakomis_dragonfruit_mcp.tools import slicing

SCRIPT = cli.REPO_ROOT / "ts" / "autosupport-slice.ts"

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


# How much newer the STL may be than its holes sidecar before the pair looks mismatched.
_STALE_SIDECAR_S = 5.0

# What a hole needs to be a keep-out zone (the `<stl>.holes.json` records `drill_holes` writes).
_HOLE_NUMBERS = ("x", "y", "z", "radius_mm", "length_mm")


# What the sidecar and result need from `dragonfruit-cli slice run --json`.
_SLICE_KEYS = {"layers", "layer_height_mm", "format", "resolution_px"}


class PlateTransform(BaseModel):
    translate_mm: list[float] = Field(
        description="Added to every STL vertex to put the model on the plate (no rotation, scale 1)"
    )
    rotation: list[float] | None = None
    scale: float = 1


class LayerFrame(BaseModel):
    """How a layer image maps to the plate frame the STLs are in.

    The image spans the build area (`build_width_mm` x `build_depth_mm`) centred
    on the plate origin; columns run from -X, rows run down from +Y. With
    `mirror_x` (or `mirror_y`) the printer's images are flipped on that axis.
    """

    source_width_px: int
    source_height_px: int
    width_px: int
    height_px: int
    build_width_mm: float
    build_depth_mm: float
    layer_height_mm: float
    mirror_x: bool = Field(description="Layer images are mirrored in X relative to the plate frame")
    mirror_y: bool


class KeepOutHole(BaseModel):
    """What one keep-out zone did to the supports."""

    hole: int = Field(description="Index in the holes list that was used")
    purpose: str | None = None
    start_plate_mm: list[float] = Field(description="The hole's start point in the plate frame")
    exit_plate_mm: list[float] = Field(
        description="Where the bore leaves the skin (start + direction x length_mm), plate frame"
    )
    t_range_mm: list[float] = Field(
        description="The zone's extent along the bore, mm from the start point: [from, to]"
    )
    direction: list[float]
    keep_out_radius_mm: float = Field(description="Hole radius + 1 mm")
    blocked_triangles: int = Field(
        description="Model triangles in the zone, refused as contacts (support blockers)"
    )
    islands_inside: list[str] = Field(
        description="External islands whose own contact point lies in the zone"
    )
    islands_inside_internal: list[str] = Field(
        default_factory=list, description="Internal (cavity) islands with their contact in the zone"
    )
    contacts_removed: int = Field(
        description="Supports removed after placement for a contact in the zone"
    )
    supports_removed: int = Field(
        description="Supports removed after placement for a contact or shaft entering the zone"
    )
    lost_coverage: list[str] = Field(
        description="Islands left with no support near them by the removals"
    )


class KeepOut(BaseModel):
    source: str = Field(description="The holes file or `holes` argument these came from")
    holes: list[KeepOutHole]
    entities_before: int = Field(default=0, description="Support entities before any removal")
    entities_after: int = Field(default=0, description="Support entities in the print")
    entities_removed_total: int = Field(
        default=0, description="Everything removed, dependants and dangling shafts included"
    )
    dangling_removed: int = Field(
        default=0,
        description="Shafts removed because nothing hung off them and they had no contact",
    )
    residual_in_zone: int = Field(
        default=0, description="Supports still entering a zone after removal (should be 0)"
    )


class SupportedSlice(ToolResult):
    output_path: str = Field(description="The sliced print, supports and raft included")
    format: str
    printer: str
    printer_chosen_by: str
    material: str
    layers: int
    layer_height_mm: float
    lift_mm: float
    islands: int
    islands_by_source: dict[str, int] = Field(
        description="voxel (slice growth) and overhang (mesh normals); mesh minima are not run"
    )
    supports_by_type: dict[str, int]
    contacts: int
    roots: int
    islands_covered: int
    islands_uncovered: int = Field(description="Islands with no support near them; see warnings")
    area_coverage: float = Field(
        description="Covered island area / total island area (can exceed 1: overlapping regions)"
    )
    raft: str
    height_mm: float = Field(description="Top of the print above the plate, lift and raft included")
    build_height_mm: float
    model_triangles: int
    support_triangles: int
    profile: dict[str, Any] = Field(description="The DragonFruit profile the slice used")
    sidecar_path: str = Field(description="`<print file>.ndfm.json`: how this file was made")
    plate_offset_mm: list[float] = Field(
        description="Translation applied to the STL: XY bounding-box centre to the plate centre "
        "(0, 0), lowest point to z = lift_mm"
    )
    plate_transform: PlateTransform
    layer_frame: LayerFrame
    plate_stl_path: str | None = Field(
        default=None, description="With export_plate_stl: the model as sliced, without supports"
    )
    plate_bbox_mm: dict[str, list[float]] | None = None
    supported_stl_path: str | None = Field(
        default=None, description="With export_supported_stl: model, supports and raft as sliced"
    )
    overwritten: list[str] = Field(
        description="Output files that already existed and were replaced"
    )
    extra_files: list[str] = Field(
        default_factory=list, description="Written by the printer driver"
    )
    islands_detected: int = Field(
        default=0,
        description="Islands found, before internal ones were skipped (`islands` is those placed)",
    )
    islands_internal: int = Field(default=0, description="Islands facing into a sealed cavity")
    islands_internal_skipped: int = Field(
        default=0,
        description="Of those, how many were left unsupported (support_internal_islands=False)",
    )
    keep_out: KeepOut | None = Field(
        default=None, description="Present when holes were kept clear of supports"
    )
    previews_written: bool = Field(
        default=False,
        description="The print file's preview pictures show the model with its supports "
        "and raft (.goo only)",
    )
    timings_ms: dict[str, int]


def _holes_sidecar(stl_path: str, stl: Path) -> Path | None:
    """`<stl>.holes.json` beside the STL as named, else beside where a symlink points."""
    named = Path(stl_path).expanduser()
    for candidate in (named, stl):
        sidecar = candidate.with_name(candidate.name + ".holes.json")
        if sidecar.is_file():
            return sidecar
    return None


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _check_hole(i: int, hole: Any, where: str) -> None:
    direction = hole.get("direction") if isinstance(hole, dict) else None
    ok = (
        isinstance(hole, dict)
        and all(_number(hole.get(k)) for k in _HOLE_NUMBERS)
        and hole["radius_mm"] > 0
        and hole["length_mm"] >= 0
        and isinstance(direction, (list, tuple))
        and len(direction) == 3
        and all(_number(v) for v in direction)
        and any(direction)
    )
    if not ok:
        raise cli.CliError(
            f"{where}: hole {i} needs finite numbers x, y, z, radius_mm (> 0), length_mm (>= 0, "
            "from the start point to where the bore leaves the skin) and a non-zero direction "
            "[dx, dy, dz], in the STL's frame"
        )


def _load_holes(
    stl_path: str, stl: Path, holes: list[dict[str, Any]] | None, keep_out_holes: bool
) -> tuple[list[dict[str, Any]], str | None, list[str]]:
    """The holes to keep supports away from (STL frame), where they came from, and warnings.

    `length_mm` of each hole runs from its start point to where the bore leaves
    the skin: the start may lie well inside a cavity.
    """
    if not keep_out_holes:
        return [], None, []
    warnings: list[str] = []
    if holes is not None:
        source = "the holes argument"
    else:
        sidecar = _holes_sidecar(stl_path, stl)
        if sidecar is None:
            return [], None, []
        source = str(sidecar)
        try:
            data = json.loads(sidecar.read_text())
        except (OSError, ValueError) as e:
            raise cli.CliError(f"{sidecar} is not readable JSON: {e}") from e
        holes = data.get("holes") if isinstance(data, dict) else None
        if not isinstance(holes, list):
            raise cli.CliError(f"{sidecar}: expected an object with a 'holes' list")
        recorded = data.get("source_stl")
        if isinstance(recorded, str) and Path(recorded).name != stl.name:
            warnings.append(
                f"{sidecar.name} was written for {Path(recorded).name}, not {stl.name}: "
                "check that its holes belong to this STL"
            )
        # drill_holes writes the STL, then the sidecar: a second or two apart is not staleness.
        if stl.stat().st_mtime > sidecar.stat().st_mtime + _STALE_SIDECAR_S:
            warnings.append(
                f"{stl.name} is newer than {sidecar.name}: the holes may not match this STL"
            )
    for i, hole in enumerate(holes):
        _check_hole(i, hole, source)
    return holes, source, warnings


@mcp.tool()
async def auto_support_and_slice(
    stl_path: str,
    printer: str | None = None,
    format: str | None = None,  # noqa: A002
    material: str | None = None,
    layer_height: float | None = None,
    aa_preset: str | None = None,
    out_path: str | None = None,
    lift_mm: float | None = None,
    density: float | None = None,
    raft: bool = True,
    export_plate_stl: bool = False,
    export_supported_stl: bool = False,
    fast_islands: bool = False,
    overhangs: bool = True,
    overhang_angle_deg: float | None = None,
    keep_out_holes: bool = True,
    holes: list[dict[str, Any]] | None = None,
    support_internal_islands: bool = False,
    options: dict[str, Any] | None = None,
    previews: bool = True,
) -> SupportedSlice:
    """Auto-support an STL with DragonFruit's own placement, add a raft, and slice it.

    Slow: expect a minute or two for a 70 mm part on this machine, and up to the
    45-minute timeout when the machine is busy or the part is large.

    Printer, format, material, layer_height, aa_preset and options work as in
    `slice`: `printer` is a name from `list_printers` (default `$NDFM_PRINTER`,
    then config.toml, then `mars5ultra`, which writes `.goo`); `material` is a
    path to a DragonFruit material profile JSON.

    What it does: centres the model on the plate and lifts it (default 7 mm, the
    app's own auto-lift) so supports fit under it, as it stands: there is no
    reorientation or tilt, so supports will touch visible faces that point
    down or sideways (a flat cut face, raised lettering). It finds islands with
    two of the app's three detectors (voxel slice growth at the Islands panel's
    resolution, and mesh-normal overhangs); the third, mesh minima, is not run,
    so isolated low points can be missed. `fast_islands` uses a coarser voxel
    scan (quicker, finds fewer small islands). `density` scales supports per
    area (2 = twice as many). `raft` adds the app's default solid raft.

    Overhangs: a down-facing surface counts as overhang when it is flatter than
    `overhang_angle_deg` from horizontal (the app's self-support angle: default
    45, range 20-75; lower gives fewer overhang supports). `overhangs=False`
    skips the overhang family entirely, leaving only voxel islands: for
    self-supporting shapes such as gyroid lattices, whose saddles read as
    overhangs and would otherwise collect supports all over (NDFM-21). It also
    drops a lifted model's base supports (the voxel family doesn't see the lowest
    layer as an island), so pair it with `lift_mm=0`; a lifted call warns. On a
    gyroid lattice even 20 degrees still flags the saddles (775 contacts vs ~940).
    `islands_by_source` shows what each family found.

    Holes: a drilled STL has a `<stl>.holes.json` beside it (written by
    `drill_holes`); when that exists, supports keep clear of every hole it lists.
    Each hole is a start point `x, y, z`, a `direction` pointing out through the
    wall, a `radius_mm`, and a `length_mm` that runs from the start point to
    where the bore leaves the skin (the start may lie well inside a cavity). The
    keep-out zone is a cylinder of radius + 1 mm round the bore, from 4 mm back
    from that exit to 3 mm past it: no contact lands inside, and any support whose
    shaft still enters it is removed, with whatever hung off it. `holes`
    replaces the file with your own list of the same records, in the STL's own
    frame (mm). `keep_out_holes=False` ignores both. `keep_out` in the result
    says, per hole, what was blocked and removed, and the entity counts before
    and after; islands that cannot be supported because of a zone are named in
    `warnings`, tagged internal or external.

    Islands whose contact faces into a sealed cavity are skipped by default
    (hollow interiors are normally left bare, and the only way to reach them is
    a trunk routed up through the drain hole); `support_internal_islands=True`
    supports them anyway. `islands_internal_skipped` counts them.

    Outputs: the print at `out_path`, or beside the STL as
    `<name>-<printer>-supported<ext>`. The printer profile decides the format;
    an `out_path` with another extension is refused. Parent folders are
    created, and existing files are overwritten (listed in `overwritten`).
    `<print>.ndfm.json` records how it was made (inspect_print and
    preview_layer read the mirroring from it). `export_plate_stl` writes the
    model alone, exactly as placed for the slice, as `<print>.plate.stl` (plate
    frame: X/Y origin at the plate centre, Z up from the plate, the same frame
    as `slice`'s); `export_supported_stl` writes model, supports and raft as
    `<print>.supported.stl`. `layer_frame` maps a layer image onto that frame:
    the image spans the build area centred on the origin, rows run down from
    +Y, and `mirror_x`/`mirror_y` say whether the printer's images are mirrored
    (the Mars 5 Ultra mirrors X).

    Previews: by default the print file's preview pictures (shown on the
    printer's screen and by print servers) are rendered from the model, in
    magenta, with its supports and raft, in blue, as Chitubox's are (.goo
    only). `previews=False` leaves the slicer's placeholder. A render that
    fails is a warning, not a failed slice; `previews_written` says whether it
    happened.

    Read `warnings`: they name islands left without support, supports culled as
    orphans, and anything else that makes the print riskier. Contact counts
    vary by about 7% between runs of the same model (upstream's overhang
    tracing is order-dependent). Not yet validated on a real printer: check
    the supports before printing.
    """
    run = functools.partial(
        run_auto_support_and_slice,
        stl_path,
        printer=printer,
        format=format,
        material=material,
        layer_height=layer_height,
        aa_preset=aa_preset,
        out_path=out_path,
        lift_mm=lift_mm,
        density=density,
        raft=raft,
        export_plate_stl=export_plate_stl,
        export_supported_stl=export_supported_stl,
        fast_islands=fast_islands,
        overhangs=overhangs,
        overhang_angle_deg=overhang_angle_deg,
        keep_out_holes=keep_out_holes,
        holes=holes,
        support_internal_islands=support_internal_islands,
        options=options,
        previews=previews,
    )
    # Minutes of work: keep the event loop free meanwhile.
    return await anyio.to_thread.run_sync(run)


def run_auto_support_and_slice(
    stl_path: str,
    *,
    printer: str | None = None,
    format: str | None = None,  # noqa: A002
    material: str | None = None,
    layer_height: float | None = None,
    aa_preset: str | None = None,
    out_path: str | None = None,
    lift_mm: float | None = None,
    density: float | None = None,
    raft: bool = True,
    export_plate_stl: bool = False,
    export_supported_stl: bool = False,
    fast_islands: bool = False,
    overhangs: bool = True,
    overhang_angle_deg: float | None = None,
    keep_out_holes: bool = True,
    holes: list[dict[str, Any]] | None = None,
    support_internal_islands: bool = False,
    options: dict[str, Any] | None = None,
    previews: bool = True,
) -> SupportedSlice:
    """The blocking body of `auto_support_and_slice`."""
    stl = Path(stl_path).expanduser().resolve()
    if not stl.is_file():
        raise cli.CliError(f"STL not found: {stl}")
    if density is not None and density <= 0:
        raise ValueError("density must be positive")
    if lift_mm is not None and lift_mm < 0:
        raise ValueError("lift_mm must not be negative")
    if layer_height is not None and layer_height <= 0:
        raise ValueError("layer_height must be positive")
    if overhang_angle_deg is not None and not 20 <= overhang_angle_deg <= 75:
        # The app's own range (AUTO_SUPPORT_CONSTRAINTS); it would clamp silently.
        raise ValueError("overhang_angle_deg must be between 20 and 75 (the app's range)")
    if aa_preset is not None and aa_preset not in slicing.AA_PRESETS:
        raise ValueError(f"aa_preset must be one of {', '.join(slicing.AA_PRESETS)}")
    material_path = None
    if material is not None:
        material_path = Path(material).expanduser().resolve()
        if not material_path.is_file():
            raise cli.CliError(f"material profile not found: {material_path}")
    if format is not None:
        format = format.lower()
        if not format.startswith("."):
            format = "." + format

    zones, zone_source, hole_warnings = _load_holes(stl_path, stl, holes, keep_out_holes)
    if not overhangs and (lift_mm is None or lift_mm > 0):
        # Measured on the gyroid lantern: its flat base was found only by the
        # overhang family, so with it off a lifted model got no supports at all.
        hole_warnings.append(
            "overhangs=False with a lift: the voxel family doesn't treat the model's "
            "lowest layer as an island, so a flat base may get no supports. Use it with "
            "lift_mm=0 (the model on the plate), or check the contacts"
        )

    plan = slicing.plan_slice(
        stl,
        printer=printer,
        format=format,
        layer_height=layer_height,
        material_path=material_path,
        aa_preset=aa_preset,
        out_path=out_path,
        options=options,
        name_tag="-supported",
    )
    chosen, job = plan.printer, plan.job
    if job.out_path.suffix.lower() != plan.suffix:
        # Unlike `slice`, refused: the TS side would refuse it after minutes of work.
        raise cli.CliError(
            f"out_path ends {job.out_path.suffix!r} but printer {chosen.name!r} writes "
            f"{plan.suffix!r}; use a {plan.suffix} path"
        )
    warnings = [w for w in plan.warnings if not w.startswith("out_path ends")] + hole_warnings
    extra = slicing.call_hook(chosen, "extra_slice_args", chosen.extra_slice_args, job)
    if extra:
        warnings.append(
            f"printer {chosen.name!r} asks for extra `scene slice` arguments {extra}, which "
            "auto_support_and_slice does not use"
        )

    job.out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ndfm-py-autosupport-") as tmp:
        profile_file = Path(tmp) / "printer.json"
        profile_file.write_text(json.dumps(plan.profile))
        args = [
            "--stl", str(job.stl_path),
            "--out", str(job.out_path),
            "--cli", str(cli.find_binary(cli.DRAGONFRUIT_CLI)),
            "--tools", str(cli.find_binary(cli.MCP_TOOLS)),
            "--printer-json", str(profile_file),
            "--raft", "solid" if raft else "off",
        ]  # fmt: skip
        if job.material:
            args += ["--material", str(job.material)]
        if job.layer_height is not None:
            args += ["--layer-height", f"{job.layer_height:g}"]
        if job.aa_preset:
            args += ["--aa-preset", job.aa_preset]
        if lift_mm is not None:
            args += ["--lift-mm", f"{lift_mm:g}"]
        if density is not None:
            args += ["--density", f"{density:g}"]
        if fast_islands:
            args.append("--coarse-islands")
        if not overhangs:
            args.append("--no-overhangs")
        if overhang_angle_deg is not None:
            settings = {"overhangSelfSupportAngleDeg": overhang_angle_deg}
            args += ["--settings", json.dumps(settings, separators=(",", ":"))]
        if support_internal_islands:
            args.append("--support-internal-islands")
        if zones:
            # In the STL's frame: the script moves them with the model onto the plate.
            keep_out_file = Path(tmp) / "holes.json"
            keep_out_file.write_text(json.dumps({"holes": zones}))
            args += ["--keep-out", str(keep_out_file)]
        # The previews show supports and raft, so they need the supported STL:
        # beside the print when asked for, else a scratch copy (only .goo has them).
        supported_stl = None
        if export_supported_stl:
            supported_stl = job.out_path.with_name(job.out_path.name + ".supported.stl")
        elif previews and plan.suffix == ".goo":
            supported_stl = Path(tmp) / "preview.supported.stl"
        if supported_stl is not None:
            args += ["--supported-stl", str(supported_stl)]
        result = cli.run_ts(args, script=SCRIPT, parse_json=True, timeout=TIMEOUT_S)

        data = result.data
        if not isinstance(data, dict) or not data.keys() >= _REQUIRED_KEYS:
            raise cli.CliError(f"unexpected autosupport-slice output: {result.stdout[:200]!r}")
        slice_info = data.get("slice")
        if not isinstance(slice_info, dict) or not slice_info.keys() >= _SLICE_KEYS:
            raise cli.CliError(f"the slicer reported nothing usable: {str(slice_info)[:200]!r}")
        out_file = Path(data["output"])
        if not out_file.is_file():
            raise cli.CliError(f"the slicer reported success but {out_file} does not exist")

        warnings += data["warnings"]
        if data["contacts"] == 0:
            warnings.append("no supports were placed: the print will have none")
        run = SliceRun(profile=plan.profile, cli_args=args, result=slice_info)
        final = slicing.call_hook(chosen, "postprocess", chosen.postprocess, out_file, job, run)
        # After postprocess, which may rewrite the file.
        previews_written = False
        if previews:
            if supported_stl is not None and supported_stl.is_file():
                previews_written = goo_preview.add_previews(
                    final, supported_stl, warnings, model_triangles=data["model_triangles"]
                )
            elif goo_preview.has_preview_slots(final):
                warnings.append(
                    "the slice succeeded but its preview pictures could not be written "
                    "(the printer shows a placeholder): no supported STL was written"
                )

        offset = [float(v) + 0.0 for v in data["plate_transform"]["translate_mm"]]
        bbox = data.get("model_bbox_mm") or {}
        plate_bbox = {"min": bbox.get("min", []), "max": bbox.get("max", [])}
        plate_path = None
        if export_plate_stl:
            # The same writer and frame as `slice`'s plate STL: the STL moved by the offset.
            plate = final.with_name(final.name + ".plate.stl")
            try:
                stl_io.write_translated(job.stl_path, plate, (offset[0], offset[1], offset[2]))
            except (OSError, stl_io.StlError) as e:
                warnings.append(f"the slice succeeded but the plate STL could not be written: {e}")
            else:
                plate_path = str(plate)
        supports_info = {
            "lift_mm": data.get("lift_mm"),
            "raft": data["raft"],
            "islands": data["islands"],
            "islands_by_source": data["islands_by_source"],
            "supports_by_type": data["placed_by_type"],
            "contacts": data["contacts"],
            "islands_uncovered": data.get("islands_uncovered", 0),
            "supported_stl": data.get("supported_stl") if export_supported_stl else None,
        }
        sidecar = slicing.write_sidecar(
            final,
            chosen,
            plan.profile,
            slice_info,
            place_on_plate=True,
            offset=offset,
            plate_path=plate_path,
            plate_bbox=plate_bbox if plate_path else None,
            supports=supports_info,
        )
        printer_info = data.get("printer", {})
        return SupportedSlice(
            output_path=str(final),
            format=slice_info["format"],
            printer=chosen.name,
            printer_chosen_by=plan.origin,
            material=printer_info.get("material", ""),
            layers=slice_info["layers"],
            layer_height_mm=round(slice_info["layer_height_mm"], 4),
            lift_mm=data.get("lift_mm", lift_mm or 0),
            islands=data["islands"],
            islands_by_source=data["islands_by_source"],
            supports_by_type=data["placed_by_type"],
            contacts=data["contacts"],
            roots=data.get("roots", 0),
            islands_covered=data.get("islands_covered", 0),
            islands_uncovered=data.get("islands_uncovered", 0),
            area_coverage=data.get("area_coverage", 0.0),
            raft=data["raft"],
            height_mm=data.get("height_mm", 0.0),
            build_height_mm=data.get("build_height_mm", 0.0),
            model_triangles=data["model_triangles"],
            support_triangles=data["support_triangles"],
            profile=plan.profile,
            sidecar_path=str(sidecar),
            plate_offset_mm=offset,
            plate_transform=PlateTransform(**data["plate_transform"]),
            layer_frame=LayerFrame(**data["layer_frame"]),
            plate_stl_path=plate_path,
            plate_bbox_mm=plate_bbox if plate_path else None,
            supported_stl_path=data.get("supported_stl") if export_supported_stl else None,
            overwritten=data.get("overwritten", []),
            extra_files=[str(p) for p in run.extra_files],
            islands_detected=data.get("islands_detected", data["islands"]),
            islands_internal=data.get("islands_internal", 0),
            islands_internal_skipped=data.get("islands_internal_skipped", 0),
            keep_out=(
                KeepOut(**{**data["keep_out"], "source": zone_source or ""})
                if data.get("keep_out")
                else None
            ),
            timings_ms=data["timings_ms"],
            previews_written=previews_written,
            warnings=warnings,
        )

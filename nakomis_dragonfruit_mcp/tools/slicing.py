"""list_printers and slice: pick a printer plugin and slice an STL to its format."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from nakomis_dragonfruit_mcp import cli
from nakomis_dragonfruit_mcp import stl as stl_io
from nakomis_dragonfruit_mcp.app import ToolResult, mcp
from nakomis_dragonfruit_mcp.printers import SliceJob, SliceRun, loader

AA_PRESETS = ("sharp", "balanced", "smooth", "raw")

SUPPORTS_WARNING = (
    "Supports are NOT included: DragonFruit's `scene slice` does not slice supports yet. "
    "Slice only what can print without them."
)


class PrinterInfo(BaseModel):
    name: str
    description: str
    route: str = Field(description="'py' for a Python driver, 'json' for a profile alone")
    source: str = Field(description="The file it was loaded from")
    preset_id: str | None = Field(description="DragonFruit's official preset id, if it names one")
    output_format: str | None
    build_volume_mm: dict[str, float | None] | None = Field(
        description="width, depth, height; width/depth derived from the screen if omitted"
    )
    selected: bool = Field(description="True for the printer used when none is asked for")


class PluginFailure(BaseModel):
    source: str
    error: str


class PrinterList(ToolResult):
    selected: str | None = Field(description="Name of the printer used by default, and why")
    printers: list[PrinterInfo]
    failed: list[PluginFailure] = Field(description="Plugins that could not be loaded, skipped")


class SliceResult(ToolResult):
    output_path: str
    format: str
    layers: int
    layer_height_mm: float
    resolution_px: list[int]
    size_bytes: int
    printer: str
    printer_chosen_by: str
    estimated_print_time_s: float | None = Field(
        description="Not available: neither the slicer nor the CLI reports one yet"
    )
    estimated_resin_ml: float | None = Field(description="Not available, as above")
    supports_included: bool = False
    profile: dict[str, Any] = Field(description="The DragonFruit profile the slice used")
    cli_args: list[str] = Field(description="Arguments given to `dragonfruit-ts-cli scene slice`")
    anti_aliasing: dict[str, Any] | None
    slice_seconds: float | None
    extra_files: list[str] = Field(
        default_factory=list, description="Written by the printer driver"
    )
    plate_offset_mm: list[float] = Field(
        description="Translation applied to the STL to place it: centre of its XY bounding box "
        "on the plate centre (0, 0) and its lowest point on the plate (z = 0)"
    )
    plate_stl_path: str | None = Field(
        default=None, description="With export_plate_stl: the model as sliced, without supports"
    )
    plate_bbox_mm: dict[str, list[float]] | None = Field(
        default=None, description="min and max corners of the plate STL, in plate coordinates"
    )


@mcp.tool()
def list_printers() -> PrinterList:
    """List the available printers, and any printer plugins that failed to load.

    A printer is a DragonFruit profile (an official preset, a custom profile or
    an app-exported bundle) in a `.json` file, optionally wrapped by a `.py`
    driver. Drop-ins are read from `$NDFM_PRINTERS_DIR` and
    `~/.config/nakomis-dragonfruit-mcp/printers/`; `.py` files there run with
    the server's privileges. The default printer comes from `$NDFM_PRINTER`,
    then `printer = "..."` in `~/.config/nakomis-dragonfruit-mcp/config.toml`,
    then `mars5ultra`.
    """
    registry = loader.discover()
    warnings = list(registry.warnings)
    try:
        name, origin = loader.choose_name(None)
    except ValueError as e:
        name, origin = loader.DEFAULT_PRINTER, "default"
        warnings.append(str(e))
    if name not in registry.printers:
        warnings.append(f"the default printer {name!r} (from {origin}) is not available")
    infos = [
        PrinterInfo(
            name=p.name,
            description=p.description,
            route=p.route,
            source=str(p.source),
            preset_id=_preset_id(p),
            output_format=p.output_format(),
            build_volume_mm=p.build_volume_mm(),
            selected=p.name == name,
        )
        for p in sorted(registry.printers.values(), key=lambda p: p.name)
    ]
    for p in registry.printers.values():
        warnings.extend(p.warnings())
    return PrinterList(
        selected=f"{name} (from {origin})",
        printers=infos,
        failed=[PluginFailure(source=f.source, error=f.error) for f in registry.failures],
        warnings=warnings,
    )


def _preset_id(printer) -> str | None:
    profile = printer.base_profile()
    section = profile["printer"] if isinstance(profile.get("printer"), dict) else profile
    return section.get("presetId")


@mcp.tool()
def slice(  # noqa: A001  (the tool's name)
    stl_path: str,
    printer: str | None = None,
    format: str | None = None,  # noqa: A002
    layer_height: float | None = None,
    material: str | None = None,
    aa_preset: str | None = None,
    out_path: str | None = None,
    options: dict[str, Any] | None = None,
    export_plate_stl: bool = False,
) -> SliceResult:
    """Slice an STL to the printer's own print file, as DragonFruit's app would.

    Supports are NOT included yet: the slice is of the bare model, placed as the
    scene puts it, so it must be printable without them.

    Args:
        stl_path: The model, as a binary STL.
        printer: A name from `list_printers`. Default: `$NDFM_PRINTER`, then
            config.toml, then `mars5ultra`.
        format: Output format extension, such as `.goo` or `.ctb`, instead of
            the printer's own. Derives a custom profile from the printer's;
            only use a format the printer firmware reads.
        layer_height: In mm. Default: the material's (0.05 for the default material).
        material: Path to a DragonFruit material profile JSON. Default: the
            app's default material for the printer.
        aa_preset: Anti-aliasing: sharp, balanced (default), smooth or raw.
        out_path: Where to write the print file. Default: next to the STL as
            `<name>-<printer><ext>`.
        options: Free-form options for the printer's own driver; ignored by
            printers that take none.
        export_plate_stl: Also write the model exactly as it sits on the build
            plate for this slice (binary STL, mm, Z up, origin at the plate
            centre, no supports), as `<print file>.plate.stl`. It lines up with
            the sliced layers. The offset applied and the bounding box are reported.

    The model is placed before slicing: the centre of its XY bounding box on the
    plate centre and its lowest point at z = 0.
    """
    stl = Path(stl_path).expanduser().resolve()
    if not stl.is_file():
        raise cli.CliError(f"STL not found: {stl}")
    if aa_preset is not None and aa_preset not in AA_PRESETS:
        raise ValueError(f"aa_preset must be one of {', '.join(AA_PRESETS)}")
    if layer_height is not None and layer_height <= 0:
        raise ValueError("layer_height must be positive")
    material_path = None
    if material is not None:
        material_path = Path(material).expanduser().resolve()
        if not material_path.is_file():
            raise cli.CliError(f"material profile not found: {material_path}")
    if format is not None:
        format = format.lower()
        if not format.startswith("."):
            format = "." + format

    if not cli.ts_cli_rust_binary().exists():
        raise cli.CliError(
            f"{cli.ts_cli_rust_binary()} is missing: dragonfruit-ts-cli looks for dragonfruit-cli "
            "there. Run scripts/build.sh, which links it to bin/dragonfruit-cli."
        )
    registry = loader.discover()
    chosen, origin = loader.get(registry, printer)
    warnings = list(registry.warnings) + chosen.warnings()

    suffix = format or chosen.output_format() or ".bin"
    out = (
        Path(out_path).expanduser().resolve()
        if out_path
        else stl.with_name(f"{stl.stem}-{chosen.name}{suffix}")
    )
    job = chosen.prepare(
        SliceJob(
            stl_path=stl,
            out_path=out,
            format=format,
            layer_height=layer_height,
            material=material_path,
            aa_preset=aa_preset,
            options=dict(options or {}),
        )
    )
    profile = chosen.effective_profile(job.format)
    if job.out_path.suffix.lower() != suffix:
        warnings.append(
            f"out_path ends {job.out_path.suffix!r} but the printer writes {suffix!r}; "
            "the file is written in the printer's format regardless"
        )
    try:
        lo, hi = stl_io.bbox(job.stl_path)
    except stl_io.StlError as e:
        raise cli.CliError(str(e)) from e
    offset = [-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]]
    job.out_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="ndfm-slice-") as tmp:
        work = Path(tmp)
        profile_file = work / "printer.json"
        profile_file.write_text(json.dumps(profile))
        scene = work / "scene.voxl"
        cli.run_ts(["scene", "create", "--o", str(scene)])
        model_id = _add_model(scene, job.stl_path, stl.stem)
        cli.run_ts(
            ["scene", "transform-model", str(scene), "--id", model_id]
            + ["--position", ",".join(repr(v) for v in offset)]
        )
        args = [
            "scene", "slice", str(scene),
            "--o", str(job.out_path),
            "--mesh-dir", str(job.stl_path.parent),
            "--printer", str(profile_file),
            "--json",
        ]  # fmt: skip
        if job.material:
            args += ["--material", str(job.material)]
        if job.layer_height is not None:
            args += ["--layer-height", f"{job.layer_height:g}"]
        if job.aa_preset:
            args += ["--aa-preset", job.aa_preset]
        args += chosen.extra_slice_args(job)
        result = cli.run_ts(args, parse_json=True)

    data = _check_slice_output(result)
    out_file = Path(data["output"])
    if not out_file.is_file():
        raise cli.CliError(f"the slicer reported success but {out_file} does not exist")
    run = SliceRun(profile=profile, cli_args=args, result=data)
    final = chosen.postprocess(out_file, job, run)
    warnings.append(SUPPORTS_WARNING)
    plate_path = plate_bbox = None
    if export_plate_stl:
        plate = final.with_name(final.name + ".plate.stl")
        stl_io.write_translated(job.stl_path, plate, (offset[0], offset[1], offset[2]))
        plate_path = str(plate)
        plate_bbox = {
            "min": [lo[i] + offset[i] for i in range(3)],
            "max": [hi[i] + offset[i] for i in range(3)],
        }
    return SliceResult(
        output_path=str(final),
        format=data["format"],
        layers=data["layers"],
        layer_height_mm=round(data["layer_height_mm"], 4),
        resolution_px=data["resolution_px"],
        size_bytes=final.stat().st_size,
        printer=chosen.name,
        printer_chosen_by=origin,
        estimated_print_time_s=None,
        estimated_resin_ml=None,
        profile=profile,
        cli_args=args,
        anti_aliasing=data.get("anti_aliasing"),
        slice_seconds=data.get("wall_s"),
        extra_files=[str(p) for p in run.extra_files],
        plate_offset_mm=offset,
        plate_stl_path=plate_path,
        plate_bbox_mm=plate_bbox,
        warnings=warnings,
    )


def _check_slice_output(result: cli.CliResult) -> dict[str, Any]:
    """`scene slice --json` output, checked: upstream's schema moves."""
    data = result.data
    needed = {"output", "format", "layers", "layer_height_mm", "resolution_px"}
    if not isinstance(data, dict) or not needed <= data.keys():
        raise cli.CliError(f"unexpected `scene slice --json` output: {result.stdout[:200]!r}")
    return data


def _add_model(scene: Path, stl: Path, name: str) -> str:
    """Add the STL to the scene and return its model id."""
    cli.run_ts(["scene", "add-model", str(scene), "--mesh", str(stl), "--name", name])
    models = cli.run_ts(["scene", "list-models", str(scene), "--json"], parse_json=True).data
    try:
        return models["models"][0]["id"]
    except (KeyError, IndexError, TypeError) as e:
        raise cli.CliError("unexpected `scene list-models --json` output") from e

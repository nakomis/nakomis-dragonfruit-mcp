"""inspect_print and preview_layer: look inside a sliced print file.

`dragonfruit-cli` reads ZIP-based archives (`.nanodlp`, ...) but not `.goo`
(read here, see `goo.py`) and not `.ctb`. The CTB that DragonFruit writes for
the Mars 5 Ultra (v5enc) has an AES-encrypted header and XOR-scrambled layers;
reading it is feasible (the key is in the DragonFruit encoder, plus the layer
table and RLE) but needs an AES implementation, so it is not done: for a `.ctb`
these tools say so. Slice to `.goo` or `.nanodlp` to preview.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import io
import json
import struct
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import anyio
from mcp.server.fastmcp.utilities.types import Image
from PIL import Image as PilImage
from pydantic import Field

from nakomis_dragonfruit_mcp import cli, goo
from nakomis_dragonfruit_mcp.app import ToolResult, mcp
from nakomis_dragonfruit_mcp.printers import presets

CTB_ENCRYPTED = 0x12FD0107
CTB_MAGICS = {0x12FD0086, 0x12FD0106, CTB_ENCRYPTED}
DEFAULT_IMAGE_PX = 800
MIN_IMAGE_PX, MAX_IMAGE_PX = 64, 1568


class PrintInfo(ToolResult):
    path: str
    format: str = Field(description="Extension of the file: .goo, .nanodlp, ...")
    container: str = Field(description="zip, goo, goo5 or ctb: how it was read")
    size_bytes: int
    layers: int | None = None
    layer_height_mm: float | None = None
    resolution_px: list[int] | None = Field(None, description="The screen: width, height")
    exposure_s: float | None = None
    bottom_exposure_s: float | None = None
    bottom_layers: int | None = None
    estimated_print_time_s: float | None = None
    estimated_resin_ml: float | None = None
    mirror_x: bool | None = Field(
        None, description="Whether the stored image is mirrored left-right; None if unknown"
    )
    mirror_source: str | None = Field(
        None, description="Where mirror_x came from: sidecar, slicer.json, goo-header, preset"
    )
    machine: str | None = None
    details: dict[str, Any] = Field(default_factory=dict, description="Anything else it reports")


class LayerPreview(ToolResult):
    png_path: str = Field(description="Full-size PNG of the layer, one byte per screen pixel")
    layer: int
    layers: int
    width: int
    height: int
    flipped_x: bool = Field(description="Flipped left-right to read as the plate does")
    mirror_source: str | None = Field(description="Where the flip decision came from")
    image_px: list[int] = Field(description="Size of the downscaled image returned too")


def _kind(path: Path) -> str:
    if not path.is_file():
        raise cli.CliError(f"print file not found: {path}")
    with path.open("rb") as f:
        head = f.read(12)
    if head[:2] == b"PK":
        return "zip"
    if goo.is_goo(head):
        return "goo"
    if goo.is_goo_v5(head):
        return "goo5"
    if len(head) >= 4 and struct.unpack("<I", head[:4])[0] in CTB_MAGICS:
        return "ctb"
    raise cli.CliError(f"{path}: not a ZIP archive, GOO or CTB file")


def _unsupported(path: Path, kind: str) -> str:
    """Why a file of this kind cannot be read here."""
    if kind == "goo5":
        return f"{path.name} is a GOO V5.1 file (little-endian, partitioned layers): not supported"
    with path.open("rb") as f:
        magic = struct.unpack("<I", f.read(4))[0]
    if magic == CTB_ENCRYPTED:
        return (
            f"{path.name} is an encrypted CTB (v5enc): its header and layers are encrypted or "
            "scrambled and cannot be read here. Slice to .goo or .nanodlp to inspect or preview "
            "layers; the slice result already has the layer count, layer height and resolution."
        )
    return f"{path.name} is a CTB: CTB reading is not implemented. Slice to .goo or .nanodlp."


def _zip_metadata(path: Path) -> dict[str, Any]:
    """DragonFruit's own `slicer.json` and per-layer `info.json`, when the archive has them."""
    out: dict[str, Any] = {}
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        for key in ("slicer.json", "info.json"):
            if key in names:
                with contextlib.suppress(json.JSONDecodeError):
                    out[key] = json.loads(z.read(key))
    return out


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _sidecar_mirror(path: Path) -> bool | None:
    """`mirrorX` from the `<print>.ndfm.json` that `slice` writes beside the file."""
    sidecar = path.with_name(path.name + ".ndfm.json")
    try:
        value = _dict(_dict(json.loads(sidecar.read_text())).get("profile")).get("mirrorX")
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, bool) else None


def _preset_mirror(software: str, machine: str) -> bool | None:
    """DragonFruit never sets a .goo's mirror flag; its printer preset says what it did."""
    if software != "DragonFruit" or not machine:
        return None
    flags = {
        _dict(p.get("display")).get("mirrorX") for p in presets.find_by_name(machine)
    }  # a name matching presets that disagree is no answer
    return flags.pop() if len(flags) == 1 and isinstance(next(iter(flags)), bool) else None


def _mirror(
    path: Path, own: bool | None, own_source: str | None, *, goo_header: goo.GooHeader | None = None
) -> tuple[bool | None, str | None]:
    """Whether the stored image is mirrored left-right, and where we learnt it.

    In order: the slice sidecar, the file's own metadata (a `.nanodlp`'s slicer.json), a `.goo`
    made by DragonFruit looked up in DragonFruit's presets by machine name, then the goo
    header's flag for other slicers' files.
    """
    side = _sidecar_mirror(path)
    if side is not None:
        return side, "sidecar"
    if own is not None:
        return own, own_source
    if goo_header is not None:
        preset = _preset_mirror(goo_header.software, goo_header.machine_name)
        if preset is not None:
            return preset, "preset"
        if goo_header.software != "DragonFruit":
            return goo_header.mirror_x, "goo-header"
    return None, None


def _common(path: Path, kind: str) -> dict[str, Any]:
    return {
        "path": str(path),
        "format": path.suffix.lower(),
        "container": kind,
        "size_bytes": path.stat().st_size,
    }


def describe(print_path: str) -> PrintInfo:
    """The blocking body of `inspect_print`."""
    path = Path(print_path).expanduser().resolve()
    kind = _kind(path)
    common = _common(path, kind)
    if kind in ("ctb", "goo5"):
        return PrintInfo(**common, warnings=[_unsupported(path, kind)])
    if kind == "goo":
        return _describe_goo(path, common)
    return _describe_zip(path, common)


def _describe_goo(path: Path, common: dict[str, Any]) -> PrintInfo:
    try:
        h = goo.read_header(path)
    except goo.GooError as e:
        raise cli.CliError(str(e)) from e
    mirror, source = _mirror(path, None, None, goo_header=h)
    warnings = []
    if mirror is None:
        warnings.append(
            "cannot tell whether this .goo's image is mirrored: DragonFruit never sets the "
            "header flag, and no sidecar or preset for this machine name was found"
        )
    return PrintInfo(
        **common,
        layers=h.layers,
        layer_height_mm=h.layer_height_mm,
        resolution_px=[h.resolution_x, h.resolution_y],
        exposure_s=h.exposure_s,
        bottom_exposure_s=h.bottom_exposure_s,
        bottom_layers=h.bottom_layers,
        estimated_print_time_s=float(h.print_time_s) or None,
        mirror_x=mirror,
        mirror_source=source,
        machine=h.machine_name or None,
        details={"goo_version": h.version, "software": h.software},
        warnings=warnings,
    )


def _describe_zip(path: Path, common: dict[str, Any]) -> PrintInfo:
    args = ["print", "inspect", str(path), "--json"]
    result = cli.run(cli.DRAGONFRUIT_CLI, args, parse_json=True)
    data = result.data
    if not isinstance(data, dict) or "numeric_layer_count" not in data:
        raise cli.CliError(f"unexpected `print inspect` output: {result.stdout[:200]!r}")
    info: dict[str, Any] = {"layers": data["numeric_layer_count"]}
    details: dict[str, Any] = {
        "entries": data.get("total_entries"),
        "uncompressed_bytes": data.get("total_uncompressed_bytes"),
    }
    warnings = []
    meta = _zip_metadata(path)
    slicer = meta.get("slicer.json")
    own_mirror = None
    if isinstance(slicer, dict):
        printer, material, eff = (
            _dict(slicer.get(k)) for k in ("printer", "material", "effective")
        )
        sx, sy = eff.get("sourceResolutionX"), eff.get("sourceResolutionY")
        if isinstance(sx, int) and isinstance(sy, int):
            info["resolution_px"] = [sx, sy]
        info.update(
            layer_height_mm=eff.get("layerHeightMm"),
            exposure_s=material.get("normalExposureSec"),
            bottom_exposure_s=material.get("bottomExposureSec"),
            bottom_layers=material.get("bottomLayerCount"),
            machine=printer.get("name"),
        )
        own_mirror = eff.get("mirrorX") if isinstance(eff.get("mirrorX"), bool) else None
        details["x_packing"] = eff.get("xPackingMode")
        details["stored_layer_px"] = [eff.get("widthPx"), eff.get("heightPx")]
    else:
        warnings.append(
            "no usable DragonFruit slicer.json in the archive: only the layer count is known"
        )
    layer_info = meta.get("info.json")
    height = info.get("layer_height_mm")
    if isinstance(layer_info, list) and isinstance(height, int | float):
        area_mm2 = sum(
            x["TotalSolidArea"]
            for x in layer_info
            if isinstance(x, dict) and isinstance(x.get("TotalSolidArea"), int | float)
        )
        info["estimated_resin_ml"] = round(area_mm2 * height / 1000, 2)
    info["mirror_x"], info["mirror_source"] = _mirror(path, own_mirror, "slicer.json")
    return PrintInfo(**common, **info, details=details, warnings=warnings)


@mcp.tool()
async def inspect_print(print_path: str) -> PrintInfo:
    """Compact metadata of a sliced print file: layers, layer height, screen, exposure, estimates.

    Reads `.goo` and ZIP-based files (`.nanodlp`, ...). A `.ctb` (encrypted v5enc
    from DragonFruit) and GOO V5.1 cannot be read (see the warning in the result).
    Estimates are the file's own when it has them: `.goo` carries a print time;
    `.nanodlp` made by DragonFruit allows a resin volume (sum of each layer's area
    x layer height), not a time.

    `mirror_x` says whether the stored image is mirrored left-right relative to the
    plate, and `mirror_source` where that came from: the `<print>.ndfm.json` sidecar
    that `slice` writes, the archive's own slicer.json, DragonFruit's printer preset
    for a DragonFruit-made `.goo` (it never sets the header flag), or the header flag
    of another slicer's file. None means it could not be told.
    """
    return await anyio.to_thread.run_sync(describe, print_path)


@mcp.tool()
async def preview_layer(
    print_path: str,
    layer: int,
    out_path: str | None = None,
    flip_x: bool | None = None,
    image_px: int = DEFAULT_IMAGE_PX,
) -> list:
    """Render one layer (1-based) of a sliced `.goo` or ZIP-based print file as a PNG.

    Writes the full-size PNG (one byte per screen pixel; packed formats such as
    the Athena's three-pixels-per-byte RGB are unpacked to the real screen) to
    `out_path`, by default in the system temp directory under `ndfm-preview/`. Also
    returns the image itself, downscaled to about `image_px` wide (clamped to
    64-1568), so you can look at it. Layer 1 is the first layer printed: the base footprint.

    DragonFruit writes the pixels already mirrored for printers whose preset has
    `mirrorX` (the Mars 5 Ultra and Athena both do), so the raw image is the
    screen's, mirrored left-right. `flip_x` flips it back so it reads as the plate
    seen from above (+X right, +Y up, the same frame as `slice`'s plate STL):
    None flips when `inspect_print` would say the file is mirrored (see its
    `mirror_source`), True or False force it. A `.ctb` cannot be previewed.
    """
    run = functools.partial(
        render, print_path, layer, out_path=out_path, flip_x=flip_x, image_px=image_px
    )
    return await anyio.to_thread.run_sync(run)


def render(
    print_path: str,
    layer: int,
    *,
    out_path: str | None = None,
    flip_x: bool | None = None,
    image_px: int = DEFAULT_IMAGE_PX,
) -> list:
    """The blocking body of `preview_layer`."""
    if isinstance(layer, bool) or not isinstance(layer, int) or layer < 1:
        raise ValueError("layer must be an integer >= 1")
    image_px = max(MIN_IMAGE_PX, min(MAX_IMAGE_PX, image_px))
    path = Path(print_path).expanduser().resolve()
    kind = _kind(path)
    if kind in ("ctb", "goo5"):
        raise cli.CliError(_unsupported(path, kind))
    warnings: list[str] = []
    if kind == "goo":
        try:
            h = goo.read_header(path)
            data = goo.read_layer(path, h, layer)
            pixels = goo.decode_layer(data, h.resolution_x, h.resolution_y)
        except goo.GooError as e:
            raise cli.CliError(str(e)) from e
        image = PilImage.frombytes("L", (h.resolution_x, h.resolution_y), pixels)
        layers = h.layers
        mirrored, source = _mirror(path, None, None, goo_header=h)
    else:
        info = _describe_zip(path, _common(path, kind))
        image, layers = _zip_layer(path, info, layer, warnings)
        mirrored, source = info.mirror_x, info.mirror_source
    if flip_x is None:
        flipped = bool(mirrored)
        if mirrored is None:
            warnings.append(
                "cannot tell whether this image is mirrored (no sidecar, slicer.json or preset "
                "for it): shown as stored; pass flip_x=True to flip it left-right"
            )
    else:
        flipped, source = flip_x, "argument"
    if flipped:
        image = image.transpose(PilImage.Transpose.FLIP_LEFT_RIGHT)

    out = Path(out_path).expanduser().resolve() if out_path else _default_png(path, layer)
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out, "PNG")
    scale = -(-image.width // image_px)
    small = image.resize(
        (-(-image.width // scale), -(-image.height // scale)), PilImage.Resampling.BOX
    )
    buffer = io.BytesIO()
    small.save(buffer, "PNG")
    result = LayerPreview(
        png_path=str(out),
        layer=layer,
        layers=layers,
        width=image.width,
        height=image.height,
        flipped_x=flipped,
        mirror_source=source,
        image_px=list(small.size),
        warnings=warnings,
    )
    return [result.model_dump_json(), Image(data=buffer.getvalue(), format="png")]


def _default_png(path: Path, layer: int) -> Path:
    tag = hashlib.sha1(str(path).encode()).hexdigest()[:8]
    return Path(tempfile.gettempdir()) / "ndfm-preview" / f"{path.stem}-{tag}-layer-{layer}.png"


def _zip_layer(
    path: Path, info: PrintInfo, layer: int, warnings: list[str]
) -> tuple[PilImage.Image, int]:
    layers = info.layers or 0
    if not 1 <= layer <= layers:
        raise cli.CliError(f"layer {layer} out of range: the file has {layers} layers")
    with tempfile.TemporaryDirectory(prefix="ndfm-preview-") as tmp:
        png = Path(tmp) / "layer.png"
        args = ["slice", "preview-layer", str(path), "--layer", str(layer), "-o", str(png)]
        cli.run(cli.DRAGONFRUIT_CLI, args)
        image = PilImage.open(png)
        image.load()
    packing = info.details.get("x_packing")
    if packing == "rgb8_div3" and image.mode == "RGB":
        # Each pixel's R, G, B are three neighbouring screen pixels.
        image = PilImage.frombytes("L", (image.width * 3, image.height), image.tobytes())
    elif image.mode != "L":
        warnings.append(f"layer image is {image.mode} (packing {packing!r}): converted to grey")
        image = image.convert("L")
    return image, layers

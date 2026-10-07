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
import io
import json
import struct
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from mcp.server.fastmcp.utilities.types import Image
from PIL import Image as PilImage
from pydantic import Field

from nakomis_dragonfruit_mcp import cli, goo
from nakomis_dragonfruit_mcp.app import ToolResult, mcp

CTB_MAGICS = {0x12FD0086: "v2/v3", 0x12FD0106: "v4/v5", 0x12FD0107: "v5 encrypted"}
DEFAULT_IMAGE_PX = 800


class PrintInfo(ToolResult):
    path: str
    format: str = Field(description="Extension of the file: .goo, .nanodlp, ...")
    container: str = Field(description="zip, goo or ctb: how it was read")
    size_bytes: int
    layers: int | None = None
    layer_height_mm: float | None = None
    resolution_px: list[int] | None = Field(None, description="The screen: width, height")
    exposure_s: float | None = None
    bottom_exposure_s: float | None = None
    bottom_layers: int | None = None
    estimated_print_time_s: float | None = None
    estimated_resin_ml: float | None = None
    mirror_x: bool | None = Field(None, description="The file says the image is mirrored")
    machine: str | None = None
    details: dict[str, Any] = Field(default_factory=dict, description="Anything else it reports")


class LayerPreview(ToolResult):
    png_path: str = Field(description="Full-size PNG of the layer, one byte per screen pixel")
    layer: int
    layers: int
    width: int
    height: int
    flipped_x: bool = Field(description="Flipped left-right to read as the plate does")
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
    if len(head) >= 4 and struct.unpack("<I", head[:4])[0] in CTB_MAGICS:
        return "ctb"
    raise cli.CliError(f"{path}: not a ZIP archive, GOO or CTB file")


def _ctb_message(path: Path) -> str:
    with path.open("rb") as f:
        magic = struct.unpack("<I", f.read(4))[0]
    return (
        f"{path.name} is a CTB ({CTB_MAGICS[magic]}): its header and layers are encrypted or "
        "scrambled and cannot be read here. Slice to .goo or .nanodlp to inspect or preview "
        "layers; the slice result already has the layer count, layer height and resolution."
    )


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


@mcp.tool()
def inspect_print(print_path: str) -> PrintInfo:
    """Compact metadata of a sliced print file: layers, layer height, screen, exposure, estimates.

    Reads `.goo` and ZIP-based files (`.nanodlp`, ...). A `.ctb` cannot be read
    (see the warning in the result). Estimates are the file's own when it has them:
    `.goo` carries a print time; `.nanodlp` made by DragonFruit allows a resin
    volume (sum of each layer's area x layer height), not a time.
    """
    path = Path(print_path).expanduser().resolve()
    kind = _kind(path)
    common = {
        "path": str(path),
        "format": path.suffix.lower(),
        "container": kind,
        "size_bytes": path.stat().st_size,
    }
    if kind == "ctb":
        return PrintInfo(**common, warnings=[_ctb_message(path)])
    if kind == "goo":
        try:
            h = goo.read_header(path)
        except goo.GooError as e:
            raise cli.CliError(str(e)) from e
        return PrintInfo(
            **common,
            layers=h.layers,
            layer_height_mm=h.layer_height_mm,
            resolution_px=[h.resolution_x, h.resolution_y],
            exposure_s=h.exposure_s,
            bottom_exposure_s=h.bottom_exposure_s,
            bottom_layers=h.bottom_layers,
            estimated_print_time_s=float(h.print_time_s) or None,
            mirror_x=h.mirror_x,
            machine=h.machine_name or None,
            details={"goo_version": h.version},
        )
    return _inspect_zip(path, common)


def _inspect_zip(path: Path, common: dict[str, Any]) -> PrintInfo:
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
    if slicer:
        printer, material, eff = (slicer.get(k) or {} for k in ("printer", "material", "effective"))
        if "sourceResolutionX" in eff:
            info["resolution_px"] = [eff["sourceResolutionX"], eff["sourceResolutionY"]]
        info.update(
            layer_height_mm=eff.get("layerHeightMm"),
            exposure_s=material.get("normalExposureSec"),
            bottom_exposure_s=material.get("bottomExposureSec"),
            bottom_layers=material.get("bottomLayerCount"),
            mirror_x=eff.get("mirrorX"),
            machine=printer.get("name"),
        )
        details["x_packing"] = eff.get("xPackingMode")
        details["stored_layer_px"] = [eff.get("widthPx"), eff.get("heightPx")]
    else:
        warnings.append("no DragonFruit slicer.json in the archive: only the layer count is known")
    layer_info = meta.get("info.json")
    if isinstance(layer_info, list) and info.get("layer_height_mm"):
        area_mm2 = sum(x.get("TotalSolidArea", 0) for x in layer_info)
        info["estimated_resin_ml"] = round(area_mm2 * info["layer_height_mm"] / 1000, 2)
    return PrintInfo(**common, **info, details=details, warnings=warnings)


@mcp.tool()
def preview_layer(
    print_path: str,
    layer: int,
    out_path: str | None = None,
    flip_x: bool | None = None,
    image_px: int = DEFAULT_IMAGE_PX,
) -> list:
    """Render one layer (1-based) of a sliced `.goo` or ZIP-based print file as a PNG.

    Writes the full-size PNG (one byte per screen pixel; packed formats such as
    the Athena's three-pixels-per-byte RGB are unpacked to the real screen) and
    also returns the image itself, downscaled to about `image_px` wide, so you can
    look at it. Layer 1 is the first layer printed: the base footprint.

    DragonFruit writes the pixels already mirrored for printers whose preset has
    `mirrorX` (the Mars 5 Ultra and Athena both do), so the raw image is the
    screen's, mirrored left-right. `flip_x` flips it back so it reads as the plate
    seen from above (+X right, +Y up, the same frame as `slice`'s plate STL):
    None flips when the file says it is mirrored (`.nanodlp` does; a `.goo` header
    may not, see the warning), True or False force it. A `.ctb` cannot be
    previewed (see `inspect_print`).
    """
    path = Path(print_path).expanduser().resolve()
    kind = _kind(path)
    if kind == "ctb":
        raise cli.CliError(_ctb_message(path))
    warnings: list[str] = []
    if kind == "goo":
        try:
            h = goo.read_header(path)
            data = goo.read_layer(path, h, layer)
            pixels = goo.decode_layer(data, h.resolution_x, h.resolution_y)
        except goo.GooError as e:
            raise cli.CliError(str(e)) from e
        image = PilImage.frombytes("L", (h.resolution_x, h.resolution_y), pixels)
        layers, mirrored = h.layers, h.mirror_x
    else:
        image, layers, mirrored = _zip_layer(path, layer, warnings)
    flipped = mirrored if flip_x is None else flip_x
    if kind == "goo" and flip_x is None and not mirrored:
        warnings.append(
            "the GOO header carries no mirror flag, but DragonFruit mirrors the pixels of "
            "printers whose preset has mirrorX (Mars 5 Ultra does): this image may read "
            "left-right mirrored relative to the plate; pass flip_x=True to flip it"
        )
    if flipped:
        image = image.transpose(PilImage.Transpose.FLIP_LEFT_RIGHT)

    out = (
        Path(out_path).expanduser().resolve()
        if out_path
        else path.with_name(f"{path.name}.layer-{layer}.png")
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out, "PNG")
    scale = max(1, -(-image.width // max(1, image_px)))
    small_size = (-(-image.width // scale), -(-image.height // scale))
    small = image.resize(small_size, PilImage.Resampling.BOX)
    buffer = io.BytesIO()
    small.save(buffer, "PNG")
    result = LayerPreview(
        png_path=str(out),
        layer=layer,
        layers=layers,
        width=image.width,
        height=image.height,
        flipped_x=flipped,
        image_px=list(small.size),
        warnings=warnings,
    )
    return [result.model_dump_json(), Image(data=buffer.getvalue(), format="png")]


def _zip_layer(path: Path, layer: int, warnings: list[str]) -> tuple[PilImage.Image, int, bool]:
    placeholder = {"path": str(path), "format": "", "container": "zip", "size_bytes": 0}
    info = _inspect_zip(path, placeholder)
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
    return image, layers, bool(info.mirror_x)

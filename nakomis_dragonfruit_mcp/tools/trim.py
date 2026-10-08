"""trim_islands: remove regions of a sliced `.goo` that would start in mid-air."""

from __future__ import annotations

import functools
from pathlib import Path

import anyio
from pydantic import Field

from nakomis_dragonfruit_mcp import cli, goo, goo_trim
from nakomis_dragonfruit_mcp.app import ToolResult, mcp

# Leftovers this small sit within HUG_PX of held material in their own layer
# (they were kept as its edge), and light bleed joins them to it: 25 px is a
# 90 um square on an 18 um screen. Anything bigger is worth a look.
SPECK_PX = 25


class TrimResult(ToolResult):
    output_path: str
    layers: int
    layers_changed: int
    regions_dropped: int
    pixels_dropped: int
    dropped_mm3: float = Field(description="Approximate resin removed: pixels x pixel area x layer")
    worst_layers: list[list[int]] = Field(
        description="Up to 10 [layer, regions, pixels] with the most pixels dropped"
    )
    islands_left: int | None = Field(
        None, description="Unsupported cores the independent check still finds; None if skipped"
    )
    largest_island_left_px: int | None = None


def run(print_path: str, out_path: str | None, pixel_um: float, verify: bool) -> TrimResult:
    src = Path(print_path).expanduser()
    if not src.is_file():
        raise cli.CliError(f"print file not found: {src}")
    with src.open("rb") as f:
        if not goo.is_goo(f.read(12)):
            raise cli.CliError(f"{src.name} is not a GOO V1.2/V3.0 file: only those can be trimmed")
    dst = Path(out_path).expanduser() if out_path else src.with_suffix(".trimmed.goo")
    if dst.exists():
        raise cli.CliError(f"{dst} already exists; pass another out_path or remove it first")
    if dst.resolve() == src.resolve():
        raise cli.CliError("out_path must differ from print_path")
    report = goo_trim.trim_islands(src, dst)
    layer_mm = goo.read_header(src).layer_height_mm
    worst = sorted(report.by_layer, key=lambda r: -r[2])[:10]
    result = TrimResult(
        output_path=str(dst),
        layers=report.layers,
        layers_changed=report.layers_changed,
        regions_dropped=report.regions_dropped,
        pixels_dropped=report.pixels_dropped,
        dropped_mm3=round(report.pixels_dropped * (pixel_um / 1000) ** 2 * layer_mm, 3),
        worst_layers=[list(r) for r in worst],
    )
    if verify:
        left = goo_trim.find_islands(dst)
        result.islands_left = len(left)
        result.largest_island_left_px = max((px for _, px in left), default=0)
        if result.largest_island_left_px > SPECK_PX:
            result.warnings.append(
                f"{len(left)} unsupported regions remain, the largest "
                f"{result.largest_island_left_px} px: check before printing"
            )
    return result


@mcp.tool()
async def trim_islands(
    print_path: str,
    out_path: str | None = None,
    pixel_um: float = 18.0,
    verify: bool = True,
) -> TrimResult:
    """Remove every region of a sliced `.goo` that has nothing cured beneath it.

    Such regions don't print: they cure onto the FEP and peel off as flakes.
    Open lattices and cut edges are full of tiny ones that mesh-level checks miss.
    Working up from layer 1, a region's cured core (grey >= 128) is kept when it
    touches the kept core of the layer below (within 2 px); anything else is
    blanked, along with grey edge pixels not hugging a kept core (3 px). A
    trimmed sliver is judged again on the next layer, so it is cut back until
    it meets held material. Unchanged layers, the header and the end marker are
    copied byte-for-byte; changed layers are re-encoded in DragonFruit's layout.

    Writes `out_path` (default `<print>.trimmed.goo`) and never overwrites.
    `pixel_um` is the screen pixel (18 for the Mars 5 Ultra), used only for the
    `dropped_mm3` estimate. `verify` re-reads the output and counts what the
    independent check still finds; specks up to 25 px next to held material are
    expected (light bleed bonds them) and only bigger ones raise a warning.
    Slow on big files: about 14 minutes for 960 full-screen layers with
    `verify`. Supports are part of the layers, so trim a supported print as is.
    """
    work = functools.partial(run, print_path, out_path, pixel_um, verify)
    return await anyio.to_thread.run_sync(work)

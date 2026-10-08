"""trim_islands: remove regions of a sliced `.goo` that would start in mid-air."""

from __future__ import annotations

import functools
import os
from pathlib import Path

import anyio
from pydantic import Field

from nakomis_dragonfruit_mcp import cli, goo, goo_trim
from nakomis_dragonfruit_mcp.app import ToolResult, mcp

# Leftovers this small sit within HUG_PX of held material in their own layer
# (they were kept as its edge), and light bleed joins them to it: 25 px is a
# 90 um square on an 18 um screen. Anything bigger is worth a look.
SPECK_PX = 25

# The optional native backend (accel/goo-trim, built by scripts/build-accel.sh):
# the same algorithm and byte-identical output, about 50x faster.
GOO_TRIM = "goo-trim"
BACKEND_ENV = "NDFM_TRIM_BACKEND"
BACKENDS = ("python", "rust", "auto")


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
    backend: str = Field(description="Which implementation ran: python or rust")
    islands_left: int | None = Field(
        None, description="Unsupported cores the independent check still finds; None if skipped"
    )
    largest_island_left_px: int | None = None


def choose_backend(requested: str | None) -> str:
    """`python` or `rust`: the argument, else $NDFM_TRIM_BACKEND, else python.

    `auto` means rust when `goo-trim` is built, python otherwise; `rust` without
    the binary is an error rather than a silent fallback.
    """
    choice = (requested or os.environ.get(BACKEND_ENV) or "python").strip().lower()
    if choice not in BACKENDS:
        raise cli.CliError(f"unknown trim backend {choice!r}: use one of {', '.join(BACKENDS)}")
    if choice == "python":
        return choice
    try:
        cli.find_binary(GOO_TRIM)
    except cli.CliError as e:
        if choice == "auto":
            return "python"
        raise cli.CliError(
            f"the rust trim backend needs {GOO_TRIM}, which isn't built: "
            "run scripts/build-accel.sh (or use backend='python')"
        ) from e
    return "rust"


def _trim_python(
    src: Path, dst: Path, verify: bool
) -> tuple[goo_trim.TrimReport, list[tuple[int, int]] | None]:
    try:
        report = goo_trim.trim_islands(src, dst)
    except FileExistsError as e:
        raise cli.CliError(f"{dst} appeared while trimming; nothing was overwritten") from e
    return report, goo_trim.find_islands(dst) if verify else None


def _trim_rust(src: Path, dst: Path, verify: bool) -> tuple[goo_trim.TrimReport, int | None, int]:
    args = ["trim", str(src), str(dst), *(["--verify"] if verify else [])]
    try:
        out = cli.run(GOO_TRIM, args, parse_json=True).data
    except cli.CliError as e:
        if "already exists" in str(e):
            raise cli.CliError(f"{dst} appeared while trimming; nothing was overwritten") from e
        raise
    report = goo_trim.TrimReport(
        layers=out["layers"],
        layers_changed=out["layers_changed"],
        regions_dropped=out["regions_dropped"],
        pixels_dropped=out["pixels_dropped"],
        by_layer=[tuple(r) for r in out["by_layer"]],
    )
    return report, out.get("islands_left"), out.get("largest_island_left_px", 0)


def run(
    print_path: str,
    out_path: str | None,
    pixel_um: float,
    verify: bool,
    backend: str | None = None,
) -> TrimResult:
    src = Path(print_path).expanduser()
    if not src.is_file():
        raise cli.CliError(f"print file not found: {src}")
    with src.open("rb") as f:
        if not goo.is_goo(f.read(12)):
            raise cli.CliError(f"{src.name} is not a GOO V1.2/V3.0 file: only those can be trimmed")
    dst = Path(out_path).expanduser() if out_path else src.with_suffix(".trimmed.goo")
    if dst.exists():
        raise cli.CliError(f"{dst} already exists; pass another out_path or remove it first")
    chosen = choose_backend(backend)
    islands_left: int | None = None
    largest = 0
    if chosen == "rust":
        report, islands_left, largest = _trim_rust(src, dst, verify)
    else:
        report, left = _trim_python(src, dst, verify)
        if left is not None:
            islands_left, largest = len(left), max((px for _, px in left), default=0)
    layer_mm = goo.read_header(src).layer_height_mm
    worst = sorted(report.by_layer, key=lambda r: -r[2])[:10]
    result = TrimResult(
        output_path=str(dst),
        backend=chosen,
        layers=report.layers,
        layers_changed=report.layers_changed,
        regions_dropped=report.regions_dropped,
        pixels_dropped=report.pixels_dropped,
        dropped_mm3=round(report.pixels_dropped * (pixel_um / 1000) ** 2 * layer_mm, 3),
        worst_layers=[list(r) for r in worst],
    )
    if verify:
        result.islands_left = islands_left
        result.largest_island_left_px = largest
        if largest > SPECK_PX:
            result.warnings.append(
                f"{islands_left} unsupported regions remain, the largest "
                f"{largest} px: check before printing"
            )
    return result


@mcp.tool()
async def trim_islands(
    print_path: str,
    out_path: str | None = None,
    pixel_um: float = 18.0,
    verify: bool = True,
    backend: str | None = None,
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
    Supports are part of the layers, so trim a supported print as is.

    `backend` picks the implementation: `python` (default), `rust` (the
    optional `goo-trim` binary from `scripts/build-accel.sh`, same output byte
    for byte, about 5 s instead of 4 minutes for 960 full-screen layers with
    `verify`) or `auto` (rust when built). Unset, `$NDFM_TRIM_BACKEND` decides.
    The result's `backend` says which ran.
    """
    work = functools.partial(run, print_path, out_path, pixel_um, verify, backend)
    return await anyio.to_thread.run_sync(work)

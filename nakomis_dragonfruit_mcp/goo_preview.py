"""Write preview pictures into a .goo, which DragonFruit's CLI leaves blank.

A .goo carries two previews after its 194-byte preamble: 116x116 then 290x290,
big-endian RGB565, each followed by CRLF. Printers' screens and print servers
(e.g. Cthulhu) show these as the file's thumbnail. They are fixed size, so
writing them changes no other byte of the file.

`render_and_write` draws the pictures itself (NDFM-14): our Rust tool's
`render` subcommand rasterises the STL in software (no display or GPU), the
model magenta and supports and raft blue, as Chitubox's previews show them.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

from nakomis_dragonfruit_mcp import cli, goo

SMALL = (116, 116)
BIG = (290, 290)
SMALL_AT = 194
BIG_AT = SMALL_AT + SMALL[0] * SMALL[1] * 2 + 2

# Twice the big slot: Pillow's downsampling then smooths the edges.
RENDER_SIZE_PX = 580
MODEL_RGB = (230, 56, 133)
SUPPORT_RGB = (64, 128, 242)
BACKGROUND_RGB = (20, 20, 20)
# A few million triangles render in about a second; this is only a backstop.
RENDER_TIMEOUT_S = 120


def _rgb565(image: Image.Image, size: tuple[int, int], background: tuple[int, int, int]) -> bytes:
    """Fit the image into a square of `size` on `background`, as big-endian RGB565."""
    canvas = Image.new("RGB", size, background)
    fitted = image.copy()
    fitted.thumbnail(size, Image.Resampling.LANCZOS)
    canvas.paste(fitted, ((size[0] - fitted.width) // 2, (size[1] - fitted.height) // 2))
    a = np.asarray(canvas, dtype=np.uint16)
    v = ((a[..., 0] >> 3) << 11) | ((a[..., 1] >> 2) << 5) | (a[..., 2] >> 3)
    return v.astype(">u2").tobytes()


def write_previews(
    goo_path: Path, picture: Path, *, background: tuple[int, int, int] | None = None
) -> None:
    """Put `picture` (any format Pillow reads) into both preview slots of `goo_path`.

    The picture is cropped to its content (anything differing from the corner
    colour) with a small margin, then fitted into each square slot.
    """
    with goo_path.open("r+b") as f:
        head = f.read(BIG_AT + BIG[0] * BIG[1] * 2 + 2)
        if not goo.is_goo(head[:16]):
            raise goo.GooError(f"{goo_path} is not a .goo file")
        for at, size in ((SMALL_AT, SMALL), (BIG_AT, BIG)):
            end = at + size[0] * size[1] * 2
            if head[end : end + 2] != b"\r\n":
                raise goo.GooError("preview slots are not where expected; nothing was written")

        with Image.open(picture) as opened:
            image = opened.convert("RGB")
        corner = image.getpixel((0, 0))
        bg = background or corner
        diff = np.abs(np.asarray(image, dtype=np.int16) - np.array(corner, dtype=np.int16)).sum(
            axis=2
        )
        ys, xs = np.nonzero(diff > 24)
        if len(xs):
            pad = int(0.06 * max(np.ptp(xs), np.ptp(ys)))
            image = image.crop(
                (
                    max(xs.min() - pad, 0),
                    max(ys.min() - pad, 0),
                    min(xs.max() + 1 + pad, image.width),
                    min(ys.max() + 1 + pad, image.height),
                )
            )

        for at, size in ((SMALL_AT, SMALL), (BIG_AT, BIG)):
            f.seek(at)
            f.write(_rgb565(image, size, bg))


def has_preview_slots(path: Path) -> bool:
    """True for a .goo (V1.2 or V3.0) with its two preview slots where expected."""
    try:
        with path.open("rb") as f:
            head = f.read(BIG_AT + BIG[0] * BIG[1] * 2 + 2)
    except OSError:
        return False
    if not goo.is_goo(head[:16]):
        return False
    return all(
        head[at + w * h * 2 : at + w * h * 2 + 2] == b"\r\n"
        for at, (w, h) in ((SMALL_AT, SMALL), (BIG_AT, BIG))
    )


def render_and_write(goo_path: Path, stl_path: Path, *, model_triangles: int | None = None) -> bool:
    """Render `stl_path` and write it into both preview slots of `goo_path`.

    The STL's first `model_triangles` triangles are drawn as the model, the rest
    (supports and raft, as a supported STL holds them) in the support colour;
    without it, everything is model. Returns False, writing nothing, when
    `goo_path` is not a .goo with preview slots (another format); raises
    CliError, GooError or OSError when the render or the write fails.
    """
    if not has_preview_slots(goo_path):
        return False
    with tempfile.TemporaryDirectory(prefix="ndfm-preview-") as tmp:
        picture = Path(tmp) / "preview.png"
        args = [
            "render",
            "--stl", str(stl_path),
            "--out", str(picture),
            "--size", str(RENDER_SIZE_PX),
            "--model-rgb", ",".join(map(str, MODEL_RGB)),
            "--support-rgb", ",".join(map(str, SUPPORT_RGB)),
            "--background", ",".join(map(str, BACKGROUND_RGB)),
        ]  # fmt: skip
        if model_triangles is not None:
            args += ["--split", str(model_triangles)]
        cli.run(cli.MCP_TOOLS, args, parse_json=True, timeout=RENDER_TIMEOUT_S)
        write_previews(goo_path, picture, background=BACKGROUND_RGB)
    return True


def add_previews(
    goo_path: Path, stl_path: Path, warnings: list[str], *, model_triangles: int | None = None
) -> bool:
    """`render_and_write`, with any failure a warning: a slice never fails for its pictures."""
    try:
        return render_and_write(goo_path, stl_path, model_triangles=model_triangles)
    except Exception as e:  # noqa: BLE001
        warnings.append(
            f"the slice succeeded but its preview pictures could not be written "
            f"(the printer shows a placeholder): {e}"
        )
        return False

"""Write preview pictures into a .goo, which DragonFruit's CLI leaves blank.

A .goo carries two previews after its 194-byte preamble: 116x116 then 290x290,
big-endian RGB565, each followed by CRLF. Printers' screens and print servers
(e.g. Cthulhu) show these as the file's thumbnail. They are fixed size, so
writing them changes no other byte of the file.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from nakomis_dragonfruit_mcp import goo

SMALL = (116, 116)
BIG = (290, 290)
SMALL_AT = 194
BIG_AT = SMALL_AT + SMALL[0] * SMALL[1] * 2 + 2


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

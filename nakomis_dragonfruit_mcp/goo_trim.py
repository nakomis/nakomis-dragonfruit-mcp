"""Remove regions of a `.goo`'s layers that would start in mid-air.

A region that has nothing cured beneath it does not print: it cures onto the
FEP, peels off and floats in the vat as a flake. Open lattices are full of
them where a cut edge dips (the gyroid lantern of 2026-10-08 had 518), and
mesh-level fixes miss slivers thinner than their voxels, so this works on the
print file's own layers.

Working up from layer 1, a region's cured core (grey >= CORE) is held when it
touches the held core of the layer below, within TOUCH_PX. Unheld cores are
blanked, and so is grey that isn't within HUG_PX of a held core. A dropped
region's layers above are then judged against what is left, so a sliver is
trimmed until it meets held material. Layers with nothing dropped are copied
byte-for-byte; the header and end marker are kept.

`find_islands` is the matching check, run independently on any `.goo`.
"""

from __future__ import annotations

import os
import shutil
import struct
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy import ndimage

from nakomis_dragonfruit_mcp import goo
from nakomis_dragonfruit_mcp.goo_motion import END_MARKER, _layer_definitions

CORE = 128  # grey at or above this cures through; fainter edge pixels barely do
TOUCH_PX = 2  # a core this close to the held core below rests on it
HUG_PX = 3  # grey this close to a held core is its anti-aliased edge
MARGIN_PX = 8
EIGHT = np.ones((3, 3), bool)


@dataclass
class TrimReport:
    layers: int
    layers_changed: int = 0
    regions_dropped: int = 0
    pixels_dropped: int = 0
    by_layer: list[tuple[int, int, int]] = field(default_factory=list)  # layer, regions, pixels


def _layers(data: bytes | bytearray, path: Path):
    """(definition offset, raw layer data) for every layer, in order."""
    for off in _layer_definitions(bytearray(data), path):
        size = struct.unpack_from(">I", data, off + goo.LAYER_DEF_BYTES)[0]
        start = off + goo.LAYER_DEF_BYTES + 4
        yield off, bytes(data[start : start + size])


def _box(lit: np.ndarray, h: int, w: int) -> tuple[slice, slice] | None:
    """The lit bounding box plus a margin, or None for an empty layer."""
    rows = np.flatnonzero(lit.any(axis=1))
    if rows.size == 0:
        return None
    cols = np.flatnonzero(lit.any(axis=0))
    return (
        slice(max(rows[0] - MARGIN_PX, 0), min(rows[-1] + MARGIN_PX + 1, h)),
        slice(max(cols[0] - MARGIN_PX, 0), min(cols[-1] + MARGIN_PX + 1, w)),
    )


def trim_islands(src: Path, dst: Path) -> TrimReport:
    """Write `src` to `dst` with every unsupported region removed; never overwrites `dst`."""
    header = goo.read_header(src)
    w, h = header.resolution_x, header.resolution_y
    data = src.read_bytes()
    report = TrimReport(layers=header.layers)
    pieces: list[bytes] = []
    held: np.ndarray | None = None  # the held core of the layer below, full frame
    first = None
    for n, (off, raw) in enumerate(_layers(data, src), 1):
        first = off if first is None else first
        full = np.frombuffer(goo.decode_layer(raw, w, h), np.uint8).reshape(h, w).copy()
        box = _box(full > 0, h, w)
        if box is None:
            held = np.zeros((h, w), bool)
            pieces.append(data[off : off + goo.LAYER_DEF_BYTES + 4 + len(raw) + 2])
            continue
        a = full[box]  # a view: blanking it blanks the layer
        core = a >= CORE
        if held is not None:
            below = ndimage.binary_dilation(held[box], iterations=TOUCH_PX)
            labels, count = ndimage.label(core, structure=EIGHT)
            touching = np.unique(labels[core & below])
            core = np.isin(labels, touching[touching > 0])
            drop = (a > 0) & ~ndimage.binary_dilation(core, iterations=HUG_PX)
            if drop.any():
                regions = count - int((touching > 0).sum())
                report.by_layer.append((n, regions, int(drop.sum())))
                report.regions_dropped += regions
                report.pixels_dropped += int(drop.sum())
                a[drop] = 0
        held = np.zeros((h, w), bool)
        held[box] = core
        if report.by_layer and report.by_layer[-1][0] == n:
            new = goo.encode_layer(full.tobytes())
            pieces.append(
                data[off : off + goo.LAYER_DEF_BYTES] + struct.pack(">I", len(new)) + new + b"\r\n"
            )
        else:
            pieces.append(data[off : off + goo.LAYER_DEF_BYTES + 4 + len(raw) + 2])
    report.layers_changed = len(report.by_layer)
    tail = END_MARKER if data.endswith(END_MARKER) else b""
    # A temp file of our own, published with link(): it refuses if dst has
    # appeared meanwhile (another run, or anything else), so nothing is overwritten.
    fd, tmp_name = tempfile.mkstemp(dir=dst.parent, prefix=dst.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data[:first])
            for piece in pieces:
                f.write(piece)
            f.write(tail)
            f.flush()
            os.fsync(f.fileno())
        shutil.copymode(src, tmp)
        os.link(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)
    return report


def find_islands(path: Path) -> list[tuple[int, int]]:
    """(layer, pixels) for every cured core with no cured core within TOUCH_PX below."""
    header = goo.read_header(path)
    w, h = header.resolution_x, header.resolution_y
    found: list[tuple[int, int]] = []
    below: np.ndarray | None = None
    for n, (_, raw) in enumerate(_layers(path.read_bytes(), path), 1):
        core = np.frombuffer(goo.decode_layer(raw, w, h), np.uint8).reshape(h, w) >= CORE
        if below is not None:
            box = _box(core | below, h, w)
            if box is not None:
                c = core[box]
                support = ndimage.binary_dilation(below[box], iterations=TOUCH_PX)
                labels, count = ndimage.label(c, structure=EIGHT)
                touching = set(np.unique(labels[c & support]).tolist())
                sizes = np.bincount(labels.ravel(), minlength=count + 1)
                found += [(n, int(sizes[k])) for k in range(1, count + 1) if k not in touching]
        below = core
    return found

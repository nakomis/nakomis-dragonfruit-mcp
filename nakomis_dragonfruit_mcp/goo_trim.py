"""Remove regions of a `.goo`'s layers that would start in mid-air.

A region that has nothing cured beneath it does not print: it cures onto the
FEP, peels off and floats in the vat as a flake. Open lattices are full of
them where a cut edge dips (the gyroid lantern of 2026-10-08 had 518), and
mesh-level fixes miss slivers thinner than their voxels, so this works on the
print file's own layers.

Working up from layer 1, a region's cured core (grey >= CORE) is held when it
overlaps the held core of the layer below: at least MIN_OVERLAP of its own
pixels must sit on it, and at least MIN_OVERLAP_PX of them (or all of them, for
a region smaller than that). A graze of a pixel or two holds nothing up: in the
vat it tears off as a flake, as a region over empty space does. Unheld cores are
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
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from nakomis_dragonfruit_mcp import goo
from nakomis_dragonfruit_mcp.goo_motion import END_MARKER, _layer_definitions

CORE = 128  # grey at or above this cures through; fainter edge pixels barely do
MIN_OVERLAP = 0.25  # share of a region's core that must sit on held core below
MIN_OVERLAP_PX = 8  # and at least this many pixels, so a speck can't pass on one
HUG_PX = 3  # grey this close to a held core is its anti-aliased edge
MARGIN_PX = 8
# OpenCV's dilate with a 3x3 cross, iterated, is scipy's default binary_dilation
# (identical output, measured on real layers) at a fraction of the cost.
CROSS = cv2.getStructuringElement(cv2.MORPH_CROSS, (3, 3))


def _dilate(mask: np.ndarray, iterations: int) -> np.ndarray:
    return cv2.dilate(mask.view(np.uint8), CROSS, iterations=iterations).view(bool)


def _label(mask: np.ndarray) -> tuple[np.ndarray, int]:
    """8-connected components: (labels, count), labels 1..count."""
    count, labels = cv2.connectedComponents(mask.view(np.uint8), connectivity=8, ltype=cv2.CV_32S)
    return labels, count - 1


def _supported(
    labels: np.ndarray, count: int, below: np.ndarray, min_overlap: float, min_overlap_px: int
) -> np.ndarray:
    """Lookup table over labels: True for each region resting on `below`.

    A region rests when its pixels on `below` are at least `min_overlap` of its
    area and at least `min_overlap_px`, or the whole region if it is smaller.
    """
    sizes = np.bincount(labels.ravel(), minlength=count + 1)
    overlap = np.bincount(labels[below], minlength=count + 1)
    lut = (overlap >= np.minimum(sizes, min_overlap_px)) & (overlap >= min_overlap * sizes)
    lut[0] = False
    return lut


def check_thresholds(min_overlap: float, min_overlap_px: int) -> None:
    """Refuse thresholds that can't mean anything."""
    if not 0.0 <= min_overlap <= 1.0:
        raise ValueError(f"min_overlap must be between 0 and 1, not {min_overlap}")
    if min_overlap_px < 1:
        raise ValueError(f"min_overlap_px must be at least 1, not {min_overlap_px}")


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


def trim_islands(
    src: Path,
    dst: Path,
    min_overlap: float = MIN_OVERLAP,
    min_overlap_px: int = MIN_OVERLAP_PX,
) -> TrimReport:
    """Write `src` to `dst` with every unsupported region removed; never overwrites `dst`.

    A region is supported when at least `min_overlap` of its core, and at least
    `min_overlap_px` pixels of it (all of it, if smaller), sit on held core below.
    """
    check_thresholds(min_overlap, min_overlap_px)
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
            labels, count = _label(core)
            lut = _supported(labels, count, core & held[box], min_overlap, min_overlap_px)
            core = lut[labels]
            drop = (a > 0) & ~_dilate(core, HUG_PX)
            if drop.any():
                regions = count - int(lut.sum())
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


def _layer_table(path: Path) -> list[tuple[int, int]]:
    """(data offset, size) of every layer, read once without loading the file."""
    header = goo.read_header(path)
    table, off = [], header.layer_table_offset
    with path.open("rb") as f:
        for _ in range(header.layers):
            f.seek(off + goo.LAYER_DEF_BYTES)
            size = struct.unpack(">I", f.read(4))[0]
            table.append((off + goo.LAYER_DEF_BYTES + 4, size))
            off += goo.LAYER_DEF_BYTES + 4 + size + 2
    return table


def _islands_in(
    path: Path,
    table: list[tuple[int, int]],
    first: int,
    last: int,
    w: int,
    h: int,
    min_overlap: float = MIN_OVERLAP,
    min_overlap_px: int = MIN_OVERLAP_PX,
):
    """find_islands for layers first..last (1-based), reading layer first-1 for support."""
    found: list[tuple[int, int]] = []
    below = None
    with path.open("rb") as f:
        for n in range(max(first - 1, 1), last + 1):
            start, size = table[n - 1]
            f.seek(start)
            core = np.frombuffer(goo.decode_layer(f.read(size), w, h), np.uint8).reshape(h, w)
            core = core >= CORE
            if below is not None and n >= first:
                box = _box(core | below, h, w)
                if box is not None:
                    c = core[box]
                    labels, count = _label(c)
                    held = _supported(labels, count, c & below[box], min_overlap, min_overlap_px)
                    sizes = np.bincount(labels.ravel(), minlength=count + 1)
                    found += [(n, int(sizes[k])) for k in range(1, count + 1) if not held[k]]
            below = core
    return found


def find_islands(
    path: Path,
    workers: int | None = None,
    min_overlap: float = MIN_OVERLAP,
    min_overlap_px: int = MIN_OVERLAP_PX,
) -> list[tuple[int, int]]:
    """(layer, pixels) for every cured core not resting on the cured core below.

    "Resting" is `trim_islands`'s rule, with the same thresholds.

    Each layer needs only itself and the one below, so ranges of layers are
    checked in parallel processes; `workers=1` runs in this process.
    """
    check_thresholds(min_overlap, min_overlap_px)
    header = goo.read_header(path)
    w, h = header.resolution_x, header.resolution_y
    table = _layer_table(path)
    workers = workers or min(os.cpu_count() or 1, 8)
    step = -(-header.layers // (workers * 4))  # a few chunks per worker evens out the load
    ranges = [(a, min(a + step - 1, header.layers)) for a in range(2, header.layers + 1, step)]
    thresholds = (min_overlap, min_overlap_px)
    if workers == 1 or len(ranges) <= 1:
        return [i for a, b in ranges for i in _islands_in(path, table, a, b, w, h, *thresholds)]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        jobs = [pool.submit(_islands_in, path, table, a, b, w, h, *thresholds) for a, b in ranges]
        return [i for job in jobs for i in job.result()]

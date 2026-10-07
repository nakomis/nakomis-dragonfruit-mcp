"""Binary STL reading and writing, just enough to place a model on the build plate.

`dragonfruit-ts-cli` reads binary STL only, so that is all we accept. Reads are
memory-mapped numpy views, so multi-million-triangle files stay cheap.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

HEADER_BYTES = 84
# normal (3), vertices (3 x 3), attribute byte count: 50 bytes, unaligned.
TRIANGLE = np.dtype([("normal", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])
assert TRIANGLE.itemsize == 50


class StlError(ValueError):
    pass


def _triangles(path: Path) -> np.ndarray:
    size = path.stat().st_size
    if size < HEADER_BYTES:
        raise StlError(f"{path} is too small to be an STL")
    with path.open("rb") as f:
        head = f.read(HEADER_BYTES)
    count = int.from_bytes(head[80:84], "little")
    expected = HEADER_BYTES + count * TRIANGLE.itemsize
    ascii_like = head.lstrip().startswith(b"solid")
    if size < expected:
        if ascii_like:
            raise StlError(f"{path} is not a binary STL (it looks like ASCII; not supported)")
        raise StlError(f"{path} is a truncated binary STL: it declares {count} triangles")
    if size > expected:
        what = "an ASCII STL" if ascii_like else "a corrupt binary STL"
        raise StlError(
            f"{path} is not a binary STL: {size - expected} bytes follow the {count} declared "
            f"triangles (looks like {what}; ASCII STL is not supported)"
        )
    if count == 0:
        raise StlError(f"{path} has no triangles")
    return np.memmap(path, dtype=TRIANGLE, mode="r", offset=HEADER_BYTES, shape=(count,))


def bbox(path: Path) -> tuple[list[float], list[float]]:
    """(min, max) of every vertex, each as [x, y, z]."""
    tris = _triangles(path)
    verts = tris["v"].reshape(-1, 3)
    return [float(x) for x in verts.min(axis=0)], [float(x) for x in verts.max(axis=0)]


def write_translated(src: Path, dst: Path, offset: tuple[float, float, float]) -> None:
    """Write `src` moved by `offset` mm. Vertices are rounded to float32 as the slicer's are."""
    tris = _triangles(src)
    with src.open("rb") as f:
        header = f.read(HEADER_BYTES)
    shift = np.asarray(offset, dtype=np.float64)
    with dst.open("wb") as out:
        out.write(header)
        step = 1_000_000  # bounded memory: a million triangles is 50 MB
        for start in range(0, len(tris), step):
            chunk = np.array(tris[start : start + step])
            chunk["v"] = (chunk["v"].astype(np.float64) + shift).astype("<f4")
            out.write(chunk.tobytes())

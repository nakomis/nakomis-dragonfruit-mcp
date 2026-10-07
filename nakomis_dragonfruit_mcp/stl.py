"""Binary STL reading and writing, just enough to place a model on the build plate.

`dragonfruit-ts-cli` reads binary STL only, so that is all we accept.
"""

from __future__ import annotations

import struct
from pathlib import Path

HEADER_BYTES = 84
_TRIANGLE = struct.Struct("<12fH")  # normal (3), vertices (9), attribute byte count


class StlError(ValueError):
    pass


def _read(path: Path) -> tuple[bytes, bytes]:
    data = path.read_bytes()
    if len(data) < HEADER_BYTES:
        raise StlError(f"{path} is too small to be an STL")
    count = struct.unpack_from("<I", data, 80)[0]
    if len(data) != HEADER_BYTES + count * _TRIANGLE.size:
        raise StlError(f"{path} is not a binary STL (ASCII STL is not supported)")
    return data[:HEADER_BYTES], data[HEADER_BYTES:]


def bbox(path: Path) -> tuple[list[float], list[float]]:
    """(min, max) of every vertex, each as [x, y, z]."""
    _, body = _read(path)
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    for tri in _TRIANGLE.iter_unpack(body):
        for axis in range(3):
            column = tri[3 + axis : 12 : 3]
            lo[axis] = min(lo[axis], *column)
            hi[axis] = max(hi[axis], *column)
    if lo[0] == float("inf"):
        raise StlError(f"{path} has no triangles")
    return lo, hi


def write_translated(src: Path, dst: Path, offset: tuple[float, float, float]) -> None:
    """Write `src` moved by `offset` mm. Vertices are rounded to float32 as the slicer's are."""
    header, body = _read(src)
    out = bytearray(header)
    dx, dy, dz = offset
    for tri in _TRIANGLE.iter_unpack(body):
        v = tri[3:12]
        moved = (
            *tri[:3],
            v[0] + dx, v[1] + dy, v[2] + dz,
            v[3] + dx, v[4] + dy, v[5] + dz,
            v[6] + dx, v[7] + dy, v[8] + dz,
            tri[12],
        )  # fmt: skip
        out += _TRIANGLE.pack(*moved)
    dst.write_bytes(out)

"""Reading Elegoo `.goo` print files: the header, and a layer's RLE image.

`dragonfruit-cli print inspect` and `slice preview-layer` only open ZIP-based
archives, so `.goo` is read here. The layout is DragonFruit's own GOO V1.2
writer's (big-endian; `plugins/elegoo/slicing/rust/goo_layout.rs`), cross-checked
against an independent GOO reader verified on a real file from another slicer.
Both use the same header and layer definitions.

Layer data is `0x55`, runs, then a checksum byte (the bitwise NOT of the sum of
the run bytes). A run starts with `[TT][SS][CCCC]`: TT 00 black, 01 grey (the
grey value is the next byte), 11 white; SS says how many extra length bytes
follow (0-3, big-endian, above the 4 low bits in CCCC). DragonFruit's V1.2
encoder never writes the format's "step" runs (TT 10), so those are refused.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

MAGIC_VERSIONS = (b"V1.2", b"V3.0")
V5_VERSION = b"V5.1"  # little-endian, partitioned layers: a different container, not read here
# A sane screen: no side over this, and no more pixels than this (a hostile header could
# otherwise make decode_layer allocate gigabytes).
MAX_SIDE_PX = 30_000
MAX_PIXELS = 400_000_000
# Smallest possible layer: 66-byte definition, u32 size, 0x55 + checksum, CRLF.
MIN_LAYER_BYTES = 66 + 4 + 2 + 2
FILE_MAGIC = bytes([0x07, 0x00, 0x00, 0x00, 0x44, 0x4C, 0x50, 0x00])
# 194 bytes of identity strings, then two RGB565 previews (116x116, 290x290), each + CRLF.
SETTINGS_OFFSET = 194 + (116 * 116 * 2 + 2) + (290 * 290 * 2 + 2)
LAYER_DEF_BYTES = 66  # plus a u32 data length
HEADER_BYTES = SETTINGS_OFFSET + 176


class GooError(ValueError):
    pass


@dataclass
class GooHeader:
    version: str
    software: str
    machine_name: str
    layers: int
    resolution_x: int
    resolution_y: int
    mirror_x: bool
    mirror_y: bool
    layer_height_mm: float
    exposure_s: float
    bottom_exposure_s: float
    bottom_layers: int
    print_time_s: int
    layer_table_offset: int


def is_goo(head: bytes) -> bool:
    return len(head) >= 12 and head[:4] in MAGIC_VERSIONS and head[4:12] == FILE_MAGIC


def is_goo_v5(head: bytes) -> bool:
    return len(head) >= 12 and head[:4] == V5_VERSION and head[4:12] == FILE_MAGIC


def _text(raw: bytes) -> str:
    return raw.split(b"\0")[0].decode("latin-1")


def read_header(path: Path) -> GooHeader:
    with path.open("rb") as f:
        data = f.read(HEADER_BYTES)
    if not is_goo(data):
        raise GooError(f"{path} is not a GOO file")
    if len(data) < HEADER_BYTES:
        raise GooError(f"{path} is truncated inside the header")
    s = SETTINGS_OFFSET

    def f32(offset: int) -> float:
        return round(struct.unpack_from(">f", data, s + offset)[0], 6)

    header = GooHeader(
        version=data[:4].decode(),
        software=_text(data[12:44]),
        machine_name=_text(data[92:124]),
        layers=struct.unpack_from(">I", data, s)[0],
        resolution_x=struct.unpack_from(">H", data, s + 4)[0],
        resolution_y=struct.unpack_from(">H", data, s + 6)[0],
        mirror_x=data[s + 8] == 1,
        mirror_y=data[s + 9] == 1,
        layer_height_mm=f32(22),
        exposure_s=f32(26),
        bottom_exposure_s=f32(59),
        bottom_layers=struct.unpack_from(">I", data, s + 63)[0],
        print_time_s=struct.unpack_from(">I", data, s + 136)[0],
        layer_table_offset=struct.unpack_from(">I", data, s + 160)[0],
    )
    if not (header.layers and header.resolution_x and header.resolution_y):
        raise GooError(f"{path}: the header does not make sense (no layers or resolution)")
    if header.layer_table_offset < s:
        raise GooError(f"{path}: the layer table starts inside the header")
    if (
        header.resolution_x > MAX_SIDE_PX
        or header.resolution_y > MAX_SIDE_PX
        or header.resolution_x * header.resolution_y > MAX_PIXELS
    ):
        raise GooError(
            f"{path}: implausible screen {header.resolution_x}x{header.resolution_y} "
            f"(limits {MAX_SIDE_PX} per side, {MAX_PIXELS // 1_000_000} MP)"
        )
    room = path.stat().st_size - header.layer_table_offset
    if header.layers * MIN_LAYER_BYTES > room:
        raise GooError(
            f"{path}: the header claims {header.layers} layers but the file is too short"
        )
    return header


def read_layer(path: Path, header: GooHeader, layer: int) -> bytes:
    """The raw RLE data of a 1-based layer, found by walking the layer table."""
    if not 1 <= layer <= header.layers:
        raise GooError(f"layer {layer} out of range: the file has {header.layers} layers")
    with path.open("rb") as f:
        offset = header.layer_table_offset
        for index in range(1, layer + 1):
            f.seek(offset)
            head = f.read(LAYER_DEF_BYTES + 4)
            if (
                len(head) < LAYER_DEF_BYTES + 4
                or head[LAYER_DEF_BYTES - 2 : LAYER_DEF_BYTES] != b"\r\n"
            ):
                raise GooError(f"layer {index} is not where the header says (truncated file?)")
            size = struct.unpack_from(">I", head, LAYER_DEF_BYTES)[0]
            if index == layer:
                if size > header_room(path, offset):
                    raise GooError(f"layer {layer} claims {size} bytes: more than the file holds")
                data = f.read(size)
                if len(data) != size:
                    raise GooError(f"layer {layer} is truncated")
                return data
            offset += LAYER_DEF_BYTES + 4 + size + 2
    raise AssertionError("unreachable")


def header_room(path: Path, offset: int) -> int:
    return path.stat().st_size - offset


def decode_layer(data: bytes, width: int, height: int) -> bytes:
    """Row-major 8-bit grey pixels of one layer."""
    if not data or data[0] != 0x55:
        raise GooError("layer data does not start with 0x55")
    end = len(data) - 1
    if (~sum(data[1:end])) & 0xFF != data[end]:
        raise GooError("layer checksum does not match")
    total = width * height
    out = bytearray(total)  # black
    pixel = 0
    i = 1

    def take() -> int:
        """The next byte of the run stream, which must stay before the checksum."""
        nonlocal i
        i += 1
        if i >= end:
            raise GooError("layer data truncated")
        return data[i]

    while i < end:
        b = data[i]
        kind = b >> 6
        if kind == 0b10:
            raise GooError("layer uses step runs, which DragonFruit's encoder never writes")
        value = 0
        if kind == 0b01:
            value = take()
        elif kind == 0b11:
            value = 255
        high = 0
        for _ in range((b >> 4) & 3):
            high = high * 256 + take()
        length = high * 16 + (b & 0x0F)
        if pixel + length > total:
            raise GooError("layer decodes to too many pixels")
        if value:
            out[pixel : pixel + length] = bytes([value]) * length
        pixel += length
        i += 1
    if pixel != total:
        raise GooError(f"layer decodes to {pixel} pixels, expected {total}")
    return bytes(out)


MAX_RUN = (1 << 28) - 1  # 4 length bits + 3 extra bytes


def encode_layer(pixels: bytes | bytearray | memoryview) -> bytes:
    """Row-major 8-bit grey pixels as layer data: the inverse of `decode_layer`.

    Writes DragonFruit's V1.2 layout (no step runs), each run with the fewest
    length bytes that hold it, so a DragonFruit layer re-encodes byte-for-byte.
    """
    p = np.frombuffer(pixels, np.uint8)
    if p.size == 0:
        raise GooError("a layer needs at least one pixel")
    starts = np.r_[0, np.flatnonzero(p[1:] != p[:-1]) + 1]
    lengths = np.diff(np.r_[starts, p.size]).astype(np.int64)
    values = p[starts].astype(np.int64)
    if (lengths > MAX_RUN).any():
        # Split over-long runs into MAX_RUN pieces plus the remainder.
        pieces = -(-lengths // MAX_RUN)
        values = np.repeat(values, pieces)
        full = np.repeat(lengths, pieces)
        first = np.r_[0, np.cumsum(pieces)[:-1]]
        lengths = np.full(full.size, MAX_RUN, np.int64)
        lengths[first + pieces - 1] = full[first] - MAX_RUN * (pieces - 1)
    kind = np.where(values == 0, 0, np.where(values == 255, 3, 1))
    extra = np.searchsorted([16, 1 << 12, 1 << 20], lengths, side="right")
    grey = kind == 1
    size = 1 + grey + extra
    at = np.r_[0, np.cumsum(size)[:-1]]
    out = np.zeros(int(size.sum()), np.uint8)
    out[at] = (kind << 6) | (extra << 4) | (lengths & 0x0F)
    out[at[grey] + 1] = values[grey]
    high = lengths >> 4
    after = at + 1 + grey
    for k in (1, 2, 3):  # length bytes above the low 4 bits, big-endian
        has = extra >= k
        out[after[has] + k - 1] = (high[has] >> (8 * (extra[has] - k))) & 0xFF
    checksum = ~int(out.sum(dtype=np.int64)) & 0xFF
    return b"\x55" + out.tobytes() + bytes([checksum])

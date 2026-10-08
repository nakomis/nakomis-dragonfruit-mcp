"""Make a DragonFruit .goo move the plate the way Chitubox's does on tilting printers.

DragonFruit's tilting-mode .goo (Elegoo Mars 5 Ultra, as of upstream dev
bba15a50) left the build plate at the bottom for a whole print: it switches
per-layer settings on, lifts 0.05 mm but retracts 6.0 mm, and writes 0 for each
layer's position Z. A Chitubox .goo that prints on the same printer switches
per-layer settings off, lifts and retracts 0.05 mm, fills in the position Z,
rests 3 s before each exposure, and sets delay mode 1 and the mirror flag.

`normalise_tilting_motion` rewrites those fields in place. Every one is fixed
size, so the file's length and layer data are untouched. Exposure times,
bottom and transition layers, PWM and the layer images are left as they are.
"""

from __future__ import annotations

import os
import shutil
import struct
from dataclasses import dataclass
from pathlib import Path

from nakomis_dragonfruit_mcp import goo

# Header fields after the two previews, in write order (DragonFruit's
# goo_encoder.rs write_goo_header, matching Cthulhu's packages/goo/header.ts).
_HEADER_FIELDS: list[tuple[str, str]] = [
    ("layers", "I"),
    ("res_x", "H"),
    ("res_y", "H"),
    ("mirror_x", "B"),
    ("mirror_y", "B"),
    ("build_w", "f"),
    ("build_d", "f"),
    ("machine_z", "f"),
    ("layer_h", "f"),
    ("exposure", "f"),
    ("delay_mode", "B"),
    ("light_off", "f"),
    ("b_wait_after_cure", "f"),
    ("b_wait_after_lift", "f"),
    ("b_wait_before_cure", "f"),
    ("wait_after_cure", "f"),
    ("wait_after_lift", "f"),
    ("wait_before_cure", "f"),
    ("b_exposure", "f"),
    ("b_layers", "I"),
    ("b_lift_h", "f"),
    ("b_lift_s", "f"),
    ("lift_h", "f"),
    ("lift_s", "f"),
    ("b_ret_h", "f"),
    ("b_ret_s", "f"),
    ("ret_h", "f"),
    ("ret_s", "f"),
    ("b_lift_h2", "f"),
    ("b_lift_s2", "f"),
    ("lift_h2", "f"),
    ("lift_s2", "f"),
    ("b_ret_h2", "f"),
    ("b_ret_s2", "f"),
    ("ret_h2", "f"),
    ("ret_s2", "f"),
    ("b_pwm", "H"),
    ("pwm", "H"),
    ("per_layer", "B"),
]
# Per-layer definition (goo_encoder.rs write_goo_layer): 66 bytes incl. CRLF.
_LAYER_FIELDS: list[tuple[str, str]] = [
    ("pause", "H"),
    ("pause_z", "f"),
    ("z", "f"),
    ("exposure", "f"),
    ("light_off", "f"),
    ("w_after_cure", "f"),
    ("w_after_lift", "f"),
    ("w_before_cure", "f"),
    ("lift_h", "f"),
    ("lift_s", "f"),
    ("lift_h2", "f"),
    ("lift_s2", "f"),
    ("ret_h", "f"),
    ("ret_s", "f"),
    ("ret_h2", "f"),
    ("ret_s2", "f"),
    ("pwm", "H"),
]


def _offsets(fields: list[tuple[str, str]], start: int) -> dict[str, tuple[int, str]]:
    out, off = {}, start
    for name, fmt in fields:
        out[name] = (off, fmt)
        off += struct.calcsize(">" + fmt)
    return out


HEADER = _offsets(_HEADER_FIELDS, goo.SETTINGS_OFFSET)
LAYER = _offsets(_LAYER_FIELDS, 0)

# Chitubox's values on a Mars 5 Ultra (firmware V1.5.0), from a .goo that printed.
REST_BEFORE_CURE_S = 3.0

# Both DragonFruit and Chitubox end a .goo with this after the last layer.
END_MARKER = b"\x00\x00\x00\x07\x00\x00\x00DLP\x00"
TILT_MOVE_MM = 0.05


@dataclass
class MotionReport:
    layers_patched: int
    header_before: dict[str, float]
    header_after: dict[str, float]


def _get(buf: bytearray | bytes, where: tuple[int, str], base: int = 0) -> float:
    off, fmt = where
    return struct.unpack_from(">" + fmt, buf, base + off)[0]


def _set(buf: bytearray, where: tuple[int, str], value: float, base: int = 0) -> None:
    off, fmt = where
    struct.pack_into(">" + fmt, buf, base + off, value)


def normalise_tilting_motion(path: Path, *, mirror_x: bool | None = None) -> MotionReport:
    """Rewrite a .goo's motion fields in place to Chitubox's tilting-printer values.

    mirror_x, when given, also sets the header's mirror flag (DragonFruit leaves
    it 0 even when the layer images are mirrored).
    """
    # The whole file is held in memory (about 2x its size at peak): fine for the
    # ~150-300 MB files a resin print makes.
    data = bytearray(path.read_bytes())
    if not goo.is_goo(data[:16]):
        raise goo.GooError(f"{path} is not a .goo file")
    watched = (
        "per_layer",
        "delay_mode",
        "mirror_x",
        "b_wait_before_cure",
        "wait_before_cure",
        "b_lift_h",
        "b_lift_s",
        "lift_h",
        "lift_s",
        "b_ret_h",
        "b_ret_s",
        "ret_h",
        "ret_s",
    )
    before = {k: _get(data, HEADER[k]) for k in watched}

    # Validate the whole layer table before changing a single byte.
    layer_defs = _layer_definitions(data, path)

    _set(data, HEADER["per_layer"], 0)
    _set(data, HEADER["delay_mode"], 1)
    for k in ("b_wait_before_cure", "wait_before_cure"):
        _set(data, HEADER[k], REST_BEFORE_CURE_S)
    for k in ("b_lift_h", "b_lift_s", "lift_h", "lift_s", "b_ret_h", "b_ret_s", "ret_h", "ret_s"):
        _set(data, HEADER[k], TILT_MOVE_MM)
    if mirror_x is not None:
        _set(data, HEADER["mirror_x"], int(mirror_x))

    for off in layer_defs:
        _set(data, LAYER["pause_z"], _get(data, LAYER["z"], off), off)
        _set(data, LAYER["w_before_cure"], REST_BEFORE_CURE_S, off)
        for k in ("lift_h", "lift_s", "ret_h", "ret_s"):
            _set(data, LAYER[k], TILT_MOVE_MM, off)

    _replace_atomically(path, data)
    after = {k: _get(data, HEADER[k]) for k in watched}
    return MotionReport(layers_patched=len(layer_defs), header_before=before, header_after=after)


def _layer_definitions(data: bytearray, path: Path) -> list[int]:
    """Offsets of every layer definition; raises GooError unless the table is whole."""
    if len(data) < goo.SETTINGS_OFFSET + 176:
        raise goo.GooError(f"{path} is too short for a .goo header")
    layers = int(_get(data, HEADER["layers"]))
    table = struct.unpack_from(">I", data, goo.SETTINGS_OFFSET + 160)[0]
    if table < goo.SETTINGS_OFFSET + 164 or table > len(data):
        raise goo.GooError(f"{path}: layer table offset {table} is outside the file")
    offsets, off = [], table
    for index in range(1, layers + 1):
        end_of_def = off + goo.LAYER_DEF_BYTES
        if end_of_def + 4 > len(data) or data[end_of_def - 2 : end_of_def] != b"\r\n":
            raise goo.GooError(f"{path}: layer {index} is not where expected; nothing was written")
        size = struct.unpack_from(">I", data, end_of_def)[0]
        end = end_of_def + 4 + size
        if end + 2 > len(data) or data[end : end + 2] != b"\r\n":
            raise goo.GooError(f"{path}: layer {index} is truncated; nothing was written")
        offsets.append(off)
        off = end + 2
    if data[off:] not in (b"", END_MARKER):
        raise goo.GooError(
            f"{path}: {len(data) - off} unexpected bytes after the last layer; nothing was written"
        )
    return offsets


def _replace_atomically(path: Path, data: bytes) -> None:
    """Write next to the target, keep its mode, then swap it in; no partial target."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        shutil.copymode(path, tmp)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)

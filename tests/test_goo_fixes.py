import struct
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from test_printfile import LAYER_1, LAYER_2, LAYER_3, make_goo

from nakomis_dragonfruit_mcp import goo
from nakomis_dragonfruit_mcp.goo_motion import HEADER, LAYER, normalise_tilting_motion
from nakomis_dragonfruit_mcp.goo_preview import BIG, BIG_AT, SMALL, SMALL_AT, write_previews
from nakomis_dragonfruit_mcp.printers import SliceJob, SliceRun
from nakomis_dragonfruit_mcp.printers.builtin.mars5ultra import Mars5Ultra


def put(buf, where, value, base=0):
    off, fmt = where
    struct.pack_into(">" + fmt, buf, base + off, value)


def get(buf, where, base=0):
    off, fmt = where
    return struct.unpack_from(">" + fmt, buf, base + off)[0]


def layer_offsets(data):
    off = struct.unpack_from(">I", data, goo.SETTINGS_OFFSET + 160)[0]
    out = []
    for _ in range(get(data, HEADER["layers"])):
        out.append(off)
        off += (
            goo.LAYER_DEF_BYTES
            + 4
            + struct.unpack_from(">I", data, off + goo.LAYER_DEF_BYTES)[0]
            + 2
        )
    return out


@pytest.fixture
def dragonfruit_goo(tmp_path):
    """A .goo with DragonFruit's tilting-mode motion values, as found on 2026-10-07."""
    path = tmp_path / "df.goo"
    make_goo(path, [LAYER_1, LAYER_2, LAYER_3])
    data = bytearray(path.read_bytes())
    # Real files end each preview slot with CRLF.
    for at, size in ((SMALL_AT, SMALL), (BIG_AT, BIG)):
        end = at + size[0] * size[1] * 2
        data[end : end + 2] = b"\r\n"
    put(data, HEADER["per_layer"], 1)
    for k, v in (
        ("b_lift_h", 0.05),
        ("lift_h", 0.05),
        ("b_lift_s", 60.0),
        ("lift_s", 60.0),
        ("b_ret_h", 6.0),
        ("ret_h", 6.0),
        ("b_ret_s", 150.0),
        ("ret_s", 150.0),
    ):
        put(data, HEADER[k], v)
    for i, off in enumerate(layer_offsets(data), start=1):
        put(data, LAYER["z"], 0.05 * i, off)
        put(data, LAYER["exposure"], 2.0, off)
        for k, v in (("lift_h", 0.05), ("lift_s", 60.0), ("ret_h", 6.0), ("ret_s", 150.0)):
            put(data, LAYER[k], v, off)
    path.write_bytes(data)
    return path


def test_motion_fields_match_chitubox(dragonfruit_goo):
    before = dragonfruit_goo.read_bytes()
    report = normalise_tilting_motion(dragonfruit_goo, mirror_x=True)
    data = dragonfruit_goo.read_bytes()
    assert len(data) == len(before)
    assert report.layers_patched == 3
    assert get(data, HEADER["per_layer"]) == 0
    assert get(data, HEADER["delay_mode"]) == 1
    assert get(data, HEADER["mirror_x"]) == 1
    for k in ("b_wait_before_cure", "wait_before_cure"):
        assert get(data, HEADER[k]) == pytest.approx(3.0)
    for k in ("b_lift_h", "b_lift_s", "lift_h", "lift_s", "b_ret_h", "b_ret_s", "ret_h", "ret_s"):
        assert get(data, HEADER[k]) == pytest.approx(0.05)
    for i, off in enumerate(layer_offsets(data), start=1):
        assert get(data, LAYER["pause_z"], off) == pytest.approx(0.05 * i)  # = the layer's Z
        assert get(data, LAYER["z"], off) == pytest.approx(0.05 * i)
        assert get(data, LAYER["exposure"], off) == pytest.approx(2.0)  # untouched
        assert get(data, LAYER["w_before_cure"], off) == pytest.approx(3.0)
        for k in ("lift_h", "lift_s", "ret_h", "ret_s"):
            assert get(data, LAYER[k], off) == pytest.approx(0.05)


def test_motion_fix_leaves_layer_images_alone(dragonfruit_goo):
    header = goo.read_header(dragonfruit_goo)
    before = [goo.read_layer(dragonfruit_goo, header, n) for n in (1, 2, 3)]
    normalise_tilting_motion(dragonfruit_goo)
    header = goo.read_header(dragonfruit_goo)
    assert [goo.read_layer(dragonfruit_goo, header, n) for n in (1, 2, 3)] == before


def test_motion_fix_without_mirror_keeps_the_flag(dragonfruit_goo):
    normalise_tilting_motion(dragonfruit_goo)
    assert get(dragonfruit_goo.read_bytes(), HEADER["mirror_x"]) == 0


def test_motion_fix_refuses_other_files(tmp_path):
    other = tmp_path / "x.goo"
    other.write_bytes(b"not a goo" * 100)
    with pytest.raises(goo.GooError):
        normalise_tilting_motion(other)


def test_motion_fix_refuses_a_broken_layer_table_without_writing(dragonfruit_goo):
    data = bytearray(dragonfruit_goo.read_bytes())
    off = layer_offsets(data)[1]
    data[off + goo.LAYER_DEF_BYTES - 2 : off + goo.LAYER_DEF_BYTES] = b"XX"
    dragonfruit_goo.write_bytes(data)
    with pytest.raises(goo.GooError):
        normalise_tilting_motion(dragonfruit_goo)
    assert dragonfruit_goo.read_bytes() == data


def test_previews_are_written_and_read_back(dragonfruit_goo, tmp_path):
    picture = tmp_path / "p.png"
    img = Image.new("RGB", (400, 300), (20, 20, 20))
    img.paste((230, 56, 133), (150, 100, 250, 200))  # magenta block on dark
    img.save(picture)
    before = dragonfruit_goo.read_bytes()
    write_previews(dragonfruit_goo, picture)
    after = dragonfruit_goo.read_bytes()
    assert len(after) == len(before)
    changed = [i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a != b]
    assert changed and min(changed) >= SMALL_AT and max(changed) < BIG_AT + BIG[0] * BIG[1] * 2
    v = np.frombuffer(after[BIG_AT : BIG_AT + BIG[0] * BIG[1] * 2], dtype=">u2").reshape(BIG)
    centre = int(v[145, 145])
    assert ((centre >> 11) & 31) > 20 and (centre & 31) > 10  # magenta-ish, not background
    assert int(v[0, 0]) == ((20 >> 3) << 11) | ((20 >> 2) << 5) | (20 >> 3)  # background


def test_previews_refuse_other_files(tmp_path):
    other = tmp_path / "x.goo"
    other.write_bytes(b"\0" * 300_000)
    picture = tmp_path / "p.png"
    Image.new("RGB", (10, 10)).save(picture)
    with pytest.raises(goo.GooError):
        write_previews(other, picture)


def test_mars5ultra_postprocess_fixes_goo_only(dragonfruit_goo, tmp_path):
    printer = Mars5Ultra()
    run = SliceRun(profile={"display": {"mirrorX": True}}, cli_args=[], result={})
    job = SliceJob(stl_path=Path("x.stl"), out_path=dragonfruit_goo)
    assert printer.postprocess(dragonfruit_goo, job, run) == dragonfruit_goo
    data = dragonfruit_goo.read_bytes()
    assert get(data, HEADER["per_layer"]) == 0 and get(data, HEADER["mirror_x"]) == 1
    ctb = tmp_path / "x.ctb"
    ctb.write_bytes(b"ctb bytes")
    assert printer.postprocess(ctb, job, run) == ctb
    assert ctb.read_bytes() == b"ctb bytes"

import asyncio
import os
from pathlib import Path

import numpy as np
import pytest
from test_printfile import make_goo, rle

from nakomis_dragonfruit_mcp import cli, goo, goo_trim
from nakomis_dragonfruit_mcp.goo_motion import END_MARKER
from nakomis_dragonfruit_mcp.tools import trim

W, H = 40, 30


def frame(*boxes, grey=None):
    """A W x H layer with white boxes (r0, r1, c0, c1) and optional grey boxes (box, value)."""
    a = np.zeros((H, W), np.uint8)
    for r0, r1, c0, c1 in boxes:
        a[r0:r1, c0:c1] = 255
    for (r0, r1, c0, c1), value in grey or []:
        a[r0:r1, c0:c1] = value
    return a.ravel().tolist()


def pixels_of(path: Path, layer: int) -> np.ndarray:
    h = goo.read_header(path)
    raw = goo.read_layer(path, h, layer)
    return np.frombuffer(goo.decode_layer(raw, W, H), np.uint8).reshape(H, W)


# --- encode_layer -------------------------------------------------------------


def test_encode_pins_literal_bytes():
    # black 1, white 2, black 2, grey 128 x2, black 1; checksum ~0x188 & 0xFF
    assert goo.encode_layer(bytes([0, 255, 255, 0, 0, 128, 128, 0])) == bytes(
        [0x55, 0x01, 0xC2, 0x02, 0x42, 0x80, 0x01, 0x77]
    )


@pytest.mark.parametrize(
    "pixels",
    [
        [0] * 1,
        [255] * 15,
        [255] * 16,  # first run needing a length byte
        [0] * 4095 + [7] * 4096,  # either side of the second length byte
        [0] * ((1 << 20) - 1) + [255] * (1 << 20),  # either side of the third
        [0, 255, 1, 254, 128] * 50,
    ],
    ids=["one", "15", "16", "4k-edge", "1M-edge", "mixed"],
)
def test_encode_matches_reference_rle(pixels):
    data = bytes(pixels)
    assert goo.encode_layer(data) == rle(pixels)
    assert goo.decode_layer(goo.encode_layer(data), len(pixels), 1) == data


def test_encode_random_layer_round_trips():
    rng = np.random.default_rng(1)
    runs = rng.integers(1, 3000, 400)
    values = rng.choice([0, 255, 64, 200], 400)
    pixels = np.repeat(values, runs).astype(np.uint8).tobytes()
    assert goo.decode_layer(goo.encode_layer(pixels), len(pixels), 1) == pixels


def test_encode_splits_runs_longer_than_one_record(monkeypatch):
    monkeypatch.setattr(goo, "MAX_RUN", 100)
    pixels = bytes([255] * 250 + [0] * 3)
    data = goo.encode_layer(pixels)
    assert goo.decode_layer(data, len(pixels), 1) == pixels
    assert data.count(bytes([0xD4, 0x06])) == 2  # two full 100-pixel white records (6 * 16 + 4)


def test_encode_refuses_empty():
    with pytest.raises(goo.GooError):
        goo.encode_layer(b"")


REAL_GOO = os.environ.get("NDFM_TEST_GOO")


@pytest.mark.skipif(not REAL_GOO, reason="set NDFM_TEST_GOO to a DragonFruit .goo")
def test_encode_reproduces_a_real_dragonfruit_file():
    path = Path(REAL_GOO)
    h = goo.read_header(path)
    for layer in sorted({1, 2, h.layers // 2, h.layers}):
        raw = goo.read_layer(path, h, layer)
        pixels = goo.decode_layer(raw, h.resolution_x, h.resolution_y)
        assert goo.encode_layer(pixels) == raw, f"layer {layer}"


# --- trim_islands ---------------------------------------------------------------

BLOCK = (5, 15, 5, 15)
DOT = (20, 24, 30, 34)  # far from the block: nothing beneath it
BRIDGE = (12, 22, 14, 32)  # joins the block's area to the dot's


@pytest.fixture
def island_goo(tmp_path):
    """Layer 1 a block; 2 adds a floating dot; 3 the dot alone; 4 the dot bridged to the block."""
    path = tmp_path / "isl.goo"
    make_goo(
        path,
        [
            frame(BLOCK),
            frame(BLOCK, DOT),
            frame(BLOCK, DOT),
            frame(BLOCK, DOT, BRIDGE),
        ],
        width=W,
        height=H,
    )
    path.write_bytes(path.read_bytes() + END_MARKER)
    return path


def test_find_islands_sees_the_floating_dot(island_goo):
    # Layer 2's dot has nothing beneath it; on layer 3 it rests on layer 2's dot.
    assert goo_trim.find_islands(island_goo) == [(2, 16)]


def test_trim_drops_the_dot_until_it_meets_held_material(island_goo, tmp_path):
    out = tmp_path / "out.goo"
    report = goo_trim.trim_islands(island_goo, out)
    assert [r[0] for r in report.by_layer] == [2, 3]
    assert report.regions_dropped == 2 and report.pixels_dropped == 32
    assert not pixels_of(out, 2)[20:24, 30:34].any()
    assert not pixels_of(out, 3)[20:24, 30:34].any()
    # Bridged on layer 4, the dot is part of a held region and stays.
    assert (pixels_of(out, 4)[20:24, 30:34] == 255).all()
    assert goo_trim.find_islands(out) == []


def test_trim_copies_unchanged_layers_header_and_end_marker(island_goo, tmp_path):
    out = tmp_path / "out.goo"
    goo_trim.trim_islands(island_goo, out)
    a, b = island_goo.read_bytes(), out.read_bytes()
    h = goo.read_header(island_goo)
    assert b[: h.layer_table_offset] == a[: h.layer_table_offset]
    for layer in (1, 4):
        assert goo.read_layer(out, h, layer) == goo.read_layer(island_goo, h, layer)
    assert b.endswith(END_MARKER)
    assert goo.read_header(out).layers == 4


def test_trim_keeps_grey_edges_of_held_cores_and_drops_lone_grey(tmp_path):
    path = tmp_path / "g.goo"
    edge = ((5, 15, 15, 16), 90)  # anti-aliased edge right beside the block
    lone = ((25, 27, 35, 37), 200)  # a grey core with nothing beneath it
    faint = ((25, 27, 2, 4), 40)  # faint grey far from anything
    make_goo(
        path,
        [frame(BLOCK), frame(BLOCK, grey=[edge, lone, faint])],
        width=W,
        height=H,
    )
    out = tmp_path / "out.goo"
    goo_trim.trim_islands(path, out)
    px = pixels_of(out, 2)
    assert (px[5:15, 15] == 90).all()
    assert not px[25:27, 35:37].any() and not px[25:27, 2:4].any()


def test_trim_drops_everything_after_a_blank_layer(tmp_path):
    path = tmp_path / "b.goo"
    make_goo(path, [frame(BLOCK), frame(), frame(BLOCK)], width=W, height=H)
    out = tmp_path / "out.goo"
    report = goo_trim.trim_islands(path, out)
    assert report.by_layer == [(3, 1, 100)]
    assert not pixels_of(out, 3).any()


# --- the tool -------------------------------------------------------------------


def test_tool_writes_beside_the_input_and_reports(island_goo):
    result = asyncio.run(trim.trim_islands(str(island_goo)))
    assert result.output_path == str(island_goo.with_suffix(".trimmed.goo"))
    assert result.layers_changed == 2 and result.regions_dropped == 2
    assert result.islands_left == 0 and result.warnings == []
    assert result.dropped_mm3 == pytest.approx(32 * 0.018**2 * 0.05, abs=1e-3)


def test_tool_never_overwrites(island_goo):
    asyncio.run(trim.trim_islands(str(island_goo)))
    with pytest.raises(cli.CliError, match="already exists"):
        asyncio.run(trim.trim_islands(str(island_goo)))


def test_tool_refuses_non_goo(tmp_path):
    other = tmp_path / "x.goo"
    other.write_bytes(b"not a goo file at all")
    with pytest.raises(cli.CliError, match="not a GOO"):
        asyncio.run(trim.trim_islands(str(other)))

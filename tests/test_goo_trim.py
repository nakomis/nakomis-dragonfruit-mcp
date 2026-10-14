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


@pytest.fixture(autouse=True)
def _python_backend_by_default(monkeypatch):
    """Whatever the shell says, these tests choose their backend explicitly."""
    monkeypatch.delenv(trim.BACKEND_ENV, raising=False)


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
    """Layer 1 a block; 2 and 3 add a floating dot; 4 bridges the dot to the block."""
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


def test_trim_never_overwrites_and_cleans_up(island_goo, tmp_path):
    out = tmp_path / "out.goo"
    out.write_bytes(b"someone else's file")
    with pytest.raises(FileExistsError):
        goo_trim.trim_islands(island_goo, out)
    assert out.read_bytes() == b"someone else's file"
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


def test_trim_keeps_the_source_permissions(island_goo, tmp_path):
    island_goo.chmod(0o640)
    out = tmp_path / "out.goo"
    goo_trim.trim_islands(island_goo, out)
    assert out.stat().st_mode & 0o777 == 0o640


def test_tool_reports_a_file_that_appears_mid_run(island_goo, tmp_path, monkeypatch):
    out = tmp_path / "late.goo"
    real = goo_trim.trim_islands

    def racing(src, dst, *thresholds):
        dst.write_bytes(b"arrived first")
        return real(src, dst, *thresholds)

    monkeypatch.setattr(goo_trim, "trim_islands", racing)
    with pytest.raises(cli.CliError, match="appeared while trimming"):
        asyncio.run(trim.trim_islands(str(island_goo), str(out)))
    assert out.read_bytes() == b"arrived first"


def test_trim_drops_everything_after_a_blank_layer(tmp_path):
    path = tmp_path / "b.goo"
    make_goo(path, [frame(BLOCK), frame(), frame(BLOCK)], width=W, height=H)
    out = tmp_path / "out.goo"
    report = goo_trim.trim_islands(path, out)
    assert report.by_layer == [(3, 1, 100)]
    assert not pixels_of(out, 3).any()


# --- how much overlap holds a region up (NDFM-27) --------------------------------------
# Layer 1 is BLOCK (rows and columns 5..15); layer 2 is one region on its own,
# placed so that the given number of its pixels sit over the block.

OVERLAPS = {
    "1px-touch": ((14, 20, 14, 20), 1, 36),  # a corner pixel
    "2px-touch": ((14, 20, 13, 19), 2, 36),  # two pixels of an edge
    "30-percent": ((12, 22, 5, 15), 30, 100),
    "20-percent": ((13, 23, 5, 15), 20, 100),
    "tiny-full": ((8, 10, 8, 10), 4, 4),  # 4 px, all over the block
    "tiny-half": ((14, 16, 9, 11), 2, 4),  # 4 px, half over the block
}


def _overlap_goo(tmp_path: Path, region) -> Path:
    path = tmp_path / "overlap.goo"
    make_goo(path, [frame(BLOCK), frame(region)], width=W, height=H)
    return path


def _region_after_trim(tmp_path, name, **thresholds) -> tuple[goo_trim.TrimReport, np.ndarray]:
    region, overlap, size = OVERLAPS[name]
    r0, r1, c0, c1 = region
    a = np.zeros((H, W), bool)
    a[r0:r1, c0:c1] = True
    assert (a[5:15, 5:15].sum(), a.sum()) == (overlap, size), "the fixture is what it says"
    src = _overlap_goo(tmp_path, region)
    out = tmp_path / "out.goo"
    report = goo_trim.trim_islands(src, out, **thresholds)
    return report, pixels_of(out, 2)[r0:r1, c0:c1]


@pytest.mark.parametrize("name", ["1px-touch", "2px-touch", "20-percent", "tiny-half"])
def test_trim_drops_a_region_that_only_grazes_the_layer_below(tmp_path, name):
    report, px = _region_after_trim(tmp_path, name)
    assert not px.any()
    assert report.regions_dropped == 1 and report.pixels_dropped == OVERLAPS[name][2]


@pytest.mark.parametrize("name", ["30-percent", "tiny-full"])
def test_trim_keeps_a_region_with_real_overlap(tmp_path, name):
    report, px = _region_after_trim(tmp_path, name)
    assert (px == 255).all()
    assert report.by_layer == []


def test_trim_thresholds_are_parameters(tmp_path):
    # 20 % passes at 0.2; a 1 px touch passes when 1 px of any share is enough.
    report, px = _region_after_trim(tmp_path, "20-percent", min_overlap=0.2)
    assert (px == 255).all() and report.by_layer == []
    (tmp_path / "out.goo").unlink()
    report, px = _region_after_trim(tmp_path, "1px-touch", min_overlap=0.0, min_overlap_px=1)
    assert (px == 255).all() and report.by_layer == []
    (tmp_path / "out.goo").unlink()
    # ...and 30 % fails at 0.5, or when 31 px are asked for.
    report, px = _region_after_trim(tmp_path, "30-percent", min_overlap=0.5)
    assert not px.any()
    (tmp_path / "out.goo").unlink()
    report, px = _region_after_trim(tmp_path, "30-percent", min_overlap_px=31)
    assert not px.any()


def test_find_islands_uses_the_same_rule(tmp_path):
    for name, (region, _, size) in OVERLAPS.items():
        src = _overlap_goo(tmp_path, region)
        grazing = name in ("1px-touch", "2px-touch", "20-percent", "tiny-half")
        assert goo_trim.find_islands(src, workers=1) == ([(2, size)] if grazing else []), name
        loose = goo_trim.find_islands(src, workers=1, min_overlap=0.0, min_overlap_px=1)
        assert loose == [], name
        src.unlink()


@pytest.mark.parametrize(
    "thresholds, match",
    [
        ({"min_overlap": -0.1}, "between 0 and 1"),
        ({"min_overlap": 1.5}, "between 0 and 1"),
        ({"min_overlap": float("nan")}, "between 0 and 1"),
        ({"min_overlap_px": 0}, "at least 1"),
    ],
)
def test_bad_thresholds_are_refused(island_goo, tmp_path, thresholds, match):
    with pytest.raises(ValueError, match=match):
        goo_trim.trim_islands(island_goo, tmp_path / "out.goo", **thresholds)
    with pytest.raises(ValueError, match=match):
        goo_trim.find_islands(island_goo, **thresholds)
    with pytest.raises(cli.CliError, match=match):
        trim.run(str(island_goo), None, 18.0, True, **thresholds)
    assert not (tmp_path / "out.goo").exists()


def test_tool_schema_offers_the_thresholds_with_their_defaults():
    from nakomis_dragonfruit_mcp.app import mcp

    (tool,) = [t for t in asyncio.run(mcp.list_tools()) if t.name == "trim_islands"]
    props = tool.inputSchema["properties"]
    assert props["min_overlap"]["default"] == 0.25
    assert (props["min_overlap"]["minimum"], props["min_overlap"]["maximum"]) == (0.0, 1.0)
    assert props["min_overlap_px"]["default"] == 8 and props["min_overlap_px"]["minimum"] == 1


def test_tool_passes_the_thresholds_through(tmp_path):
    src = _overlap_goo(tmp_path, OVERLAPS["20-percent"][0])
    strict = asyncio.run(trim.trim_islands(str(src), str(tmp_path / "a.goo")))
    loose = asyncio.run(trim.trim_islands(str(src), str(tmp_path / "b.goo"), min_overlap=0.2))
    assert strict.regions_dropped == 1 and strict.islands_left == 0
    assert loose.regions_dropped == 0 and loose.islands_left == 0


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


@pytest.mark.parametrize("seed", range(5))
def test_fast_decoder_matches_the_run_by_run_one(seed):
    rng = np.random.default_rng(seed)
    runs = rng.integers(1, 5000, 300)
    runs[::17] = rng.integers(1 << 20, 1 << 21, len(runs[::17]))  # three length bytes
    values = rng.choice([0, 255, 1, 128, 254], 300)
    pixels = np.repeat(values, runs).astype(np.uint8).tobytes()
    data = goo.encode_layer(pixels)
    assert goo.decode_layer(data, len(pixels), 1) == goo._decode_slowly(data, len(pixels), 1)
    assert goo.decode_layer(data, len(pixels), 1) == pixels


# --- the backend switch -----------------------------------------------------------


def _no_binary(monkeypatch):
    def missing(name):
        raise cli.CliError(f"{name} not found")

    monkeypatch.setattr(cli, "find_binary", missing)


def _fake_binary(monkeypatch):
    monkeypatch.setattr(cli, "find_binary", lambda name: Path("/fake") / name)


def test_backend_defaults_to_python_even_when_built(monkeypatch):
    _fake_binary(monkeypatch)
    assert trim.choose_backend(None) == "python"


def test_backend_env_and_parameter(monkeypatch):
    _fake_binary(monkeypatch)
    monkeypatch.setenv(trim.BACKEND_ENV, "rust")
    assert trim.choose_backend(None) == "rust"
    assert trim.choose_backend("python") == "python"  # the parameter wins
    monkeypatch.setenv(trim.BACKEND_ENV, " Auto ")
    assert trim.choose_backend(None) == "rust"


def test_backend_auto_falls_back_but_rust_insists(monkeypatch):
    _no_binary(monkeypatch)
    assert trim.choose_backend("auto") == "python"
    with pytest.raises(cli.CliError, match="build-accel.sh"):
        trim.choose_backend("rust")
    monkeypatch.setenv(trim.BACKEND_ENV, "rust")
    with pytest.raises(cli.CliError, match="build-accel.sh"):
        trim.choose_backend(None)


def test_backend_unknown_is_refused():
    with pytest.raises(cli.CliError, match="unknown trim backend"):
        trim.choose_backend("fortran")


def test_tool_reports_the_python_backend(island_goo):
    result = asyncio.run(trim.trim_islands(str(island_goo), backend="python"))
    assert result.backend == "python" and result.regions_dropped == 2


def test_tool_maps_the_rust_output(island_goo, tmp_path, monkeypatch):
    _fake_binary(monkeypatch)
    calls = []

    def fake_run(binary, args, *, parse_json=False, timeout=0):
        calls.append((binary, args))
        data = {
            "layers": 4,
            "layers_changed": 2,
            "regions_dropped": 2,
            "pixels_dropped": 32,
            "by_layer": [[2, 1, 16], [3, 1, 16]],
            "islands_left": 3,
            "largest_island_left_px": 40,
        }
        return cli.CliResult(exe=Path(binary), args=args, stdout="", stderr="", data=data)

    monkeypatch.setattr(cli, "run", fake_run)
    out = tmp_path / "r.goo"
    result = asyncio.run(trim.trim_islands(str(island_goo), str(out), backend="rust"))
    assert calls == [
        (
            "goo-trim",
            [
                "trim",
                str(island_goo),
                str(out),
                "--min-overlap=0.25",
                "--min-overlap-px=8",
                "--verify",
            ],
        )
    ]
    assert result.backend == "rust" and result.worst_layers == [[2, 1, 16], [3, 1, 16]]
    assert (result.islands_left, result.largest_island_left_px) == (3, 40)
    assert result.warnings and "40 px" in result.warnings[0]


def test_tool_maps_a_rust_file_that_appears_mid_run(island_goo, tmp_path, monkeypatch):
    _fake_binary(monkeypatch)

    def fake_run(binary, args, **kw):
        raise cli.CliError(
            "goo-trim exited 1: goo-trim: x already exists (appeared while trimming)"
        )

    monkeypatch.setattr(cli, "run", fake_run)
    with pytest.raises(cli.CliError, match="appeared while trimming; nothing was overwritten"):
        asyncio.run(trim.trim_islands(str(island_goo), str(tmp_path / "x.goo"), backend="rust"))


def _goo_trim_built() -> bool:
    try:
        cli.find_binary(trim.GOO_TRIM)
    except cli.CliError:
        return False
    return True


def _fixtures(tmp_path: Path, island: Path) -> list[Path]:
    tmp_path.mkdir(exist_ok=True)
    grey = tmp_path / "grey.goo"
    edge = ((5, 15, 15, 16), 90)
    make_goo(
        grey,
        [frame(BLOCK), frame(BLOCK, grey=[edge, ((25, 27, 35, 37), 200), ((25, 27, 2, 4), 40)])],
        width=W,
        height=H,
    )
    blank = tmp_path / "blank.goo"
    make_goo(blank, [frame(), frame(BLOCK, DOT), frame(), frame(BLOCK)], width=W, height=H)
    grazes = []
    for name, (region, _, _) in OVERLAPS.items():
        path = tmp_path / f"{name}.goo"
        # Beside the block, so the layer also has held core and a hugging edge.
        make_goo(path, [frame(BLOCK), frame((5, 12, 5, 9), region)], width=W, height=H)
        grazes.append(path)
    return [island, grey, blank, *grazes]


@pytest.mark.skipif(not _goo_trim_built(), reason="run scripts/build-accel.sh to build goo-trim")
def test_rust_matches_python_byte_for_byte(island_goo, tmp_path):
    for i, (overlap, overlap_px) in enumerate([(0.25, 8), (0.2, 3)]):
        flags = [f"--min-overlap={overlap}", f"--min-overlap-px={overlap_px}"]
        for src in _fixtures(tmp_path / str(i), island_goo):
            out = tmp_path / str(i)
            py_out, rs_out = out / f"{src.stem}.py.goo", out / f"{src.stem}.rs.goo"
            kw = {"min_overlap": overlap, "min_overlap_px": overlap_px}
            py = trim.run(str(src), str(py_out), 18.0, True, backend="python", **kw)
            rs = trim.run(str(src), str(rs_out), 18.0, True, backend="rust", **kw)
            assert rs_out.read_bytes() == py_out.read_bytes(), src.name
            assert rs.backend == "rust" and py.backend == "python"
            assert rs.model_dump(exclude={"backend", "output_path"}) == py.model_dump(
                exclude={"backend", "output_path"}
            ), src.name
            args = ["islands", str(src), *flags]
            islands = cli.run(trim.GOO_TRIM, args, parse_json=True).data["islands"]
            assert [tuple(i) for i in islands] == goo_trim.find_islands(src, 1, **kw), src.name

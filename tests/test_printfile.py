import asyncio
import base64
import json
import shutil
import struct
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from nakomis_dragonfruit_mcp import cli, goo
from nakomis_dragonfruit_mcp.app import mcp
from nakomis_dragonfruit_mcp.tools import printfile, slicing
from tests import test_slicing as slicing_tests
from tests.test_cli import make_fake_binary


def rle(pixels: list[int]) -> bytes:
    """GOO layer data for a list of 0..255 pixels, in runs (black, grey, white)."""
    body = bytearray()
    i = 0
    while i < len(pixels):
        j = i
        while j < len(pixels) and pixels[j] == pixels[i]:
            j += 1
        length, value = j - i, pixels[i]
        kind = 0b00 if value == 0 else 0b11 if value == 255 else 0b01
        extra = [(length >> 4) >> (8 * k) & 0xFF for k in range(3, -1, -1)]
        while extra and extra[0] == 0:
            extra.pop(0)
        body.append(kind << 6 | len(extra) << 4 | length & 0x0F)
        if kind == 0b01:
            body.append(value)
        body += bytes(extra)
        i = j
    return b"\x55" + bytes(body) + bytes([~sum(body) & 0xFF])


def make_goo(path: Path, layers: list[list[int]], width=4, height=2, mirror=False) -> None:
    s = goo.SETTINGS_OFFSET
    head = bytearray(goo.HEADER_BYTES)
    head[:12] = b"V1.2" + goo.FILE_MAGIC
    head[92:104] = b"Test Printer"
    struct.pack_into(">I", head, s, len(layers))
    struct.pack_into(">HH", head, s + 4, width, height)
    head[s + 8] = int(mirror)
    struct.pack_into(">f", head, s + 22, 0.05)
    struct.pack_into(">f", head, s + 26, 2.5)
    struct.pack_into(">f", head, s + 59, 30.0)
    struct.pack_into(">I", head, s + 63, 3)
    struct.pack_into(">I", head, s + 136, 1234)
    struct.pack_into(">I", head, s + 160, goo.HEADER_BYTES)
    out = bytearray(head)
    for pixels in layers:
        data = rle(pixels)
        out += b"\0" * 64 + b"\r\n" + struct.pack(">I", len(data)) + data + b"\r\n"
    path.write_bytes(out)


LAYER_1 = [0, 255, 255, 0, 0, 128, 128, 0]
LAYER_2 = [255] * 8
LAYER_3 = [0] * 8


@pytest.fixture
def goo_file(tmp_path):
    path = tmp_path / "m.goo"
    make_goo(path, [LAYER_1, LAYER_2, LAYER_3])
    return path


def test_goo_header(goo_file):
    h = goo.read_header(goo_file)
    assert (h.version, h.machine_name, h.layers) == ("V1.2", "Test Printer", 3)
    assert (h.resolution_x, h.resolution_y, h.mirror_x) == (4, 2, False)
    assert (h.layer_height_mm, h.exposure_s, h.bottom_exposure_s) == (0.05, 2.5, 30.0)
    assert (h.bottom_layers, h.print_time_s) == (3, 1234)


@pytest.mark.parametrize("n, pixels", [(1, LAYER_1), (2, LAYER_2), (3, LAYER_3)])
def test_goo_layers_round_trip(goo_file, n, pixels):
    h = goo.read_header(goo_file)
    assert list(goo.decode_layer(goo.read_layer(goo_file, h, n), 4, 2)) == pixels


def test_goo_long_runs_use_extra_length_bytes():
    pixels = [255] * 300_000 + [0] * 5 + [7] * 70
    assert list(goo.decode_layer(rle(pixels), 300_075, 1)) == pixels


def test_goo_errors(goo_file, tmp_path):
    h = goo.read_header(goo_file)
    with pytest.raises(goo.GooError, match="out of range"):
        goo.read_layer(goo_file, h, 4)
    with pytest.raises(goo.GooError, match="0x55"):
        goo.decode_layer(b"\x00", 1, 1)
    good = bytearray(rle(LAYER_1))
    good[-1] ^= 1
    with pytest.raises(goo.GooError, match="checksum"):
        goo.decode_layer(bytes(good), 4, 2)
    with pytest.raises(goo.GooError, match="expected 8"):
        goo.decode_layer(rle([0] * 7), 4, 2)
    with pytest.raises(goo.GooError, match="too many"):
        goo.decode_layer(rle([0] * 9), 4, 2)
    step = bytes([0b10 << 6 | 1])
    with pytest.raises(goo.GooError, match="step"):
        goo.decode_layer(b"\x55" + step + bytes([~sum(step) & 0xFF]), 4, 2)
    junk = tmp_path / "junk.goo"
    junk.write_bytes(b"V1.2" + goo.FILE_MAGIC + b"\0" * goo.HEADER_BYTES)
    with pytest.raises(goo.GooError, match="does not make sense"):
        goo.read_header(junk)
    short = tmp_path / "short.goo"
    short.write_bytes((b"V1.2" + goo.FILE_MAGIC)[:12] + b"\0" * 100)
    with pytest.raises(goo.GooError, match="truncated"):
        goo.read_header(short)
    notgoo = tmp_path / "x.goo"
    notgoo.write_bytes(b"hello")
    with pytest.raises(goo.GooError, match="not a GOO"):
        goo.read_header(notgoo)
    truncated = tmp_path / "t.goo"
    truncated.write_bytes(goo_file.read_bytes()[: goo.HEADER_BYTES + 70])
    with pytest.raises(goo.GooError):
        goo.read_layer(truncated, h, 1)


def test_inspect_goo(goo_file):
    info = printfile.inspect_print(str(goo_file))
    assert (info.format, info.container, info.layers) == (".goo", "goo", 3)
    assert info.resolution_px == [4, 2] and info.layer_height_mm == 0.05
    assert info.estimated_print_time_s == 1234 and info.machine == "Test Printer"
    assert info.exposure_s == 2.5 and info.bottom_layers == 3


def test_ctb_is_reported_not_read(tmp_path):
    ctb = tmp_path / "m.ctb"
    ctb.write_bytes(struct.pack("<I", 0x12FD0107) + b"\0" * 100)
    info = printfile.inspect_print(str(ctb))
    assert info.container == "ctb" and info.layers is None
    assert "v5 encrypted" in info.warnings[0]
    with pytest.raises(cli.CliError, match="encrypted"):
        printfile.preview_layer(str(ctb), 1)


def test_unknown_and_missing_files(tmp_path):
    other = tmp_path / "x.bin"
    other.write_bytes(b"hello world, nothing here")
    with pytest.raises(cli.CliError, match="not a ZIP"):
        printfile.inspect_print(str(other))
    with pytest.raises(cli.CliError, match="not found"):
        printfile.inspect_print(str(tmp_path / "nope"))


def test_preview_goo(goo_file, tmp_path):
    out = tmp_path / "p.png"
    content = printfile.preview_layer(str(goo_file), 1, out_path=str(out))
    meta = json.loads(content[0])
    assert (meta["layer"], meta["layers"], meta["width"], meta["height"]) == (1, 3, 4, 2)
    assert meta["flipped_x"] is False
    assert any("no mirror flag" in w for w in meta["warnings"])
    assert list(Image.open(out).tobytes()) == LAYER_1
    assert content[1].data[:4] == b"\x89PNG"
    flipped = printfile.preview_layer(str(goo_file), 1, out_path=str(out), flip_x=True)
    assert json.loads(flipped[0])["flipped_x"] is True
    assert list(Image.open(out).tobytes()) == LAYER_1[:4][::-1] + LAYER_1[4:][::-1]


def test_preview_goo_default_path_and_downscale(goo_file):
    meta = json.loads(printfile.preview_layer(str(goo_file), 2, image_px=2)[0])
    assert meta["png_path"] == f"{goo_file}.layer-2.png"
    assert meta["image_px"] == [2, 1]


def test_preview_goo_mirrored_header_flips_by_default(tmp_path):
    path = tmp_path / "mm.goo"
    make_goo(path, [LAYER_1], mirror=True)
    out = tmp_path / "p.png"
    meta = json.loads(printfile.preview_layer(str(path), 1, out_path=str(out))[0])
    assert meta["flipped_x"] is True and meta["warnings"] == []


def test_preview_goo_bad_layer(goo_file):
    with pytest.raises(cli.CliError, match="out of range"):
        printfile.preview_layer(str(goo_file), 9)


# -- ZIP-based archives, with a fake dragonfruit-cli --------------------------------------------


@pytest.fixture
def bin_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("NDFM_BIN_DIR", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    return tmp_path


@pytest.fixture
def zip_file(tmp_path, bin_dir):
    path = tmp_path / "a.nanodlp"
    slicer = {
        "printer": {"name": "Athena 8K"},
        "material": {"normalExposureSec": 2.5, "bottomExposureSec": 28, "bottomLayerCount": 5},
        "effective": {
            "layerHeightMm": 0.1,
            "sourceResolutionX": 6,
            "sourceResolutionY": 2,
            "widthPx": 2,
            "heightPx": 2,
            "xPackingMode": "rgb8_div3",
            "mirrorX": True,
        },
    }
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("slicer.json", json.dumps(slicer))
        z.writestr("info.json", json.dumps([{"TotalSolidArea": 1000.0}, {"TotalSolidArea": 500.0}]))
        z.writestr("1.png", b"")
    # Left pixel R,G,B = 1,2,3 then 4,5,6 on row 0, and the reverse on row 1.
    packed = Image.new("RGB", (2, 2))
    packed.putdata([(1, 2, 3), (4, 5, 6), (6, 5, 4), (3, 2, 1)])
    png = tmp_path / "fake-layer.png"
    packed.save(png)
    make_fake_binary(
        bin_dir,
        "dragonfruit-cli",
        f"""
if [ "$1 $2" = "print inspect" ]; then
  echo '{{"numeric_layer_count": 2, "layer_count": 3, "total_entries": 9}}'
else
  while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done
  cp {png} "$out"
fi""",
    )
    return path


def test_inspect_zip(zip_file):
    info = printfile.inspect_print(str(zip_file))
    assert (info.container, info.layers, info.layer_height_mm) == ("zip", 2, 0.1)
    assert info.resolution_px == [6, 2] and info.machine == "Athena 8K"
    assert info.estimated_resin_ml == 0.15  # (1000 + 500) mm2 x 0.1 mm = 150 mm3
    assert info.mirror_x is True and info.details["x_packing"] == "rgb8_div3"
    assert info.estimated_print_time_s is None and info.warnings == []


def test_inspect_zip_without_dragonfruit_metadata(tmp_path, bin_dir):
    path = tmp_path / "plain.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("1.png", b"")
    make_fake_binary(bin_dir, "dragonfruit-cli", """echo '{"numeric_layer_count": 1}'""")
    info = printfile.inspect_print(str(path))
    assert info.layers == 1 and info.layer_height_mm is None
    assert "only the layer count" in info.warnings[0]


def test_inspect_zip_unexpected_output(zip_file, bin_dir):
    make_fake_binary(bin_dir, "dragonfruit-cli", "echo '{}'")
    with pytest.raises(cli.CliError, match="unexpected"):
        printfile.inspect_print(str(zip_file))


def test_preview_zip_unpacks_rgb_and_flips(zip_file, tmp_path):
    out = tmp_path / "z.png"
    meta = json.loads(printfile.preview_layer(str(zip_file), 1, out_path=str(out))[0])
    assert (meta["width"], meta["height"], meta["flipped_x"]) == (6, 2, True)
    # Unpacked row 0 is 1,2,3,4,5,6; flipped left-right it reads 6,5,4,3,2,1.
    assert list(Image.open(out).tobytes())[:6] == [6, 5, 4, 3, 2, 1]
    raw = printfile.preview_layer(str(zip_file), 1, out_path=str(out), flip_x=False)
    assert json.loads(raw[0])["flipped_x"] is False
    assert list(Image.open(out).tobytes())[:6] == [1, 2, 3, 4, 5, 6]


def test_preview_zip_layer_out_of_range(zip_file):
    with pytest.raises(cli.CliError, match="out of range"):
        printfile.preview_layer(str(zip_file), 3)


def test_preview_zip_other_packing_is_converted_with_warning(zip_file, tmp_path):
    with zipfile.ZipFile(zip_file, "w") as z:
        z.writestr("slicer.json", json.dumps({"effective": {"xPackingMode": "weird"}}))
    meta = json.loads(
        printfile.preview_layer(str(zip_file), 1, out_path=str(tmp_path / "w.png"))[0]
    )
    assert "converted to grey" in meta["warnings"][0]


def test_preview_returns_image_content_over_mcp(goo_file):
    content = asyncio.run(mcp.call_tool("preview_layer", {"print_path": str(goo_file), "layer": 1}))
    blocks = content[0] if isinstance(content, tuple) else content
    assert [b.type for b in blocks] == ["text", "image"]
    assert base64.b64decode(blocks[1].data)[:4] == b"\x89PNG"


# -- integration ------------------------------------------------------------------------------


def _have_cli() -> bool:
    try:
        cli.find_binary(cli.DRAGONFRUIT_CLI)
    except cli.CliError:
        return False
    return True


@pytest.mark.integration
@pytest.mark.skipif(not _have_cli(), reason="dragonfruit-cli not built")
def test_real_cli_inspects_what_it_sliced(tmp_path):
    # The cheapest real archive: the CLI's own zip reader on a zip we write.
    path = tmp_path / "x.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("1.png", b"")
        z.writestr("2.png", b"")
    assert printfile.inspect_print(str(path)).layers == 2


@pytest.mark.integration
@pytest.mark.skipif(
    not (_have_cli() and slicing_tests._engine_ready()),
    reason="built engine or test model not available",
)
@pytest.mark.parametrize("printer, fmt", [("mars5ultra", ".goo"), ("athena8k", None)])
def test_real_slice_inspect_and_preview(tmp_path, printer, fmt):
    model = tmp_path / slicing_tests.TEST_MODEL.name
    shutil.copy(slicing_tests.TEST_MODEL, model)
    sliced = slicing.slice(str(model), printer=printer, format=fmt, layer_height=0.2)
    assert sliced.layers == 352
    info = printfile.inspect_print(sliced.output_path)
    assert info.layers == 352 and info.layer_height_mm == 0.2
    assert info.resolution_px == sliced.resolution_px
    base, top = (json.loads(printfile.preview_layer(sliced.output_path, n)[0]) for n in (1, 352))
    base_png, top_png = (Image.open(m["png_path"]) for m in (base, top))
    assert base_png.size == tuple(sliced.resolution_px)
    base_area = sum(1 for v in base_png.tobytes() if v)
    top_area = sum(1 for v in top_png.tobytes() if v)
    assert base_area > 10 * top_area > 0

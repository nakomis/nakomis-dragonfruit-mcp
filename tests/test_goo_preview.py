"""Rendered previews (NDFM-14): goo_preview.render_and_write and its wiring into the tools."""

import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from nakomis_dragonfruit_mcp import cli, goo_preview
from nakomis_dragonfruit_mcp.goo_motion import _layer_definitions
from nakomis_dragonfruit_mcp.goo_preview import BIG, BIG_AT, SMALL, SMALL_AT
from nakomis_dragonfruit_mcp.tools import slicing, supports
from tests.conftest import write_minimal_goo, write_stl
from tests.test_cli import make_fake_binary
from tests.test_supports import SUMMARY

# Stands in for `dragonfruit-mcp-tools render`: logs its arguments, copies
# $FAKE_PNG to --out and prints a report.
FAKE_RENDER = r"""
echo "$*" >> "$FAKE_RENDER_LOG"
while [ $# -gt 0 ]; do
  case "$1" in --out) out="$2" ;; esac
  shift
done
cp "$FAKE_PNG" "$out"
echo '{"triangles": 2}'
"""


def slot(data: bytes, at: int, size: tuple[int, int]) -> np.ndarray:
    """A preview slot as (rows, cols, rgb) in 0-255, decoded from big-endian RGB565."""
    v = np.frombuffer(data[at : at + size[0] * size[1] * 2], dtype=">u2").reshape(size)
    r, g, b = (v >> 11) & 31, (v >> 5) & 63, v & 31
    return np.stack([r * 255 // 31, g * 255 // 63, b * 255 // 31], axis=-1)


def magenta_pixels(rgb: np.ndarray) -> int:
    r, g, b = rgb[..., 0].astype(int), rgb[..., 1].astype(int), rgb[..., 2].astype(int)
    return int(((r > 100) & (r > b) & (b > g + 20)).sum())


def blue_pixels(rgb: np.ndarray) -> int:
    r, g, b = rgb[..., 0].astype(int), rgb[..., 1].astype(int), rgb[..., 2].astype(int)
    return int(((b > 100) & (b > r + 60) & (b > g)).sum())


@pytest.fixture
def fake_render(tmp_path, monkeypatch):
    """A fake renderer that 'draws' a magenta square on the dark background."""
    bins = tmp_path / "render-bin"
    bins.mkdir()
    make_fake_binary(bins, cli.MCP_TOOLS, FAKE_RENDER)
    picture = tmp_path / "rendered.png"
    img = Image.new("RGB", (580, 580), goo_preview.BACKGROUND_RGB)
    img.paste(goo_preview.MODEL_RGB, (200, 150, 380, 430))
    img.save(picture)
    log = tmp_path / "render.log"
    monkeypatch.setenv("NDFM_BIN_DIR", str(bins))
    monkeypatch.setenv("FAKE_PNG", str(picture))
    monkeypatch.setenv("FAKE_RENDER_LOG", str(log))
    return log


@pytest.fixture
def goo_file(tmp_path):
    path = tmp_path / "print.goo"
    write_minimal_goo(path)
    return path


@pytest.mark.real_previews
def test_render_and_write_fills_both_slots(fake_render, goo_file, stl):
    before = goo_file.read_bytes()
    assert goo_preview.render_and_write(goo_file, stl, model_triangles=7) is True
    after = goo_file.read_bytes()
    assert (
        len(after) == len(before)
        and after[BIG_AT + BIG[0] * BIG[1] * 2 :] == before[BIG_AT + BIG[0] * BIG[1] * 2 :]
    )
    assert magenta_pixels(slot(after, SMALL_AT, SMALL)) > 1000
    assert magenta_pixels(slot(after, BIG_AT, BIG)) > 10000
    args = fake_render.read_text().split()
    assert args[0] == "render"
    assert args[args.index("--stl") + 1] == str(stl)
    assert args[args.index("--split") + 1] == "7"
    assert args[args.index("--size") + 1] == "580"
    assert args[args.index("--model-rgb") + 1] == "230,56,133"
    assert args[args.index("--support-rgb") + 1] == "64,128,242"
    assert args[args.index("--background") + 1] == "20,20,20"


@pytest.mark.real_previews
def test_without_a_split_everything_is_model(fake_render, goo_file, stl):
    assert goo_preview.render_and_write(goo_file, stl) is True
    assert "--split" not in fake_render.read_text().split()


@pytest.mark.real_previews
def test_other_formats_are_skipped_without_rendering(fake_render, tmp_path, stl):
    for name, data in (("p.ctb", b"ctb bytes"), ("p.goo", b"\0" * 300_000)):
        other = tmp_path / name
        other.write_bytes(data)
        assert goo_preview.render_and_write(other, stl) is False
        assert other.read_bytes() == data
    assert goo_preview.has_preview_slots(tmp_path / "missing.goo") is False
    assert not fake_render.exists()


@pytest.mark.real_previews
def test_a_failed_render_is_a_warning_and_leaves_the_file(tmp_path, goo_file, stl, monkeypatch):
    bins = tmp_path / "failing-bin"
    bins.mkdir()
    make_fake_binary(bins, cli.MCP_TOOLS, "echo 'error: the mesh has no triangles' >&2; exit 1")
    monkeypatch.setenv("NDFM_BIN_DIR", str(bins))
    before = goo_file.read_bytes()
    warnings: list[str] = []
    assert goo_preview.add_previews(goo_file, stl, warnings) is False
    assert goo_file.read_bytes() == before
    assert len(warnings) == 1 and "preview pictures could not be written" in warnings[0]
    assert "no triangles" in warnings[0]


# -- wiring: slice --------------------------------------------------------------------------


def test_slice_writes_previews_after_postprocess(fake_df, stl, preview_calls):
    result = slicing.run_slice(str(stl))
    assert result.previews_written is True
    assert len(preview_calls) == 1
    call = preview_calls[0]
    assert call["goo"] == Path(result.output_path) and call["stl"] == stl.resolve()
    assert call["model_triangles"] is None


def test_slice_previews_can_be_turned_off(fake_df, stl, preview_calls):
    result = slicing.run_slice(str(stl), previews=False)
    assert result.previews_written is False and preview_calls == []


def test_slice_to_another_format_writes_no_previews_and_no_warning(fake_df, stl, preview_calls):
    result = slicing.run_slice(str(stl), format="ctb")
    assert result.previews_written is False
    assert not any("preview" in w for w in result.warnings)


@pytest.mark.real_previews
def test_a_failed_render_does_not_fail_the_slice(fake_df, stl, tmp_path, monkeypatch):
    bins = tmp_path / "failing-bin"
    bins.mkdir()
    make_fake_binary(bins, cli.MCP_TOOLS, "echo boom >&2; exit 1")
    monkeypatch.setenv("NDFM_BIN_DIR", str(bins))
    result = slicing.run_slice(str(stl))
    assert result.previews_written is False
    failed = [w for w in result.warnings if "preview pictures could not be written" in w]
    assert len(failed) == 1 and "boom" in failed[0]
    # The motion fix still stands and the file still validates.
    data = bytearray(Path(result.output_path).read_bytes())
    assert len(_layer_definitions(data, Path(result.output_path))) == 1


@pytest.mark.real_previews
def test_slice_previews_end_to_end_with_a_fake_renderer(fake_df, stl, fake_render):
    result = slicing.run_slice(str(stl))
    assert result.previews_written is True
    data = Path(result.output_path).read_bytes()
    assert magenta_pixels(slot(data, BIG_AT, BIG)) > 10000
    assert len(_layer_definitions(bytearray(data), Path(result.output_path))) == 1


# -- wiring: auto_support_and_slice ----------------------------------------------------------


@pytest.fixture
def fake_supports(fake_df, tmp_path, monkeypatch):
    bins = tmp_path / "bin"
    bins.mkdir()
    make_fake_binary(bins, "dragonfruit-cli", "exit 0")
    make_fake_binary(bins, "dragonfruit-mcp-tools", "exit 0")
    monkeypatch.setenv("NDFM_BIN_DIR", str(bins))
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps(SUMMARY))
    monkeypatch.setenv("FAKE_SUMMARY", str(summary))
    return fake_df


def test_auto_support_previews_use_a_scratch_supported_stl(fake_supports, stl, preview_calls):
    result = supports.run_auto_support_and_slice(str(stl))
    assert result.previews_written is True
    assert len(preview_calls) == 1
    call = preview_calls[0]
    assert call["goo"] == Path(result.output_path)
    assert call["stl_existed"] and call["stl"].name == "preview.supported.stl"
    assert call["model_triangles"] == SUMMARY["model_triangles"]
    # Scratch only: removed afterwards and never reported as an output.
    assert not call["stl"].exists()
    assert result.supported_stl_path is None
    sidecar = json.loads(Path(result.sidecar_path).read_text())
    assert sidecar["supports"]["supported_stl"] is None


def test_auto_support_previews_use_the_exported_supported_stl(fake_supports, stl, preview_calls):
    supports.run_auto_support_and_slice(str(stl), export_supported_stl=True)
    call = preview_calls[0]
    assert call["stl"].name == "model-mars5ultra-supported.goo.supported.stl"
    assert call["stl"].is_file()


def test_auto_support_previews_can_be_turned_off(fake_supports, stl, preview_calls):
    result = supports.run_auto_support_and_slice(str(stl), previews=False)
    assert result.previews_written is False and preview_calls == []
    lines = [ln for ln in fake_supports.log.read_text().splitlines() if "autosupport" in ln]
    assert "--supported-stl" not in lines[0].split()


def test_auto_support_other_formats_write_no_scratch_stl(fake_supports, stl, preview_calls):
    result = supports.run_auto_support_and_slice(str(stl), format="ctb")
    assert result.previews_written is False and preview_calls == []
    lines = [ln for ln in fake_supports.log.read_text().splitlines() if "autosupport" in ln]
    assert "--supported-stl" not in lines[0].split()


def test_auto_support_without_a_supported_stl_warns(fake_supports, stl, monkeypatch):
    monkeypatch.setenv("FAKE_NO_SUPPORTED_STL", "1")
    result = supports.run_auto_support_and_slice(str(stl))
    assert result.previews_written is False
    assert any("no supported STL was written" in w for w in result.warnings)


# -- integration: the real renderer ----------------------------------------------------------


def _renderer_built() -> bool:
    try:
        cli.find_binary(cli.MCP_TOOLS)
    except cli.CliError:
        return False
    return True


@pytest.mark.integration
@pytest.mark.skipif(not _renderer_built(), reason="bin/dragonfruit-mcp-tools not built")
def test_real_render_of_a_supported_model(tmp_path, goo_file):
    # A 10 mm cube (model) standing on a wide thin slab (raft), in one STL.
    def box(x0, y0, z0, x1, y1, z1):
        c = [(x, y, z) for z in (z0, z1) for y in (y0, y1) for x in (x0, x1)]
        quads = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
        return [t for a, b, cc, d in quads for t in ((c[a], c[b], c[cc]), (c[a], c[cc], c[d]))]

    stl = tmp_path / "supported.stl"
    write_stl(stl, box(-5, -5, 2, 5, 5, 12) + box(-12, -12, 0, 12, 12, 2))
    assert goo_preview.render_and_write(goo_file, stl, model_triangles=12) is True
    data = goo_file.read_bytes()
    for at, size in ((SMALL_AT, SMALL), (BIG_AT, BIG)):
        rgb = slot(data, at, size)
        assert magenta_pixels(rgb) > size[0] * size[1] // 20, size
        assert blue_pixels(rgb) > size[0] * size[1] // 20, size
    assert len(_layer_definitions(bytearray(data), goo_file)) == 1

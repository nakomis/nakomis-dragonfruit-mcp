import json
import shutil
from pathlib import Path

import pytest

from nakomis_dragonfruit_mcp import cli, goo_preview
from nakomis_dragonfruit_mcp import stl as stl_io
from nakomis_dragonfruit_mcp.goo_motion import HEADER, _layer_definitions
from nakomis_dragonfruit_mcp.tools import slicing
from tests.conftest import write_stl

TEST_MODEL = Path(
    "/Users/martinmu_1/Pictures/falai-mcp/ndfm-logos/3d/ndfm-logo-3d-plain-mcp-vertical.stl"
)


def slice_lines(fake_df):
    return [
        line
        for line in fake_df.log.read_text().splitlines()
        if line.split()[1:3] == ["scene", "slice"]
    ]


def test_list_printers(fake_df):
    result = slicing.list_printers()
    by_name = {p.name: p for p in result.printers}
    mars = by_name["mars5ultra"]
    assert (mars.route, mars.preset_id, mars.output_format) == (
        "py",
        "elegoo-mars-5-ultra-ctb",
        ".goo",
    )
    assert mars.build_volume_mm == {"width": 153.36, "depth": 77.76, "height": 165}
    assert mars.selected and not by_name["athena8k"].selected
    assert by_name["athena8k"].route == "py"
    assert result.selected == "mars5ultra (from default)"
    assert result.failed == []


def test_list_printers_reports_failures_and_bad_default(fake_df, tmp_path, monkeypatch):
    drop = tmp_path / "d"
    drop.mkdir()
    (drop / "bad.py").write_text("def (:\n")
    monkeypatch.setenv("NDFM_PRINTERS_DIR", str(drop))
    monkeypatch.setenv("NDFM_PRINTER", "ghost")
    result = slicing.list_printers()
    assert [f.source.endswith("bad.py") for f in result.failed] == [True]
    assert any("'ghost'" in w for w in result.warnings)


def test_slice_default_printer(fake_df, stl):
    result = slicing.run_slice(str(stl))
    assert result.printer == "mars5ultra" and result.printer_chosen_by == "default"
    assert result.format == ".goo" and result.layers == 42
    assert result.output_path == str(stl.with_name("model-mars5ultra.goo"))
    # mars5ultra's driver post-processes .goo: the plate-motion fix is applied.
    data = Path(result.output_path).read_bytes()
    assert data[:4] == b"V1.2"
    assert data[HEADER["per_layer"][0]] == 0 and data[HEADER["delay_mode"][0]] == 1
    # The default .goo is derived from the .ctb preset: custom profile, no presetId.
    assert "presetId" not in result.profile
    assert result.profile["display"]["outputFormat"] == ".goo"
    assert fake_df.printer_json() == result.profile
    assert any("Supports are NOT included" in w for w in result.warnings)
    assert result.supports_included is False
    # create, add-model, list-models, transform-model, slice
    assert len(fake_df.log.read_text().splitlines()) == 5
    assert "--mesh-dir" in result.cli_args and "--layer-height" not in result.cli_args


def test_slice_places_model_on_plate_and_exports_plate_stl(fake_df, stl):
    result = slicing.run_slice(str(stl), export_plate_stl=True)
    assert result.plate_offset_mm == [-12.0, -24.0, -5.0]
    assert any(
        "transform-model" in line and "--position -12.0,-24.0,-5.0" in line
        for line in fake_df.log.read_text().splitlines()
    )
    assert result.plate_stl_path == result.output_path + ".plate.stl"
    assert result.plate_bbox_mm == {"min": [-2.0, -4.0, 0.0], "max": [2.0, 4.0, 4.0]}
    lo, hi = stl_io.bbox(Path(result.plate_stl_path))
    assert (lo, hi) == ([-2.0, -4.0, 0.0], [2.0, 4.0, 4.0])


def test_slice_without_export_has_no_plate_stl(fake_df, stl):
    result = slicing.run_slice(str(stl))
    assert result.plate_stl_path is None and result.plate_bbox_mm is None
    assert not Path(result.output_path + ".plate.stl").exists()


def test_slice_rejects_ascii_stl(fake_df, tmp_path):
    ascii_stl = tmp_path / "a.stl"
    ascii_stl.write_text("solid x\n" + "facet normal 0 0 1\n" * 10 + "endsolid x\n")
    with pytest.raises(cli.CliError, match="not a binary STL"):
        slicing.run_slice(str(ascii_stl))


def test_stl_errors(tmp_path):
    tiny = tmp_path / "tiny.stl"
    tiny.write_bytes(b"x")
    with pytest.raises(stl_io.StlError, match="too small"):
        stl_io.bbox(tiny)
    empty = tmp_path / "empty.stl"
    empty.write_bytes(b"\0" * 84)
    with pytest.raises(stl_io.StlError, match="no triangles"):
        stl_io.bbox(empty)


def test_slice_passes_options_through(fake_df, stl, tmp_path):
    material = tmp_path / "resin.json"
    material.write_text("{}")
    out = tmp_path / "out" / "x.ctb"
    result = slicing.run_slice(
        str(stl), layer_height=0.025, aa_preset="sharp", material=str(material), out_path=str(out)
    )
    (line,) = slice_lines(fake_df)
    assert "--layer-height 0.025" in line and "--aa-preset sharp" in line
    assert f"--material {material}" in line
    assert result.layer_height_mm == 0.025
    assert out.read_text() == "data"


def test_slice_goo_derives_custom_profile(fake_df, stl):
    for asked in (None, "goo"):
        result = slicing.run_slice(str(stl), format=asked)
        assert result.format == ".goo"
        assert result.output_path.endswith("model-mars5ultra.goo")
        sent = fake_df.printer_json()
        assert "presetId" not in sent
        assert sent["display"]["outputFormat"] == ".goo" and "formatVersion" not in sent["display"]


def test_slice_ctb_is_the_untouched_upstream_preset(fake_df, stl):
    result = slicing.run_slice(str(stl), format=".ctb")
    assert result.format == ".ctb" and result.output_path.endswith("model-mars5ultra.ctb")
    assert fake_df.printer_json() == {"presetId": "elegoo-mars-5-ultra-ctb"}  # v5enc, as upstream


def test_slice_warns_when_out_path_extension_disagrees(fake_df, stl, tmp_path):
    result = slicing.run_slice(str(stl), out_path=str(tmp_path / "x.ctb"))
    assert any("'.ctb'" in w and "'.goo'" in w for w in result.warnings)


def test_slice_athena_sidecar(fake_df, stl):
    result = slicing.run_slice(str(stl), printer="athena8k")
    assert result.format == ".nanodlp"
    sidecar = Path(result.output_path + ".json")
    assert result.extra_files == [str(sidecar)]
    recorded = json.loads(sidecar.read_text())
    assert recorded["printer"] == "athena8k"
    assert recorded["profile"] == {"presetId": "concepts3d-athena1-8k-nanodlp"}
    assert recorded["scene_slice_args"] == result.cli_args


def test_slice_with_py_drop_in_applies_its_behaviour(fake_df, stl, tmp_path, monkeypatch):
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "my_weird_printer.py").write_text(
        """
from nakomis_dragonfruit_mcp.printers import Printer

class MyWeirdPrinter(Printer):
    name = "myweirdprinter"
    preset_id = "elegoo-mars-5-ultra-ctb"
    def prepare(self, job):
        job.layer_height = job.options.get("lh", 0.04)
        return job
    def extra_slice_args(self, job):
        return ["--dither", "on"]
    def postprocess(self, out, job, run):
        renamed = out.with_name("weird.out")
        out.rename(renamed)
        return renamed
    def warnings(self):
        return ["weird printers are weird"]
"""
    )
    monkeypatch.setenv("NDFM_PRINTERS_DIR", str(drop))
    result = slicing.run_slice(str(stl), printer="myweirdprinter", options={"lh": 0.03})
    (line,) = slice_lines(fake_df)
    assert line.endswith("--layer-height 0.03 --dither on")
    assert result.output_path.endswith("weird.out") and Path(result.output_path).exists()
    assert "weird printers are weird" in result.warnings


def test_slice_with_lone_json_drop_in(fake_df, stl, tmp_path, monkeypatch):
    drop = tmp_path / "drop"
    drop.mkdir()
    profile = {
        "name": "Tiny",
        "display": {"resolutionX": 10, "resolutionY": 10, "outputFormat": ".foo"},
    }
    (drop / "tiny.json").write_text(json.dumps(profile))
    monkeypatch.setenv("NDFM_PRINTERS_DIR", str(drop))
    result = slicing.run_slice(str(stl), printer="tiny")
    assert result.format == ".foo" and result.printer == "tiny"
    assert fake_df.printer_json() == profile


def test_slice_printer_precedence(fake_df, stl, monkeypatch):
    monkeypatch.setenv("NDFM_PRINTER", "athena8k")
    result = slicing.run_slice(str(stl))
    assert (result.printer, result.printer_chosen_by) == ("athena8k", "$NDFM_PRINTER")
    result = slicing.run_slice(str(stl), printer="mars5ultra")
    assert (result.printer, result.printer_chosen_by) == ("mars5ultra", "argument")


@pytest.mark.parametrize(
    "kwargs, error",
    [
        ({"aa_preset": "crisp"}, ValueError),
        ({"layer_height": 0}, ValueError),
        ({"printer": "ghost"}, ValueError),
        ({"material": "/no/such.json"}, cli.CliError),
    ],
)
def test_slice_bad_arguments(fake_df, stl, kwargs, error):
    with pytest.raises(error):
        slicing.run_slice(str(stl), **kwargs)


def test_slice_missing_stl(fake_df, tmp_path):
    with pytest.raises(cli.CliError, match="STL not found"):
        slicing.run_slice(str(tmp_path / "nope.stl"))


def test_slice_missing_rust_link_says_how_to_fix(fake_df, stl):
    cli.ts_cli_rust_binary().unlink()
    with pytest.raises(cli.CliError, match="scripts/build.sh"):
        slicing.run_slice(str(stl))


def test_slice_unexpected_output_schema(fake_df, stl):
    tsx = fake_df.dir / "node_modules" / ".bin" / "tsx"
    tsx.write_text("#!/bin/sh\necho '{\"layers\": 1}'\n")
    with pytest.raises(cli.CliError, match="unexpected"):
        slicing.run_slice(str(stl))


def test_slice_output_missing(fake_df, stl):
    tsx = fake_df.dir / "node_modules" / ".bin" / "tsx"
    tsx.write_text(
        "#!/bin/sh\n"
        """if [ "$3" = list-models ]; then echo '{"models": [{"id": "m1"}]}'; fi\n"""
        """if [ "$3" = slice ]; then echo '{"output": "/nonexistent/x", "format": ".ctb", """
        """"layers": 1, "layer_height_mm": 0.05, "resolution_px": [1, 1]}'; fi\n"""
    )
    with pytest.raises(cli.CliError, match="does not exist"):
        slicing.run_slice(str(stl))


# -- integration: the real engine ------------------------------------------------------------


def _engine_ready() -> bool:
    try:
        cli.find_binary(cli.DRAGONFRUIT_CLI)
        cli.find_tsx()
    except cli.CliError:
        return False
    return cli.ts_cli_rust_binary().exists() and TEST_MODEL.exists()


integration = [
    pytest.mark.integration,
    pytest.mark.skipif(not _engine_ready(), reason="built engine or test model not available"),
]


@pytest.fixture
def real_model(tmp_path):
    # Slice a copy: outputs land next to the STL.
    copy = tmp_path / TEST_MODEL.name
    shutil.copy(TEST_MODEL, copy)
    return copy


@pytest.mark.integration
@pytest.mark.skipif(not _engine_ready(), reason="built engine or test model not available")
@pytest.mark.parametrize(
    "printer, format, expected",
    [
        ("mars5ultra", None, ".goo"),
        ("mars5ultra", ".ctb", ".ctb"),
        ("athena8k", None, ".nanodlp"),
    ],
)
def test_real_slice_of_test_model(real_model, monkeypatch, printer, format, expected):
    result = slicing.run_slice(
        str(real_model), printer=printer, format=format, export_plate_stl=True
    )
    assert result.format == expected
    assert result.layers == 1405
    assert result.layer_height_mm == 0.05
    assert Path(result.output_path).stat().st_size > 1_000_000
    # The test model is already centred on x/y and sits on z = 0.
    assert result.plate_bbox_mm["max"][2] == pytest.approx(70.2033, abs=1e-3)
    assert result.plate_offset_mm[2] == 0 and abs(result.plate_offset_mm[0]) < 0.01
    # NDFM-14: a .goo gets rendered previews (magenta model); other formats are left alone.
    assert result.previews_written is (expected == ".goo")
    if expected == ".goo":
        from tests.test_goo_preview import magenta_pixels, slot

        data = Path(result.output_path).read_bytes()
        assert magenta_pixels(slot(data, goo_preview.BIG_AT, goo_preview.BIG)) > 5000
        # Written after mars5ultra's motion fix, and the file still validates.
        assert data[HEADER["per_layer"][0]] == 0
        assert len(_layer_definitions(bytearray(data), Path(result.output_path))) == 1405


# -- placement, fit, hooks, sidecar, async -----------------------------------------------------


def test_place_on_plate_false_slices_as_positioned(fake_df, stl):
    result = slicing.run_slice(str(stl), place_on_plate=False, export_plate_stl=True)
    assert result.plate_offset_mm == [0.0, 0.0, 0.0]
    assert not any("transform-model" in line for line in fake_df.log.read_text().splitlines())
    assert result.plate_bbox_mm == {"min": [10.0, 20.0, 5.0], "max": [14.0, 28.0, 9.0]}


def test_warns_when_model_exceeds_build_volume(fake_df, tmp_path):
    big = tmp_path / "big.stl"
    write_stl(big, [((0, 0, 0), (200, 0, 0), (200, 10, 200))])  # 200 wide, 200 tall
    result = slicing.run_slice(str(big))
    (warning,) = [w for w in result.warnings if "does not fit" in w]
    assert "x -100.0..100.0" in warning and "exceeds +/-76.7" in warning
    assert "height 200.0 mm exceeds 165.0" in warning and "y " not in warning


def test_no_fit_warning_when_it_fits(fake_df, stl):
    assert not any("does not fit" in w for w in slicing.run_slice(str(stl)).warnings)


def test_sidecar_is_written_beside_the_print(fake_df, stl):
    result = slicing.run_slice(str(stl), export_plate_stl=True)
    sidecar = Path(result.output_path + ".ndfm.json")
    assert result.sidecar_path == str(sidecar)
    data = json.loads(sidecar.read_text())
    assert data["printer"] == "mars5ultra" and data["layers"] == 42
    assert data["profile"]["mirrorX"] is True and data["profile"]["outputFormat"] == ".goo"
    assert data["profile"]["basePresetId"] == "elegoo-mars-5-ultra-ctb"
    assert data["profile"]["resolutionX"] == 8520 and data["profile"]["pixelSize"]["x"] == 18
    assert data["profile"]["formatVersion"] is None
    assert data["plate_offset_mm"] == [-12.0, -24.0, -5.0]
    assert data["plate_stl"]["path"] == result.plate_stl_path
    assert data["tool_version"] and data["supports_included"] is False


def test_sidecar_keeps_format_version_for_the_untouched_preset(fake_df, stl):
    result = slicing.run_slice(str(stl), format=".ctb")
    data = json.loads(Path(result.sidecar_path).read_text())
    assert data["profile"]["formatVersion"] == "v5enc" and data["profile"]["outputFormat"] == ".ctb"


def test_plate_stl_failure_is_a_warning(fake_df, stl, monkeypatch):
    def boom(*args):
        raise OSError("disk full")

    monkeypatch.setattr(stl_io, "write_translated", boom)
    result = slicing.run_slice(str(stl), export_plate_stl=True)
    assert result.plate_stl_path is None
    assert any("plate STL could not be written: disk full" in w for w in result.warnings)


@pytest.mark.parametrize("hook", ["prepare", "extra_slice_args", "postprocess", "warnings"])
def test_failing_hook_names_the_plugin_and_hook(fake_df, stl, tmp_path, monkeypatch, hook):
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "bad.py").write_text(
        f"""
from nakomis_dragonfruit_mcp.printers import Printer

class Bad(Printer):
    name = "bad"
    preset_id = "elegoo-mars-5-ultra-ctb"
    def {hook}(self, *args):
        raise RuntimeError("kaput")
"""
    )
    monkeypatch.setenv("NDFM_PRINTERS_DIR", str(drop))
    with pytest.raises(cli.CliError, match=f"plugin 'bad' hook {hook} failed: RuntimeError: kaput"):
        slicing.run_slice(str(stl), printer="bad")


def test_list_printers_survives_a_misbehaving_driver(fake_df, tmp_path, monkeypatch):
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "bad.py").write_text(
        """
from nakomis_dragonfruit_mcp.printers import Printer

class Bad(Printer):
    name = "bad"
    preset_id = "elegoo-mars-5-ultra-ctb"
    def warnings(self):
        raise RuntimeError("kaput")
"""
    )
    monkeypatch.setenv("NDFM_PRINTERS_DIR", str(drop))
    result = slicing.list_printers()
    assert "bad" not in [p.name for p in result.printers]
    assert "mars5ultra" in [p.name for p in result.printers]
    assert [f.error for f in result.failed] == ["RuntimeError: kaput"]


def test_list_printers_warns_of_unknown_preset_without_display(fake_df, tmp_path, monkeypatch):
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "ghost.json").write_text(json.dumps({"presetId": "no-such-preset"}))
    monkeypatch.setenv("NDFM_PRINTERS_DIR", str(drop))
    result = slicing.list_printers()
    assert any("ghost" in w and "no-such-preset" in w for w in result.warnings)


def test_suffix_follows_a_format_changed_by_prepare(fake_df, stl, tmp_path, monkeypatch):
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "chg.py").write_text(
        """
from nakomis_dragonfruit_mcp.printers import Printer

class Chg(Printer):
    name = "chg"
    preset_id = "elegoo-mars-5-ultra-ctb"
    def prepare(self, job):
        job.format = ".goo"
        return job
"""
    )
    monkeypatch.setenv("NDFM_PRINTERS_DIR", str(drop))
    result = slicing.run_slice(str(stl), printer="chg")
    assert result.output_path.endswith("model-chg.goo")
    assert not any("out_path ends" in w for w in result.warnings)


def test_binary_stl_with_trailing_bytes_is_explained(fake_df, stl):
    stl.write_bytes(stl.read_bytes() + b"junk")
    with pytest.raises(cli.CliError, match="4 bytes follow the 2 declared"):
        slicing.run_slice(str(stl))


def test_slice_tool_is_async_and_returns_a_result(fake_df, stl):
    import asyncio

    result = asyncio.run(slicing.slice(str(stl)))
    assert result.layers == 42 and Path(result.output_path).exists()

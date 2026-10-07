import json
import shutil
from pathlib import Path

import pytest

from nakomis_dragonfruit_mcp import cli
from nakomis_dragonfruit_mcp.tools import slicing

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
        "json",
        "elegoo-mars-5-ultra-ctb",
        ".ctb",
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
    result = slicing.slice(str(stl))
    assert result.printer == "mars5ultra" and result.printer_chosen_by == "default"
    assert result.format == ".ctb" and result.layers == 42
    assert result.output_path == str(stl.with_name("model-mars5ultra.ctb"))
    assert Path(result.output_path).read_text() == "data"
    assert result.profile == {"presetId": "elegoo-mars-5-ultra-ctb"}
    assert fake_df.printer_json() == {"presetId": "elegoo-mars-5-ultra-ctb"}
    assert any("Supports are NOT included" in w for w in result.warnings)
    assert result.supports_included is False
    # create, add-model, slice
    assert len(fake_df.log.read_text().splitlines()) == 3
    assert "--mesh-dir" in result.cli_args and "--layer-height" not in result.cli_args


def test_slice_passes_options_through(fake_df, stl, tmp_path):
    material = tmp_path / "resin.json"
    material.write_text("{}")
    out = tmp_path / "out" / "x.ctb"
    result = slicing.slice(
        str(stl), layer_height=0.025, aa_preset="sharp", material=str(material), out_path=str(out)
    )
    (line,) = slice_lines(fake_df)
    assert "--layer-height 0.025" in line and "--aa-preset sharp" in line
    assert f"--material {material}" in line
    assert result.layer_height_mm == 0.025
    assert out.read_text() == "data"


def test_slice_goo_derives_custom_profile(fake_df, stl):
    result = slicing.slice(str(stl), format="goo")
    assert result.format == ".goo"
    assert result.output_path.endswith("model-mars5ultra.goo")
    sent = fake_df.printer_json()
    assert "presetId" not in sent
    assert sent["display"]["outputFormat"] == ".goo" and "formatVersion" not in sent["display"]


def test_slice_warns_when_out_path_extension_disagrees(fake_df, stl, tmp_path):
    result = slicing.slice(str(stl), out_path=str(tmp_path / "x.goo"))
    assert any("'.goo'" in w and "'.ctb'" in w for w in result.warnings)


def test_slice_athena_sidecar(fake_df, stl):
    result = slicing.slice(str(stl), printer="athena8k")
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
    result = slicing.slice(str(stl), printer="myweirdprinter", options={"lh": 0.03})
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
    result = slicing.slice(str(stl), printer="tiny")
    assert result.format == ".foo" and result.printer == "tiny"
    assert fake_df.printer_json() == profile


def test_slice_printer_precedence(fake_df, stl, monkeypatch):
    monkeypatch.setenv("NDFM_PRINTER", "athena8k")
    result = slicing.slice(str(stl))
    assert (result.printer, result.printer_chosen_by) == ("athena8k", "$NDFM_PRINTER")
    result = slicing.slice(str(stl), printer="mars5ultra")
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
        slicing.slice(str(stl), **kwargs)


def test_slice_missing_stl(fake_df, tmp_path):
    with pytest.raises(cli.CliError, match="STL not found"):
        slicing.slice(str(tmp_path / "nope.stl"))


def test_slice_missing_rust_link_says_how_to_fix(fake_df, stl):
    cli.ts_cli_rust_binary().unlink()
    with pytest.raises(cli.CliError, match="scripts/build.sh"):
        slicing.slice(str(stl))


def test_slice_unexpected_output_schema(fake_df, stl):
    tsx = fake_df.dir / "node_modules" / ".bin" / "tsx"
    tsx.write_text("#!/bin/sh\necho '{\"layers\": 1}'\n")
    with pytest.raises(cli.CliError, match="unexpected"):
        slicing.slice(str(stl))


def test_slice_output_missing(fake_df, stl):
    tsx = fake_df.dir / "node_modules" / ".bin" / "tsx"
    tsx.write_text(
        '#!/bin/sh\necho \'{"output": "/nonexistent/x", "format": ".ctb", "layers": 1,'
        ' "layer_height_mm": 0.05, "resolution_px": [1, 1]}\'\n'
    )
    with pytest.raises(cli.CliError, match="does not exist"):
        slicing.slice(str(stl))


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
        ("mars5ultra", None, ".ctb"),
        ("mars5ultra", ".goo", ".goo"),
        ("athena8k", None, ".nanodlp"),
    ],
)
def test_real_slice_of_test_model(real_model, monkeypatch, printer, format, expected):
    # Undo the isolation fixture's HOME change only where the real binaries need nothing from it.
    result = slicing.slice(str(real_model), printer=printer, format=format)
    assert result.format == expected
    assert result.layers == 1405
    assert result.layer_height_mm == 0.05
    assert Path(result.output_path).stat().st_size > 1_000_000

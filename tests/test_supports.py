import json
import os
import shutil
from pathlib import Path

import pytest

from nakomis_dragonfruit_mcp import cli
from nakomis_dragonfruit_mcp.tools import supports
from tests.test_cli import make_fake_binary

# The integration tests need a real model: set NDFM_TEST_STL to a binary STL
# that fits the Mars 5 Ultra (the project's own is the 70 mm logo model).
TEST_STL = Path(os.environ["NDFM_TEST_STL"]) if os.environ.get("NDFM_TEST_STL") else None

SUMMARY = {
    "printer": {
        "preset_id": None,
        "name": "Mars 5 Ultra",
        "output_format": ".goo",
        "material": "Standard 405nm",
        "layer_height_mm": 0.05,
    },
    "lift_mm": 7,
    "model_bbox_mm": {"min": [-2, -4, 7], "max": [2, 4, 11]},
    "islands": 14,
    "islands_by_source": {"voxel": 9, "overhang": 5},
    "placed_by_type": {"trunk": 91, "branch": 36, "leaf": 80, "twig": 42},
    "contacts": 286,
    "roots": 91,
    "islands_covered": 11,
    "islands_uncovered": 3,
    "area_coverage": 1.27,
    "raft": "solid",
    "height_mm": 11,
    "build_height_mm": 165,
    "model_triangles": 2,
    "support_triangles": 140812,
    "plate_transform": {"translate_mm": [-12, -24, 2], "rotation": None, "scale": 1},
    "layer_frame": {
        "source_width_px": 8520,
        "source_height_px": 4320,
        "width_px": 8520,
        "height_px": 4320,
        "x_packing_mode": "none",
        "build_width_mm": 153.36,
        "build_depth_mm": 77.76,
        "layer_height_mm": 0.05,
        "mirror_x": True,
        "mirror_y": False,
    },
    "supported_stl": None,
    "overwritten": [],
    "output": "__OUT__",
    "slice": {
        "layers": 220,
        "format": ".goo",
        "layer_height_mm": 0.05,
        "resolution_px": [8520, 4320],
    },
    "timings_ms": {"islands_ms": 4317, "auto_place_ms": 6559, "slice_ms": 42748},
    "warnings": ["3 of 14 islands have no support near them"],
}


@pytest.fixture
def fake_env(fake_df, tmp_path, monkeypatch):
    """The shared fake DragonFruit, plus fake binaries and the summary our script prints."""
    bins = tmp_path / "bin"
    bins.mkdir()
    make_fake_binary(bins, "dragonfruit-cli", "exit 0")
    make_fake_binary(bins, "dragonfruit-mcp-tools", "exit 0")
    monkeypatch.setenv("NDFM_BIN_DIR", str(bins))
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps(SUMMARY))
    monkeypatch.setenv("FAKE_SUMMARY", str(summary))
    fake_df.summary = summary
    return fake_df


def script_args(fake_df) -> list[str]:
    lines = [ln for ln in fake_df.log.read_text().splitlines() if "autosupport-slice.ts" in ln]
    assert len(lines) == 1
    return lines[0].split()


def run(stl, **kwargs):
    return supports.run_auto_support_and_slice(str(stl), **kwargs)


def test_maps_summary_and_uses_the_default_printer(fake_env, stl):
    result = run(stl)
    assert result.printer == "mars5ultra" and result.printer_chosen_by == "default"
    assert result.output_path == str(stl.with_name("model-mars5ultra-supported.goo"))
    assert result.format == ".goo"
    assert result.supports_by_type == {"trunk": 91, "branch": 36, "leaf": 80, "twig": 42}
    assert result.contacts == 286 and result.islands_covered == 11
    assert result.area_coverage == 1.27
    assert result.layers == 220
    assert result.layer_frame.mirror_x is True
    assert result.plate_offset_mm == [-12.0, -24.0, 2.0]
    assert result.warnings == ["3 of 14 islands have no support near them"]
    # mars5ultra defaults to .goo: a custom profile derived from the .ctb preset.
    profile = fake_env.printer_json()
    assert profile["display"]["outputFormat"] == ".goo"
    assert "presetId" not in profile


def test_sidecar_records_supports_and_mirroring(fake_env, stl):
    result = run(stl)
    sidecar = json.loads(Path(result.sidecar_path).read_text())
    assert result.sidecar_path == result.output_path + ".ndfm.json"
    assert sidecar["supports_included"] is True
    assert sidecar["profile"]["mirrorX"] is True
    assert sidecar["supports"]["contacts"] == 286
    assert sidecar["supports"]["lift_mm"] == 7
    assert sidecar["plate_offset_mm"] == [-12.0, -24.0, 2.0]
    assert sidecar["layers"] == 220


def test_default_arguments(fake_env, stl):
    run(stl)
    args = script_args(fake_env)
    assert args[0] == str(supports.SCRIPT)
    assert args[args.index("--raft") + 1] == "solid"
    assert args[args.index("--cli") + 1].endswith("/bin/dragonfruit-cli")
    assert args[args.index("--tools") + 1].endswith("/bin/dragonfruit-mcp-tools")
    for flag in ("--material", "--lift-mm", "--density", "--supported-stl", "--coarse-islands"):
        assert flag not in args


def test_options_are_passed_through(fake_env, stl, tmp_path):
    material = tmp_path / "material.json"
    material.write_text('{"name": "known good", "bottomExposureSec": 40}')
    out = tmp_path / "out" / "print.ctb"
    result = run(
        stl,
        format="ctb",
        material=str(material),
        layer_height=0.03,
        aa_preset="sharp",
        out_path=str(out),
        lift_mm=5,
        density=2,
        raft=False,
        export_supported_stl=True,
        fast_islands=True,
    )
    args = script_args(fake_env)
    assert args[args.index("--out") + 1] == str(out)
    assert args[args.index("--layer-height") + 1] == "0.03"
    assert args[args.index("--aa-preset") + 1] == "sharp"
    assert args[args.index("--lift-mm") + 1] == "5"
    assert args[args.index("--density") + 1] == "2"
    assert args[args.index("--raft") + 1] == "off"
    assert args[args.index("--supported-stl") + 1] == str(out) + ".supported.stl"
    assert "--coarse-islands" in args
    assert json.loads(Path(f"{fake_env.log}.material").read_text())["bottomExposureSec"] == 40
    # .ctb is the preset's own format: the preset reference goes through unchanged.
    assert fake_env.printer_json() == {"presetId": "elegoo-mars-5-ultra-ctb"}
    assert result.output_path == str(out)


def test_plate_stl_uses_slices_frame(fake_env, stl):
    result = run(stl, export_plate_stl=True)
    plate = Path(result.plate_stl_path)
    assert plate.name == "model-mars5ultra-supported.goo.plate.stl"
    from nakomis_dragonfruit_mcp import stl as stl_io

    lo, hi = stl_io.bbox(plate)
    # The fixture STL spans x 10..14, y 20..28, z 5..9; moved by the reported offset.
    assert lo == [-2.0, -4.0, 7.0] and hi == [2.0, 4.0, 11.0]


def test_a_mismatched_extension_is_refused_before_any_work(fake_env, stl, tmp_path):
    with pytest.raises(cli.CliError, match="writes '.goo'"):
        run(stl, out_path=str(tmp_path / "x.ctb"))
    assert not fake_env.log.exists()


def test_no_supports_is_a_warning(fake_env, stl):
    fake_env.summary.write_text(json.dumps({**SUMMARY, "contacts": 0, "warnings": []}))
    assert run(stl).warnings == ["no supports were placed: the print will have none"]


def test_unexpected_output_raises(fake_env, stl):
    fake_env.summary.write_text('{"islands": 3}')
    with pytest.raises(cli.CliError, match="unexpected"):
        run(stl)


def test_missing_output_raises(fake_env, stl):
    fake_env.summary.write_text(json.dumps({**SUMMARY, "output": "/nonexistent/x.goo"}))
    with pytest.raises(cli.CliError, match="does not exist"):
        run(stl)


def test_missing_stl_and_material_raise(fake_env, stl, tmp_path):
    with pytest.raises(cli.CliError, match="STL not found"):
        run(tmp_path / "nope.stl")
    with pytest.raises(cli.CliError, match="material profile not found"):
        run(stl, material=str(tmp_path / "nope.json"))


@pytest.mark.parametrize(
    "kwargs", [{"density": 0}, {"lift_mm": -1}, {"layer_height": 0}, {"aa_preset": "fuzzy"}]
)
def test_bad_values_raise(fake_env, stl, kwargs):
    with pytest.raises(ValueError):
        run(stl, **kwargs)


def test_the_tool_runs_off_the_event_loop(fake_env, stl):
    import asyncio

    result = asyncio.run(supports.auto_support_and_slice(str(stl)))
    assert result.contacts == 286


KEEP_OUT_RESULT = {
    "hole": 0, "purpose": "suction_relief", "start_plate_mm": [-14.2, -9.8, 7.1],
    "direction": [0, 0, -1], "keep_out_radius_mm": 3.0, "blocked_triangles": 41,
    "islands_inside": [], "contacts_removed": 1, "supports_removed": 2, "lost_coverage": [],
}  # fmt: skip

BASE_HOLE = {
    "x": -2.193, "y": -5.822, "z": 2.619, "radius_mm": 2.0,
    "direction": [0, 0, -1], "axis": "z", "length_mm": 3.6,
    "purpose": "suction_relief", "cavity": 0,
}  # fmt: skip


def keep_out_args(fake_env) -> dict:
    return json.loads(Path(f"{fake_env.log}.keepout").read_text())


def test_no_holes_file_means_no_keep_out(fake_env, stl):
    result = run(stl)
    assert "--keep-out" not in script_args(fake_env)
    assert result.keep_out is None


def test_holes_sidecar_is_passed_in_the_stl_frame(fake_env, stl):
    stl.with_name(stl.name + ".holes.json").write_text(
        json.dumps({"holes": [BASE_HOLE], "source_stl": "model.stl"})
    )
    fake_env.summary.write_text(
        json.dumps({**SUMMARY, "keep_out": {"source": "x", "holes": [KEEP_OUT_RESULT]}})
    )
    result = run(stl)
    args = script_args(fake_env)
    assert "--keep-out" in args
    # Untransformed: the script applies the plate translation, as it does to the model.
    sent = keep_out_args(fake_env)["holes"][0]
    assert (sent["x"], sent["y"], sent["z"]) == (-2.193, -5.822, 2.619)
    assert sent["radius_mm"] == 2.0 and sent["direction"] == [0, 0, -1] and sent["length_mm"] == 3.6
    assert result.keep_out.source == str(stl.with_name(stl.name + ".holes.json"))
    assert result.keep_out.holes[0].supports_removed == 2


def test_holes_argument_overrides_the_sidecar(fake_env, stl):
    stl.with_name(stl.name + ".holes.json").write_text(json.dumps({"holes": [BASE_HOLE]}))
    other = {**BASE_HOLE, "x": 9.5, "radius_mm": 1.5}
    fake_env.summary.write_text(
        json.dumps({**SUMMARY, "keep_out": {"source": "x", "holes": [KEEP_OUT_RESULT]}})
    )
    result = run(stl, holes=[other])
    sent = keep_out_args(fake_env)["holes"]
    assert len(sent) == 1 and sent[0]["x"] == 9.5 and sent[0]["radius_mm"] == 1.5
    assert result.keep_out.source == "the holes argument"


def test_keep_out_can_be_switched_off(fake_env, stl):
    stl.with_name(stl.name + ".holes.json").write_text(json.dumps({"holes": [BASE_HOLE]}))
    run(stl, keep_out_holes=False)
    assert "--keep-out" not in script_args(fake_env)
    run(stl, keep_out_holes=False, holes=[BASE_HOLE])
    assert all("--keep-out" not in ln for ln in fake_env.log.read_text().splitlines())


@pytest.mark.parametrize(
    "bad",
    [{**BASE_HOLE, "radius_mm": 0}, {**BASE_HOLE, "direction": [0, 0, 0]}, {"x": 1}],
)
def test_bad_holes_raise_before_any_work(fake_env, stl, bad):
    with pytest.raises(ValueError, match="hole 0"):
        run(stl, holes=[bad])
    assert not fake_env.log.exists()


def test_a_broken_holes_file_raises(fake_env, stl):
    stl.with_name(stl.name + ".holes.json").write_text("{nope")
    with pytest.raises(cli.CliError, match="not readable JSON"):
        run(stl)
    stl.with_name(stl.name + ".holes.json").write_text('{"holes": 3}')
    with pytest.raises(cli.CliError, match="'holes' list"):
        run(stl)


def _real_pipeline_available() -> bool:
    if TEST_STL is None or not TEST_STL.exists():
        return False
    try:
        cli.find_binary(cli.DRAGONFRUIT_CLI)
        cli.find_binary(cli.MCP_TOOLS)
        cli.find_tsx()
    except cli.CliError:
        return False
    generated = cli.dragonfruit_dir() / "src" / "supports" / "generatedSupportRegistrations.ts"
    return generated.exists()


needs_pipeline = pytest.mark.skipif(
    not _real_pipeline_available(),
    reason="needs NDFM_TEST_STL, bin/, DragonFruit's node_modules and generated registrations",
)


def _run_script(tmp_path: Path, *args: str) -> dict:
    """The TS script directly, for what the tool does not expose (--no-slice, --job-dir)."""
    printer = tmp_path / "printer.json"
    printer.write_text('{"presetId": "elegoo-mars-5-ultra-ctb"}')
    base = [
        "--stl", str(TEST_STL),
        "--tools", str(cli.find_binary(cli.MCP_TOOLS)),
        "--cli", str(cli.find_binary(cli.DRAGONFRUIT_CLI)),
        "--printer-json", str(printer),
    ]  # fmt: skip
    return cli.run_ts([*base, *args], script=supports.SCRIPT, parse_json=True).data


@pytest.mark.integration
@needs_pipeline
def test_real_auto_support_and_slice(tmp_path):
    # A copy, so the default outputs land in tmp_path rather than beside the original.
    stl = tmp_path / TEST_STL.name
    shutil.copy(TEST_STL, stl)
    result = supports.run_auto_support_and_slice(str(stl), export_plate_stl=True)
    out = Path(result.output_path)
    assert out.stat().st_size > 0
    assert out.suffix == ".goo" and result.printer == "mars5ultra"
    assert result.islands_by_source.get("overhang", 0) > 0
    assert result.contacts > 50
    assert result.support_triangles > 0
    assert result.layers > 1400
    assert result.height_mm <= result.build_height_mm
    assert result.overwritten == []
    assert Path(result.plate_stl_path).stat().st_size == 84 + 50 * result.model_triangles
    sidecar = json.loads(Path(result.sidecar_path).read_text())
    assert sidecar["supports_included"] is True and sidecar["profile"]["mirrorX"] is True


@pytest.mark.integration
@needs_pipeline
def test_real_job_splits_model_from_supports(tmp_path):
    job_dir = tmp_path / "job"
    data = _run_script(tmp_path, "--no-slice", "--coarse-islands", "--job-dir", str(job_dir))
    job = json.loads((job_dir / "job.json").read_text())
    # The engine treats triangles after this count as support.
    assert job["model_triangle_count"] == data["model_triangles"]
    assert (job_dir / "positions.bin").stat().st_size == 36 * data["total_triangles"]
    assert data["support_triangles"] > 0
    assert data["output"] is None


@pytest.mark.integration
@needs_pipeline
def test_real_without_raft(tmp_path):
    data = _run_script(tmp_path, "--no-slice", "--coarse-islands", "--raft", "off")
    assert data["raft"] == "off"
    assert data["contacts"] > 0
    assert data["support_triangles"] > 0


@pytest.mark.integration
@needs_pipeline
def test_real_refuses_a_print_taller_than_the_printer(tmp_path):
    with pytest.raises(cli.CliError, match="builds only"):
        supports.run_auto_support_and_slice(
            str(TEST_STL), out_path=str(tmp_path / "x.goo"), lift_mm=500
        )


@pytest.mark.integration
@needs_pipeline
def test_real_script_refuses_an_extension_the_printer_does_not_write(tmp_path):
    with pytest.raises(cli.CliError, match="writes .ctb files"):
        _run_script(tmp_path, "--out", str(tmp_path / "x.goo"))


@pytest.mark.integration
@needs_pipeline
def test_real_keep_out_moves_the_hole_with_the_model_and_clears_it(tmp_path):
    plain = _run_script(tmp_path, "--no-slice", "--coarse-islands")
    assert plain["keep_out"] is None
    # A hole in the STL's frame at an island's contact: the plate shift undone,
    # as the sidecar would carry it. Islands report plate-frame contacts.
    shift = plain["plate_transform"]["translate_mm"]
    contact = plain["island_list"][0]["contact"]
    hole = {
        "x": contact[0] - shift[0], "y": contact[1] - shift[1], "z": contact[2] - shift[2],
        "radius_mm": 2.0, "direction": [0, 0, -1], "length_mm": 2.0, "purpose": "test",
    }  # fmt: skip
    holes = tmp_path / "holes.json"
    holes.write_text(json.dumps({"holes": [hole]}))
    data = _run_script(tmp_path, "--no-slice", "--coarse-islands", "--keep-out", str(holes))
    report = data["keep_out"]["holes"][0]
    # The zone sits where the model went on the plate.
    assert report["start_plate_mm"] == pytest.approx(contact, abs=1e-3)
    assert report["keep_out_radius_mm"] == 3.0
    assert report["blocked_triangles"] > 0
    # Nothing is left inside the zone: whatever was removed is counted, not hidden.
    assert report["supports_removed"] >= report["contacts_removed"] >= 0
    assert data["contacts"] > 0

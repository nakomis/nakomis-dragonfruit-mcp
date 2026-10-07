import json
import os
import shutil
from pathlib import Path

import pytest
from test_cli import make_fake_binary

from nakomis_dragonfruit_mcp import cli
from nakomis_dragonfruit_mcp.tools import supports

TEST_STL = Path(
    os.environ.get(
        "NDFM_TEST_STL",
        "/Users/martinmu_1/Pictures/falai-mcp/ndfm-logos/3d/ndfm-logo-3d-plain-mcp-vertical.stl",
    )
)

SUMMARY = {
    "printer": {
        "preset_id": "elegoo-mars-5-ultra-ctb",
        "name": "Mars 5 Ultra",
        "material": "Standard 405nm",
        "layer_height_mm": 0.05,
    },
    "lift_mm": 7,
    "islands": 14,
    "islands_by_source": {"voxel": 9, "overhang": 5},
    "placed_by_type": {"trunk": 91, "branch": 36, "leaf": 80, "twig": 42},
    "contacts": 286,
    "roots": 91,
    "islands_uncovered": 3,
    "raft": "solid",
    "model_triangles": 462916,
    "support_triangles": 140812,
    "plate_transform": {"translate_mm": [0, 0.15, 7], "rotation": None, "scale": 1},
    "plate_stl": None,
    "supported_stl": None,
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
    "output": "/tmp/model-supported.ctb",
    "slice": {"layers": 1545, "format": ".ctb"},
    "timings_ms": {"islands_ms": 4317, "auto_place_ms": 6559, "slice_ms": 42748},
    "warnings": ["3 of 14 islands have no support near them"],
}


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    """Fake binaries, and a fake tsx that records its argv and prints `summary.json`."""
    bins = tmp_path / "bin"
    bins.mkdir()
    make_fake_binary(bins, "dragonfruit-cli", "exit 0")
    make_fake_binary(bins, "dragonfruit-mcp-tools", "exit 0")
    df = tmp_path / "df"
    tsx_dir = df / "node_modules" / ".bin"
    tsx_dir.mkdir(parents=True)
    make_fake_binary(
        tsx_dir,
        "tsx",
        f'printf "%s\\n" "$@" > {tmp_path}/argv; printf "%s" "$NODE_PATH" > {tmp_path}/node_path; '
        f"cat {tmp_path}/summary.json",
    )
    monkeypatch.setenv("NDFM_BIN_DIR", str(bins))
    monkeypatch.setenv("NDFM_DRAGONFRUIT_DIR", str(df))
    stl = tmp_path / "model.stl"
    stl.write_bytes(b"\0" * 84)
    (tmp_path / "summary.json").write_text(json.dumps(SUMMARY))
    return tmp_path


def argv(tmp_path: Path) -> list[str]:
    return (tmp_path / "argv").read_text().splitlines()


def test_auto_support_and_slice_maps_summary(fake_env):
    result = supports.auto_support_and_slice(str(fake_env / "model.stl"))
    assert result.output == "/tmp/model-supported.ctb"
    assert result.supports_by_type == {"trunk": 91, "branch": 36, "leaf": 80, "twig": 42}
    assert result.contacts == 286
    assert result.islands_by_source == {"voxel": 9, "overhang": 5}
    assert result.layers == 1545
    assert result.layer_frame.mirror_x is True
    assert result.plate_transform.translate_mm == [0, 0.15, 7]
    assert result.warnings == ["3 of 14 islands have no support near them"]


def test_default_arguments(fake_env):
    supports.auto_support_and_slice(str(fake_env / "model.stl"))
    args = argv(fake_env)
    assert args[0] == str(supports.SCRIPT)
    assert args[args.index("--printer") + 1] == "elegoo-mars-5-ultra-ctb"
    assert args[args.index("--raft") + 1] == "solid"
    assert args[args.index("--cli") + 1] == str(fake_env / "bin" / "dragonfruit-cli")
    assert args[args.index("--tools") + 1] == str(fake_env / "bin" / "dragonfruit-mcp-tools")
    for flag in ("--out", "--lift-mm", "--density", "--plate-stl", "--supported-stl"):
        assert flag not in args
    assert (fake_env / "node_path").read_text() == str(fake_env / "df" / "node_modules")


def test_options_are_passed_through(fake_env):
    supports.auto_support_and_slice(
        str(fake_env / "model.stl"),
        printer_preset="elegoo-mars-4-ultra",
        out_path=str(fake_env / "out" / "print.ctb"),
        lift_mm=5,
        density=2,
        raft=False,
        export_plate_stl=True,
        export_supported_stl=True,
    )
    args = argv(fake_env)
    assert args[args.index("--printer") + 1] == "elegoo-mars-4-ultra"
    assert args[args.index("--out") + 1] == str(fake_env / "out" / "print.ctb")
    assert args[args.index("--lift-mm") + 1] == "5"
    assert args[args.index("--density") + 1] == "2"
    assert args[args.index("--raft") + 1] == "off"
    assert args[args.index("--plate-stl") + 1] == str(fake_env / "out" / "print-plate.stl")
    assert args[args.index("--supported-stl") + 1] == str(
        fake_env / "out" / "print-with-supports.stl"
    )


def test_extra_stls_default_beside_the_stl(fake_env):
    supports.auto_support_and_slice(str(fake_env / "model.stl"), export_plate_stl=True)
    args = argv(fake_env)
    assert args[args.index("--plate-stl") + 1] == str(fake_env / "model-supported-plate.stl")


def test_no_supports_is_a_warning(fake_env):
    summary = {**SUMMARY, "contacts": 0, "placed_by_type": {}, "warnings": []}
    (fake_env / "summary.json").write_text(json.dumps(summary))
    result = supports.auto_support_and_slice(str(fake_env / "model.stl"))
    assert result.warnings == ["no supports were placed: the print will have none"]


def test_unexpected_output_raises(fake_env):
    (fake_env / "summary.json").write_text('{"islands": 3}')
    with pytest.raises(cli.CliError, match="unexpected"):
        supports.auto_support_and_slice(str(fake_env / "model.stl"))


def test_missing_stl_raises(fake_env):
    with pytest.raises(cli.CliError, match="not found"):
        supports.auto_support_and_slice(str(fake_env / "nope.stl"))


@pytest.mark.parametrize("kwargs", [{"density": 0}, {"lift_mm": -1}])
def test_bad_numbers_raise(fake_env, kwargs):
    with pytest.raises(ValueError):
        supports.auto_support_and_slice(str(fake_env / "model.stl"), **kwargs)


def _real_pipeline_available() -> bool:
    try:
        cli.find_binary(cli.DRAGONFRUIT_CLI)
        cli.find_binary(cli.MCP_TOOLS)
        cli.find_tsx()
    except cli.CliError:
        return False
    generated = cli.dragonfruit_dir() / "src" / "supports" / "generatedSupportRegistrations.ts"
    return generated.exists() and TEST_STL.exists()


@pytest.mark.integration
@pytest.mark.skipif(
    not _real_pipeline_available(),
    reason="needs bin/, DragonFruit's node_modules and generated registrations, and the test STL",
)
def test_real_auto_support_and_slice(tmp_path):
    # A copy, so the default outputs land in tmp_path rather than beside the original.
    stl = tmp_path / TEST_STL.name
    shutil.copy(TEST_STL, stl)
    result = supports.auto_support_and_slice(str(stl), export_plate_stl=True)
    assert result.output and Path(result.output).stat().st_size > 0
    assert Path(result.output).suffix == ".ctb"
    assert result.islands_by_source.get("overhang", 0) > 0
    assert result.contacts > 50
    assert result.support_triangles > 0
    assert result.layers and result.layers > 1400
    assert (
        result.plate_stl
        and Path(result.plate_stl).stat().st_size == 84 + 50 * result.model_triangles
    )

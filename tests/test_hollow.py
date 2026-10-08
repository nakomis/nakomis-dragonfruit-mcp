import json
import os
from pathlib import Path

import pytest

from nakomis_dragonfruit_mcp import cli
from nakomis_dragonfruit_mcp.tools import hollow as hollow_tools
from tests.test_cli import make_fake_binary


def _stats(volume_ml, cavities=0, shells=1, watertight=True):
    return {
        "triangles": 1000,
        "volume_ml": volume_ml,
        "shells": shells,
        "cavities": cavities,
        "watertight": watertight,
    }


@pytest.fixture
def bin_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("NDFM_BIN_DIR", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    return tmp_path


def _fake_tools(bin_dir, report):
    """A stand-in dragonfruit-mcp-tools that records its arguments and prints `report`."""
    make_fake_binary(
        bin_dir,
        "dragonfruit-mcp-tools",
        f"echo \"$*\" > '{bin_dir}/args.txt'\ncat <<'EOF'\n{json.dumps(report)}\nEOF",
    )
    return bin_dir / "args.txt"


HOLLOW_REPORT = {
    "wall_mm": 2.0,
    "voxel_mm": 0.65,
    "before": _stats(47.0),
    "after": _stats(17.5, cavities=1, shells=2),
    "timing_ms": {"load": 1.0, "hollow": 2.0, "total": 3.0},
    "warnings": ["the cavity is sealed: uncured resin is trapped"],
}
HOLE_CHECK = {
    "hole": 0,
    "checked": True,
    "hole_area_mm2": 12.57,
    "min_open_area_mm2": 10.18,
    "min_open_fraction": 1.0,
    "at_mm": -0.1,
}
PUNCH_REPORT = {
    "before": _stats(17.5, cavities=1, shells=2),
    "after": _stats(17.4),
    "cavities_found": 1,
    "holes": [
        {
            "x": 1.0,
            "y": 2.0,
            "z": 3.0,
            "radius_mm": 2.0,
            "direction": [0.0, 0.0, -1.0],
            "axis": "-z",
            "length_mm": 9.0,
            "purpose": "suction relief",
            "cavity": 1,
            "extension_mm": 2.25,
            "note": "through the floor",
        }
    ],
    "hole_checks": [HOLE_CHECK],
    "timing_ms": {"load": 1.0, "punch": 2.0, "total": 3.0},
    "warnings": [],
}


def _punch_report(stl, **extra):
    """PUNCH_REPORT with a sidecar that really exists beside the (fake) output."""
    sidecar = stl.with_name("model.drilled.stl.holes.json")
    sidecar.write_text("{}")
    return {**PUNCH_REPORT, "holes_sidecar": str(sidecar), **extra}


@pytest.fixture
def stl(tmp_path):
    path = tmp_path / "model.stl"
    path.write_bytes(b"not really an stl; the fake binary never reads it")
    return path


def test_hollow_without_a_wall_leaves_it_to_the_options_file(bin_dir, stl):
    args = _fake_tools(bin_dir, HOLLOW_REPORT)
    hollow_tools.hollow(stl, options_json="opts.json")
    assert "--wall-mm" not in args.read_text()


def test_hollow_reports_savings_and_forwards_warnings(bin_dir, stl):
    args = _fake_tools(bin_dir, HOLLOW_REPORT)
    result = hollow_tools.hollow(stl, wall_mm=2.5)
    assert result.solid_volume_ml == 47.0
    assert result.hollow_volume_ml == 17.5
    assert result.resin_saved_ml == pytest.approx(29.5)
    assert result.resin_saved_pct == pytest.approx(62.77, abs=0.01)
    assert result.after.cavities == 1
    assert result.warnings == ["the cavity is sealed: uncured resin is trapped"]
    assert result.output_path == str(stl.with_name("model.hollow.stl"))
    recorded = args.read_text()
    assert recorded.startswith(f"hollow {stl} -o {stl.with_name('model.hollow.stl')}")
    assert "--wall-mm 2.5" in recorded
    assert "--json" in recorded
    assert "--voxel-mm" not in recorded


def test_hollow_passes_optional_arguments(bin_dir, stl, tmp_path):
    args = _fake_tools(bin_dir, HOLLOW_REPORT)
    out = tmp_path / "out.stl"
    result = hollow_tools.hollow(stl, out_path=out, voxel_mm=0.4, options_json="opts.json")
    assert result.output_path == str(out)
    recorded = args.read_text()
    assert f"-o {out}" in recorded
    assert "--voxel-mm 0.4" in recorded
    assert "--options-json opts.json" in recorded


def test_hollow_missing_model(bin_dir):
    with pytest.raises(cli.CliError, match="model not found"):
        hollow_tools.hollow("/no/such/model.stl")


def test_hollow_zero_volume_does_not_divide(bin_dir, stl):
    report = {**HOLLOW_REPORT, "before": _stats(0.0), "after": _stats(0.0)}
    _fake_tools(bin_dir, report)
    assert hollow_tools.hollow(stl).resin_saved_pct == 0.0


@pytest.mark.parametrize("payload", [[], {"before": {}}])
def test_hollow_stale_binary_schema(bin_dir, stl, payload):
    _fake_tools(bin_dir, payload)
    with pytest.raises(cli.CliError, match="stale bin"):
        hollow_tools.hollow(stl)


def test_drill_auto_drain(bin_dir, stl):
    args = _fake_tools(bin_dir, _punch_report(stl))
    result = hollow_tools.drill_holes(stl, auto_drain=True, radius_mm=1.5)
    assert result.drains
    assert (result.cavities_before, result.cavities_after) == (1, 0)
    assert result.cavities_found == 1
    assert result.holes[0].direction == [0.0, 0.0, -1.0]
    assert result.holes[0].extension_mm == 2.25
    assert result.hole_checks[0].min_open_fraction == 1.0
    assert result.holes_sidecar == str(stl.with_name("model.drilled.stl.holes.json"))
    assert (result.holes[0].axis, result.holes[0].purpose) == ("-z", "suction relief")
    assert result.output_path == str(stl.with_name("model.drilled.stl"))
    recorded = args.read_text()
    assert "--auto-drain" in recorded
    assert "--radius-mm 1.5" in recorded
    assert "--down-axis -z" in recorded
    assert "--xy" not in recorded
    assert "--holes" not in recorded


def test_drill_xy_and_down_axis_are_passed(bin_dir, stl):
    args = _fake_tools(bin_dir, _punch_report(stl))
    hollow_tools.drill_holes(stl, auto_drain=True, xy=(-3.0, 4.5), down_axis="+x")
    recorded = args.read_text()
    assert "--down-axis +x" in recorded
    assert "--xy -3 4.5" in recorded


def test_drill_xy_needs_auto_drain(bin_dir, stl):
    _fake_tools(bin_dir, _punch_report(stl))
    with pytest.raises(cli.CliError, match="xy"):
        hollow_tools.drill_holes(stl, holes=[{"x": 0, "y": 0, "z": 0}], xy=(1, 2))


def test_drill_explicit_holes(bin_dir, stl):
    args = _fake_tools(bin_dir, _punch_report(stl))
    holes = [{"x": 1, "y": 2, "z": 3, "direction": [0, 1, 0]}]
    hollow_tools.drill_holes(stl, holes=holes)
    recorded = args.read_text()
    assert "--auto-drain" not in recorded
    assert json.dumps(holes) in recorded


def test_drill_sealed_cavity_left_is_not_draining(bin_dir, stl):
    report = {
        **_punch_report(stl),
        "after": _stats(17.5, cavities=1, shells=2),
        "warnings": ["a sealed cavity remains"],
    }
    _fake_tools(bin_dir, report)
    result = hollow_tools.drill_holes(stl, auto_drain=True)
    assert not result.drains
    assert result.warnings == ["a sealed cavity remains"]


@pytest.mark.parametrize("kwargs", [{}, {"auto_drain": True, "holes": [{"x": 0, "y": 0, "z": 0}]}])
def test_drill_needs_exactly_one_of_holes_or_auto_drain(bin_dir, stl, kwargs):
    _fake_tools(bin_dir, _punch_report(stl))
    with pytest.raises(cli.CliError, match="either"):
        hollow_tools.drill_holes(stl, **kwargs)


def test_drill_missing_sidecar_is_an_error(bin_dir, stl):
    report = {**PUNCH_REPORT, "holes_sidecar": str(stl.with_name("nope.holes.json"))}
    _fake_tools(bin_dir, report)
    with pytest.raises(cli.CliError, match="did not write"):
        hollow_tools.drill_holes(stl, auto_drain=True)


def test_drill_constriction_warning_is_passed_on(bin_dir, stl):
    narrow = {**HOLE_CHECK, "min_open_fraction": 0.2, "min_open_area_mm2": 2.0}
    warning = "hole 0 (vent, 4.0 mm across) narrows to 2.0 mm2 of open section"
    _fake_tools(bin_dir, _punch_report(stl, hole_checks=[narrow], warnings=[warning]))
    result = hollow_tools.drill_holes(stl, auto_drain=True)
    assert result.hole_checks[0].min_open_fraction == 0.2
    assert result.warnings == [warning]


def test_drill_stale_binary_schema(bin_dir, stl):
    _fake_tools(bin_dir, {"holes": []})
    with pytest.raises(cli.CliError, match="stale bin"):
        hollow_tools.drill_holes(stl, auto_drain=True)


MODEL = os.environ.get("NDFM_TEST_MODEL")


def _tools_available() -> bool:
    try:
        cli.find_binary(cli.MCP_TOOLS)
    except cli.CliError:
        return False
    return True


def _write_cube(path: Path, size: float = 20.0) -> None:
    """A binary STL cube from the origin, outward-facing."""
    import struct

    p = [
        (x * size, y * size, z * size) for z in (0, 1) for y in (0, 1) for x in (0, 1)
    ]  # index = x + 2y + 4z
    tris = [
        (0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
        (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
    ]  # fmt: skip
    # The tuple above is written for a 0,1,3,2-ordered square; remap to our x+2y+4z indexing.
    remap = {0: 0, 1: 1, 2: 3, 3: 2, 4: 4, 5: 5, 6: 7, 7: 6}
    with path.open("wb") as f:
        f.write(b"\0" * 80 + struct.pack("<I", len(tris)))
        for a, b, c in tris:
            f.write(struct.pack("<3f", 0, 0, 0))
            for i in (a, b, c):
                f.write(struct.pack("<3f", *p[remap[i]]))
            f.write(b"\0\0")


@pytest.mark.integration
@pytest.mark.skipif(
    not _tools_available(), reason="dragonfruit-mcp-tools not built (run scripts/build.sh)"
)
def test_real_binary_hollow_then_drill_a_cube(tmp_path):
    cube = tmp_path / "cube.stl"
    _write_cube(cube)
    hollowed = hollow_tools.hollow(cube, wall_mm=2.0, voxel_mm=0.5)
    assert hollowed.solid_volume_ml == pytest.approx(8.0, abs=0.01)
    # 20^3 - 16^3 = 3.9 ml, give or take a voxel.
    assert hollowed.hollow_volume_ml == pytest.approx(3.9, rel=0.15)
    assert hollowed.after.cavities == 1
    assert any("sealed" in w for w in hollowed.warnings)
    assert Path(hollowed.output_path).is_file()

    drilled = hollow_tools.drill_holes(hollowed.output_path, auto_drain=True, radius_mm=1.5)
    assert drilled.drains
    assert len(drilled.holes) == 2 * drilled.cavities_found == 2
    assert sorted(h.axis for h in drilled.holes) == ["+z", "-z"]
    assert drilled.after.watertight
    assert drilled.warnings == []
    # A flat cube cavity needs no extension, and the holes open at full width.
    assert all(h.extension_mm == 0.0 for h in drilled.holes)
    assert all(c.checked and c.min_open_fraction > 0.95 for c in drilled.hole_checks)
    sidecar = json.loads(Path(drilled.holes_sidecar).read_text())
    assert drilled.holes_sidecar == drilled.output_path + ".holes.json"
    assert sidecar["source_stl"] == drilled.output_path
    assert len(sidecar["holes"]) == 2
    assert {h["purpose"] for h in sidecar["holes"]} == {"suction relief", "vent"}
    assert set(sidecar["holes"][0]) == {
        "x", "y", "z", "radius_mm", "direction", "axis", "length_mm", "purpose", "cavity",
    }  # fmt: skip


@pytest.mark.integration
@pytest.mark.skipif(
    not (_tools_available() and MODEL and Path(MODEL).is_file()),
    reason="set NDFM_TEST_MODEL to an STL (the 47 ml MCP dragon fruit) with the binary built",
)
def test_real_binary_on_the_test_model(tmp_path):
    hollowed = hollow_tools.hollow(MODEL, out_path=tmp_path / "h.stl")
    assert hollowed.solid_volume_ml == pytest.approx(47.13, abs=0.05)
    assert 14 < hollowed.hollow_volume_ml < 22
    assert hollowed.after.watertight
    drilled = hollow_tools.drill_holes(tmp_path / "h.stl", auto_drain=True)
    assert drilled.drains
    assert drilled.after.watertight
    assert len(drilled.holes) == 2 * drilled.cavities_found
    # Out through the base and the crown, never the visible cut face (-Y).
    assert sorted(h.axis for h in drilled.holes) == ["+z", "-z"]
    assert {h.purpose for h in drilled.holes} == {"suction relief", "vent"}
    # The crown is a dome narrowing to an apex: the vent must start inside the cavity.
    vent = next(h for h in drilled.holes if h.purpose == "vent")
    assert vent.extension_mm > 1.0
    assert all(c.checked and c.min_open_fraction > 0.9 for c in drilled.hole_checks)


def test_tools_are_registered_with_the_server():
    import asyncio

    from nakomis_dragonfruit_mcp import server  # noqa: F401
    from nakomis_dragonfruit_mcp.app import mcp

    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert {"hollow", "drill_holes"} <= names

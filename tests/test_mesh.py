import json
import stat
from pathlib import Path

import pytest

from nakomis_dragonfruit_mcp import cli
from nakomis_dragonfruit_mcp.tools import mesh

MODEL = Path(
    "/Users/martinmu_1/Pictures/falai-mcp/ndfm-logos/3d/ndfm-logo-3d-plain-mcp-vertical.stl"
)


def fake_mesh_cli(directory: Path, payload: object) -> None:
    path = directory / "dragonfruit-cli"
    path.write_text(f"#!/bin/sh\necho '{json.dumps(payload)}'\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def info(*, size=(40.0, 35.9, 70.2), volume=47129.8, triangles=462916, **extra) -> dict:
    return {
        "source": "stl",
        "triangles": triangles,
        "vertices": triangles * 3,
        "bbox": {"min": [-20.0, -18.0, 0.0], "max": [20.0, 17.9, 70.2], "size": list(size)},
        "volume_mm3": volume,
        **extra,
    }


@pytest.fixture
def stl(tmp_path):
    path = tmp_path / "model.stl"
    path.write_bytes(b"")
    return path


@pytest.fixture
def bin_dir(tmp_path, monkeypatch):
    directory = tmp_path / "bin"
    directory.mkdir()
    monkeypatch.setenv("NDFM_BIN_DIR", str(directory))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    return directory


def test_mesh_info(bin_dir, stl):
    fake_mesh_cli(bin_dir, info())
    result = mesh.mesh_info(str(stl))
    assert result.triangles == 462916
    assert result.vertices == 462916 * 3
    assert (result.size_mm.x, result.size_mm.y, result.size_mm.z) == (40.0, 35.9, 70.2)
    assert result.bbox_min.z == 0.0
    assert result.bbox_max.x == 20.0
    assert result.volume_ml == pytest.approx(47.1298)
    assert result.warnings == []


def test_mesh_info_passes_path_and_json_flag(bin_dir, stl):
    path = bin_dir / "dragonfruit-cli"
    path.write_text(
        '#!/bin/sh\n[ "$1 $2 $4" = "mesh info --json" ] && echo \'' + json.dumps(info()) + "'\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    assert mesh.mesh_info(str(stl)).triangles == 462916


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"size": (0.04, 0.036, 0.07)}, "metres or inches"),
        ({"size": (40000.0, 35900.0, 70200.0), "volume": 4.7e13}, "microns"),
        ({"volume": 3.0}, "not a closed solid"),
        ({"size": (40.0, 35.9, 0.0), "volume": 0.0}, "flat"),
        ({"triangles": 0, "volume": 0.0}, "no triangles"),
    ],
)
def test_mesh_info_warnings(bin_dir, stl, kwargs, fragment):
    fake_mesh_cli(bin_dir, info(**kwargs))
    warnings = mesh.mesh_info(str(stl)).warnings
    assert len(warnings) == 1
    assert fragment in warnings[0]


def test_mesh_info_missing_file(bin_dir, tmp_path):
    with pytest.raises(cli.CliError, match="mesh not found"):
        mesh.mesh_info(str(tmp_path / "nope.stl"))


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"triangles": 1},
        {**info(), "bbox": {"min": [0, 0, 0]}},
        {**info(), "bbox": {"min": [0], "max": [0], "size": [0]}},
    ],
)
def test_mesh_info_unexpected_schema(bin_dir, stl, payload):
    fake_mesh_cli(bin_dir, payload)
    with pytest.raises(cli.CliError, match="unexpected"):
        mesh.mesh_info(str(stl))


def _real_cli_available() -> bool:
    try:
        cli.find_binary(cli.DRAGONFRUIT_CLI)
    except cli.CliError:
        return False
    return MODEL.exists()


@pytest.mark.integration
@pytest.mark.skipif(not _real_cli_available(), reason="dragonfruit-cli or test model missing")
def test_mesh_info_real_binary_matches_gui():
    result = mesh.mesh_info(str(MODEL))
    assert result.triangles == 462916
    assert result.volume_ml == pytest.approx(47.13, abs=0.01)  # DragonFruit GUI: 47.13 ml
    assert result.size_mm.z == pytest.approx(70.2, abs=0.05)
    assert result.warnings == []

import json
import stat
import tempfile
from pathlib import Path

import pytest

from nakomis_dragonfruit_mcp import cli
from nakomis_dragonfruit_mcp.tools import islands

MODEL = Path(
    "/Users/martinmu_1/Pictures/falai-mcp/ndfm-logos/3d/ndfm-logo-3d-plain-mcp-vertical.stl"
)

# Grid origin is (min X, max Y) = (-5, 5); pixels are 0.1 mm.
BBOX = {"min_x": -5.0, "max_x": 5.0, "min_y": -5.0, "max_y": 5.0, "min_z": 0.0, "max_z": 17.0}


def island(id_, first_layer, area, cx=0.0, cy=0.0, merged_area=None):
    return {
        "id": id_,
        "first_layer": first_layer,
        "last_layer": first_layer + 5,
        "per_layer_area_mm2": {str(first_layer): area, str(first_layer + 5): merged_area or area},
        "centroid": {"x": 1.0, "y": 1.0, "z": 1.0},  # the all-layers average: must not be used
        "_first": {"x": cx, "y": cy, "z": float(first_layer)},
    }


def write_fixture(directory: Path, found: list[dict]) -> None:
    """What `island full -o <dir>` leaves behind, as far as the tool reads it."""
    directory.mkdir()
    result = {
        "layers": 340,
        "params": {"bbox": BBOX, "px_mm": 0.1},
        "islands": [{k: v for k, v in i.items() if k != "_first"} for i in found],
    }
    (directory / "result.json").write_text(json.dumps(result))
    state = directory / "tracker-state"
    state.mkdir()
    for layer in {i["first_layer"] for i in found}:
        snap = [
            {"id": i["id"], "last_layer_centroid": i["_first"]}
            for i in found
            if i["first_layer"] <= layer
        ]
        (state / f"{layer:03d}.islands.json").write_text(json.dumps(snap))


def install_fake(bin_dir: Path, fixture: Path, record: Path | None = None) -> None:
    script = bin_dir / "dragonfruit-cli"
    log = f'echo "$@" > {record}\n' if record else ""
    script.write_text(
        "#!/bin/sh\n"
        f"{log}"
        'while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done\n'
        f'cp -R "{fixture}"/. "$out"/\n'
        "echo '{}'\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)


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


def setup(bin_dir, tmp_path, found, record=None):
    write_fixture(tmp_path / "fixture", found)
    install_fake(bin_dir, tmp_path / "fixture", record)


def test_find_islands_positions_and_units(bin_dir, tmp_path, stl):
    # Pixel (69.5, 79.5) is the floating 2x2 mm box at x 1..3, y -4..-2 that
    # the real binary reported in a hand-checked run; layer 300 is z 15.0.
    base = island(1, 0, 100.0)
    box = island(2, 300, 4.0, cx=69.5, cy=79.5, merged_area=900.0)
    setup(bin_dir, tmp_path, [base, box])
    result = islands.find_islands(str(stl))
    assert result.plate_contacts == 1
    assert result.islands_total == 1
    only = result.islands[0]
    assert (only.layer, only.z_mm) == (300, pytest.approx(15.0))
    assert only.x_mm == pytest.approx(2.0)
    assert only.y_mm == pytest.approx(-3.0)
    assert only.first_area_mm2 == 4.0  # first-layer area, not the merged one
    assert only.footprint_mm2 == 4.0
    assert result.bbox_max == [5.0, 5.0, 17.0]
    assert result.warnings == []


def test_find_islands_passes_parameters(bin_dir, tmp_path, stl):
    record = tmp_path / "args.txt"
    setup(bin_dir, tmp_path, [island(1, 5, 1.0)], record)
    islands.find_islands(str(stl), layer_height=0.025, px_mm=0.05, support_buffer_mm=0.3)
    args = record.read_text().split()
    assert args[:3] == ["island", "full", str(stl)]
    assert args[args.index("--px-mm") + 1] == "0.05"
    assert args[args.index("--layer-height") + 1] == "0.025"
    assert args[args.index("--buffer") + 1] == "0.3"
    assert "--json" not in args


def test_find_islands_truncates_to_largest(bin_dir, tmp_path, stl):
    found = [island(n, 10 * n, float(n), cx=n * 15.0) for n in range(1, 6)]
    setup(bin_dir, tmp_path, found)
    result = islands.find_islands(str(stl), max_islands=2)
    assert result.truncated
    assert result.islands_total == 5
    assert [i.first_area_mm2 for i in result.islands] == [5.0, 4.0]
    assert any("2 largest of 5" in w for w in result.warnings)


def test_find_islands_min_area_filters_first_area_after_clustering(bin_dir, tmp_path, stl):
    found = [
        island(1, 10, 0.05, merged_area=500.0),  # small first layer, huge later: dropped
        island(2, 20, 2.0, cx=50.0),
        island(3, 21, 0.4, cx=51.0),  # clusters with 2: kept, and cannot rescue 1
        island(4, 90, 0.3, cx=80.0),  # alone and under the minimum
    ]
    setup(bin_dir, tmp_path, found)
    result = islands.find_islands(str(stl), min_area_mm2=1.0)
    assert [i.layer for i in result.islands] == [20]
    assert result.islands[0].footprint_mm2 == pytest.approx(2.4)
    assert result.total_area_mm2 == pytest.approx(2.4)


def test_find_islands_clusters_a_climbing_tip(bin_dir, tmp_path, stl):
    found = [
        island(1, 100, 0.01, cx=50.0, cy=50.0),
        island(2, 101, 0.03, cx=52.0, cy=50.0),
        island(3, 102, 0.01, cx=50.0, cy=50.0),
        island(4, 103, 0.5, cx=10.0, cy=10.0),  # elsewhere: its own island
        island(5, 200, 0.2, cx=50.0, cy=50.0),  # same place, much later: its own island
    ]
    setup(bin_dir, tmp_path, found)
    result = islands.find_islands(str(stl))
    assert result.raw_detections == 5
    assert result.islands_total == 3
    tip = next(i for i in result.islands if i.layer == 100)
    assert tip.detections == 3
    assert tip.first_area_mm2 == 0.01
    assert tip.footprint_mm2 == pytest.approx(0.05)
    # Area-weighted: pixel x = (0.01*50 + 0.03*52 + 0.01*50) / 0.05 = 51.2
    assert tip.x_mm == pytest.approx(-5.0 + (51.2 + 0.5) * 0.1)
    unclustered = islands.find_islands(str(stl), cluster_mm=0)
    assert unclustered.islands_total == 5


def test_find_islands_slow_ramp_does_not_chain(bin_dir, tmp_path, stl):
    # A speck every layer, each 0.4 mm (4 px) further along. Measured from the
    # first speck of a cluster, 0.4 and 0.8 mm join it and 1.2 mm starts another.
    found = [island(n, 100 + n, 0.01, cx=10.0 + 4 * n) for n in range(6)]
    setup(bin_dir, tmp_path, found)
    result = islands.find_islands(str(stl))
    assert sorted(i.detections for i in result.islands) == [3, 3]


def test_find_islands_long_climb_is_bounded_in_layers(bin_dir, tmp_path, stl):
    found = [island(n, 100 + n, 0.01, cx=50.0) for n in range(25)]
    setup(bin_dir, tmp_path, found)
    result = islands.find_islands(str(stl))
    assert sorted(i.detections for i in result.islands) == [3, 11, 11]


def test_find_islands_joins_the_nearest_cluster(bin_dir, tmp_path, stl):
    found = [
        island(1, 100, 0.2, cx=50.0),
        island(2, 100, 0.2, cx=62.0),  # 1.2 mm from the first: its own cluster
        island(3, 101, 0.01, cx=57.0),  # 0.7 mm from the first, 0.5 mm from the second
    ]
    setup(bin_dir, tmp_path, found)
    by_x = sorted(islands.find_islands(str(stl)).islands, key=lambda i: i.x_mm)
    assert [i.detections for i in by_x] == [1, 2]


def test_find_islands_none_found(bin_dir, tmp_path, stl):
    setup(bin_dir, tmp_path, [island(1, 0, 100.0)])
    result = islands.find_islands(str(stl))
    assert result.islands == []
    assert not result.truncated
    assert result.warnings == ["No islands found: nothing in the model starts in mid-air."]


def test_find_islands_single_pixel_warning(bin_dir, tmp_path, stl):
    setup(bin_dir, tmp_path, [island(1, 10, 0.01), island(2, 40, 2.0, cx=80.0)])
    warnings = islands.find_islands(str(stl)).warnings
    assert len(warnings) == 1
    assert "1 island is a single pixel" in warnings[0]


def test_find_islands_outside_bbox_warning(bin_dir, tmp_path, stl):
    setup(bin_dir, tmp_path, [island(1, 10, 1.0, cx=500.0)])
    assert "outside the model's bounding box" in islands.find_islands(str(stl)).warnings[0]


def test_find_islands_cleans_up_its_temp_dir(bin_dir, tmp_path, stl, monkeypatch):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    record = tmp_path / "args.txt"
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    setup(bin_dir, tmp_path, [island(1, 10, 1.0)], record)
    islands.find_islands(str(stl))
    assert str(scratch) in record.read_text()  # it really did work inside scratch
    assert list(scratch.iterdir()) == []


def test_find_islands_cleans_up_on_failure(bin_dir, tmp_path, stl, monkeypatch):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    script = bin_dir / "dragonfruit-cli"
    record = tmp_path / "args.txt"
    script.write_text(f"#!/bin/sh\necho \"$@\" > {record}\necho 'not a binary STL' >&2\nexit 1\n")
    script.chmod(0o755)
    with pytest.raises(cli.CliError, match="not a binary STL"):
        islands.find_islands(str(stl))
    assert str(scratch) in record.read_text()
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"layer_height": 0},
        {"px_mm": -1},
        {"support_buffer_mm": -0.1},
        {"cluster_mm": -1},
        {"max_islands": 0},
    ],
)
def test_find_islands_rejects_bad_parameters(bin_dir, stl, kwargs):
    with pytest.raises(cli.CliError):
        islands.find_islands(str(stl), **kwargs)


def test_find_islands_missing_file(bin_dir, tmp_path):
    with pytest.raises(cli.CliError, match="mesh not found"):
        islands.find_islands(str(tmp_path / "nope.stl"))


def test_find_islands_unexpected_result(bin_dir, tmp_path, stl):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "result.json").write_text('{"layers": 1}')
    install_fake(bin_dir, fixture)
    with pytest.raises(cli.CliError, match="unexpected island scan result"):
        islands.find_islands(str(stl))


@pytest.mark.parametrize("snapshot", [None, "[]", "not json", "{}"])
def test_find_islands_skips_islands_without_a_snapshot(bin_dir, tmp_path, stl, snapshot):
    setup(bin_dir, tmp_path, [island(1, 10, 1.0), island(2, 20, 2.0, cx=50.0)])
    state = tmp_path / "fixture" / "tracker-state" / "010.islands.json"
    state.unlink() if snapshot is None else state.write_text(snapshot)
    result = islands.find_islands(str(stl))
    assert [i.layer for i in result.islands] == [20]
    assert result.warnings == [
        "1 island was skipped: the scan has no tracker snapshot locating it."
    ]


def test_find_islands_zero_area_cluster(bin_dir, tmp_path, stl):
    setup(bin_dir, tmp_path, [island(1, 10, 0.0, cx=40.0), island(2, 11, 0.0, cx=42.0)])
    (only,) = islands.find_islands(str(stl)).islands
    assert only.detections == 2
    assert only.x_mm == pytest.approx(-5.0 + 41.5 * 0.1)


@pytest.mark.parametrize("kwargs", [{"px_mm": float("nan")}, {"cluster_mm": float("inf")}])
def test_find_islands_rejects_non_finite_parameters(bin_dir, stl, kwargs):
    with pytest.raises(cli.CliError, match="finite"):
        islands.find_islands(str(stl), **kwargs)


def test_find_islands_schema_change_in_island_entry(bin_dir, tmp_path, stl):
    setup(bin_dir, tmp_path, [island(1, 10, 1.0)])
    result = json.loads((tmp_path / "fixture" / "result.json").read_text())
    del result["islands"][0]["per_layer_area_mm2"]
    (tmp_path / "fixture" / "result.json").write_text(json.dumps(result))
    with pytest.raises(cli.CliError, match="unexpected island scan output"):
        islands.find_islands(str(stl))


def _real_cli_available() -> bool:
    try:
        cli.find_binary(cli.DRAGONFRUIT_CLI)
    except cli.CliError:
        return False
    return MODEL.exists()


@pytest.mark.integration
@pytest.mark.skipif(not _real_cli_available(), reason="dragonfruit-cli or test model missing")
def test_find_islands_real_binary():
    result = islands.find_islands(str(MODEL), max_islands=100)
    assert result.layers == 1405
    assert result.plate_contacts == 1
    assert result.islands_total > 10
    for i in result.islands:
        assert result.bbox_min[0] <= i.x_mm <= result.bbox_max[0]
        assert result.bbox_min[1] <= i.y_mm <= result.bbox_max[1]
        assert result.bbox_min[2] <= i.z_mm <= result.bbox_max[2]
    # The bracts curl outward: some islands sit near the sides of the 40 mm width.
    assert any(abs(i.x_mm) > 17 for i in result.islands)

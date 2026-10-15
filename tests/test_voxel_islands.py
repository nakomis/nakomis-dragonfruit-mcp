"""NDFM-23: auto-supports on a large solid finish, and the script always exits.

A 60 mm block at the engine's native 0.05 mm used to hang `auto_support_and_slice`:
our deferred worker shim handed every layer an earlier layer's island candidates,
upstream's candidate `Set` overflowed (`RangeError: Set maximum size exceeded`),
and the error left the yield MessageChannel holding the event loop open, so the
script never exited and the tool sat until its 45-minute timeout.

The fixtures are generated here, not committed.
"""

import os
import struct
import subprocess
from pathlib import Path

import pytest

from nakomis_dragonfruit_mcp import cli
from nakomis_dragonfruit_mcp.tools import supports

# Twelve triangles a box, wound outwards; vertex i is (x[i>>2], y[(i>>1)&1], z[i&1]).
_FACES = [
    (0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1),
    (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3),
]  # fmt: skip


def write_block_stl(path: Path, size_mm: float, tilt_mm: float = 0.0) -> Path:
    """A solid `size_mm` cube as a binary STL, its base raised `tilt_mm` along +Y.

    The tilt makes the base meet the layers a strip at a time, as most real parts
    do, so the voxel island family sees candidates in the first layers.
    """
    vertices = []
    for x in (0.0, size_mm):
        for y in (0.0, size_mm):
            for top in (False, True):
                vertices.append((x, y, size_mm if top else tilt_mm * y / size_mm))
    data = bytearray(80) + struct.pack("<I", len(_FACES))
    for face in _FACES:
        data += struct.pack("<3f", 0, 0, 0)
        for i in face:
            data += struct.pack("<3f", *vertices[i])
        data += b"\0\0"
    path.write_bytes(data)
    return path


def test_write_block_stl(tmp_path):
    stl = write_block_stl(tmp_path / "block.stl", 60, tilt_mm=0.4)
    data = stl.read_bytes()
    assert len(data) == 84 + 50 * 12
    at = [84 + 50 * t + 12 + 12 * k + 8 for t in range(12) for k in range(3)]
    zs = [struct.unpack_from("<f", data, offset)[0] for offset in at]
    assert min(zs) == 0 and max(zs) == 60 and any(0 < z < 1 for z in zs)


def _ts_available() -> bool:
    try:
        cli.find_tsx()
    except cli.CliError:
        return False
    generated = cli.dragonfruit_dir() / "src" / "supports" / "generatedSupportRegistrations.ts"
    return generated.exists()


needs_ts = pytest.mark.skipif(
    not _ts_available(),
    reason="needs DragonFruit's node_modules and generated registrations (scripts/build.sh)",
)


@needs_ts
def test_ts_voxel_islands_unit_tests():
    """ts/tests/voxel-islands.test.ts: against a flood fill, upstream, and past a Set's size."""
    df = cli.dragonfruit_dir()
    env = {
        **os.environ,
        "TSX_TSCONFIG_PATH": str(df / "tsconfig.json"),
        "NODE_PATH": str(df / "node_modules"),
    }
    test_file = cli.REPO_ROOT / "ts" / "tests" / "voxel-islands.test.ts"
    proc = subprocess.run(
        # TAP, whatever the Node version: newer Nodes default to the spec reporter
        # even when not on a terminal, and only TAP prints "# fail 0".
        [str(cli.find_tsx()), "--test", "--test-reporter=tap", str(test_file)],
        cwd=df,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
    assert "# fail 0" in proc.stdout


def _script(tmp_path: Path, stl: Path, *args: str, timeout: float) -> cli.CliResult:
    """The TS script without the slicer (`--no-slice`), so only Node is needed."""
    printer = tmp_path / "printer.json"
    printer.write_text('{"presetId": "elegoo-mars-5-ultra-ctb"}')
    base = ["--stl", str(stl), "--printer-json", str(printer), "--no-slice", "--no-overhangs"]
    return cli.run_ts([*base, *args], script=supports.SCRIPT, parse_json=True, timeout=timeout)


@pytest.mark.integration
@needs_ts
def test_large_solid_islands_finish(tmp_path):
    # Hung before NDFM-23 (RangeError, then never exited). Now about 20 s of islands.
    stl = write_block_stl(tmp_path / "block60.stl", 60, tilt_mm=0.4)
    data = _script(tmp_path, stl, timeout=300).data
    assert data["island_detection"]["px_mm"] == 0.05
    assert data["islands_by_source"].get("voxel", 0) > 0
    # The base strips, all at the bottom: nothing above the tilt is unsupported.
    assert all(island["contact"][2] < 7 + 0.5 for island in data["island_list"])
    assert data["contacts"] > 0


@pytest.mark.integration
@needs_ts
def test_script_exits_after_a_late_error(tmp_path):
    # An error after the island scan used to leave the event loop held open.
    stl = write_block_stl(tmp_path / "block.stl", 10, tilt_mm=0.2)
    not_a_dir = tmp_path / "a-file"
    not_a_dir.write_text("")
    with pytest.raises(cli.CliError, match="exited 1") as excinfo:
        _script(tmp_path, stl, "--job-dir", str(not_a_dir), timeout=120)
    assert "timed out" not in str(excinfo.value)
    assert "EEXIST" in str(excinfo.value)

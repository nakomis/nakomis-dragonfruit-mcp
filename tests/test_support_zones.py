"""NDFM-28: the height cap's zone and the clearance check, in ts/support-zones.ts."""

import os
import subprocess

from nakomis_dragonfruit_mcp import cli
from tests.test_voxel_islands import needs_ts


@needs_ts
def test_ts_support_zones_unit_tests():
    """ts/tests/support-zones.test.ts: the cap zone, blocked triangles, and grazes."""
    df = cli.dragonfruit_dir()
    env = {
        **os.environ,
        "TSX_TSCONFIG_PATH": str(df / "tsconfig.json"),
        "NODE_PATH": str(df / "node_modules"),
    }
    test_file = cli.REPO_ROOT / "ts" / "tests" / "support-zones.test.ts"
    proc = subprocess.run(
        # TAP, whatever the Node version: only TAP prints "# fail 0".
        [str(cli.find_tsx()), "--test", "--test-reporter=tap", str(test_file)],
        cwd=df,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
    assert "# fail 0" in proc.stdout
    assert "# pass 0" not in proc.stdout

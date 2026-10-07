"""Shared fixtures: a fake DragonFruit checkout whose tsx stands in for `scene slice`."""

import json
import stat
from pathlib import Path

import pytest

from nakomis_dragonfruit_mcp.printers import loader

# Stands in for `tsx dragonfruit-ts-cli.ts`: logs its arguments (one line per call)
# and, for `scene slice`, copies the profile it was given aside, writes the output
# file and prints the JSON the real command does.
FAKE_TSX = r"""#!/bin/sh
echo "$*" >> "$FAKE_LOG"
case "$2 $3" in
"scene slice")
  while [ $# -gt 0 ]; do
    case "$1" in
      --o) out="$2" ;;
      --printer) cp "$2" "$FAKE_LOG.printer" ;;
      --layer-height) lh="$2" ;;
    esac
    shift
  done
  ext=".${out##*.}"
  printf data > "$out"
  printf '{"output": "%s", "format": "%s", "layers": 42, "layer_height_mm": %s, ' \
    "$out" "$ext" "${lh:-0.05}"
  printf '"resolution_px": [8520, 4320], "wall_s": 1.5, "anti_aliasing": {"preset": "balanced"}}'
  ;;
esac
"""


@pytest.fixture
def fake_df(tmp_path, monkeypatch):
    """A fake DragonFruit checkout; returns a namespace with the log and presets dir."""
    df = tmp_path / "df"
    tsx_dir = df / "node_modules" / ".bin"
    tsx_dir.mkdir(parents=True)
    tsx = tsx_dir / "tsx"
    tsx.write_text(FAKE_TSX)
    tsx.chmod(tsx.stat().st_mode | stat.S_IXUSR)
    rust = df / "rust" / "dragonfruit-cli" / "target" / "release"
    rust.mkdir(parents=True)
    (rust / "dragonfruit-cli").write_text("")
    presets = df / "plugins" / "elegoo" / "printers"
    presets.mkdir(parents=True)
    (presets / "mars-series.json").write_text(
        json.dumps(
            [
                {
                    "presetId": "elegoo-mars-5-ultra-ctb",
                    "pixelSize": {"x": 18, "y": 18},
                    "buildVolumeMm": {"width": None, "depth": None, "height": 165},
                    "display": {
                        "resolutionX": 8520,
                        "resolutionY": 4320,
                        "outputFormat": ".ctb",
                        "formatVersion": "v5enc",
                    },
                }
            ]
        )
    )
    (df / "plugins" / "athena" / "printers").mkdir(parents=True)
    (df / "plugins" / "athena" / "printers" / "printers.json").write_text(
        json.dumps(
            [
                {
                    "presetId": "concepts3d-athena1-8k-nanodlp",
                    "display": {
                        "resolutionX": 7680,
                        "resolutionY": 4320,
                        "outputFormat": ".nanodlp",
                    },
                }
            ]
        )
    )
    log = tmp_path / "fake.log"
    monkeypatch.setenv("NDFM_DRAGONFRUIT_DIR", str(df))
    monkeypatch.setenv("FAKE_LOG", str(log))
    return type(
        "FakeDf",
        (),
        {
            "dir": df,
            "log": log,
            "printer_json": lambda self: json.loads(Path(f"{log}.printer").read_text()),
        },
    )()


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """No test sees the real home directory's config or the environment's printer choice."""
    # Not HOME: the asdf shims that find node and tsx need the real one.
    monkeypatch.setattr(loader, "config_dir", lambda: tmp_path / "config")
    monkeypatch.delenv("NDFM_PRINTER", raising=False)
    monkeypatch.delenv("NDFM_PRINTERS_DIR", raising=False)


@pytest.fixture
def stl(tmp_path):
    path = tmp_path / "model.stl"
    path.write_bytes(b"\0" * 84)
    return path

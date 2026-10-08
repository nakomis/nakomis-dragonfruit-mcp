"""Shared fixtures: a fake DragonFruit checkout whose tsx stands in for `scene slice`."""

import json
import stat
import struct
from pathlib import Path

import pytest

from nakomis_dragonfruit_mcp.printers import loader

# Stands in for `tsx dragonfruit-ts-cli.ts`: logs its arguments (one line per call)
# and, for `scene slice`, copies the profile it was given aside, writes the output
# file and prints the JSON the real command does.
FAKE_TSX = r"""#!/bin/sh
echo "$*" >> "$FAKE_LOG"
case "$1" in
*autosupport-slice.ts)
  # Our own script: keep the profile and material it was given, write the print
  # and print the summary from $FAKE_SUMMARY with the output path filled in.
  while [ $# -gt 0 ]; do
    case "$1" in
      --out) out="$2" ;;
      --printer-json) cp "$2" "$FAKE_LOG.printer" ;;
      --material) cp "$2" "$FAKE_LOG.material" ;;
    esac
    shift
  done
  # A real (minimal) .goo when one is asked for, so printer drivers that
  # post-process .goo files see a valid file; placeholder bytes otherwise.
  case "$out" in *.goo) cp "$FAKE_GOO" "$out" ;; *) printf data > "$out" ;; esac
  sed "s#__OUT__#$out#g" "$FAKE_SUMMARY"
  exit 0
  ;;
esac
case "$2 $3" in
"scene list-models") printf '{"models": [{"id": "m1"}]}' ;;
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
  # A real (minimal) .goo when one is asked for, so printer drivers that
  # post-process .goo files see a valid file; placeholder bytes otherwise.
  case "$out" in *.goo) cp "$FAKE_GOO" "$out" ;; *) printf data > "$out" ;; esac
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
    minimal_goo = tmp_path / "minimal.goo"
    write_minimal_goo(minimal_goo)
    monkeypatch.setenv("FAKE_GOO", str(minimal_goo))
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
                    "name": "Mars 5 Ultra",
                    "pixelSize": {"x": 18, "y": 18},
                    "buildVolumeMm": {"width": None, "depth": None, "height": 165},
                    "display": {
                        "resolutionX": 8520,
                        "resolutionY": 4320,
                        "outputFormat": ".ctb",
                        "formatVersion": "v5enc",
                        "mirrorX": True,
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


def write_minimal_goo(path):
    """The smallest valid .goo: a DragonFruit-style header and one black 4x2 layer."""
    from nakomis_dragonfruit_mcp import goo

    head = bytearray(goo.HEADER_BYTES)
    head[:12] = b"V1.2" + goo.FILE_MAGIC
    head[12:23] = b"DragonFruit"
    for end in (194 + 116 * 116 * 2, 194 + 116 * 116 * 2 + 2 + 290 * 290 * 2):
        head[end : end + 2] = b"\r\n"
    s = goo.SETTINGS_OFFSET
    struct.pack_into(">I", head, s, 1)
    struct.pack_into(">HH", head, s + 4, 4, 2)
    struct.pack_into(">f", head, s + 22, 0.05)
    struct.pack_into(">I", head, s + 160, goo.HEADER_BYTES)
    body = bytes([0b00 << 6 | 8])  # one black run of 8 pixels
    data = b"\x55" + body + bytes([~sum(body) & 0xFF])
    layer = b"\0" * 64 + b"\r\n" + struct.pack(">I", len(data)) + data + b"\r\n"
    path.write_bytes(bytes(head) + layer)


def write_stl(path, triangles):
    """A binary STL from [(v0, v1, v2), ...], each vertex an (x, y, z)."""
    body = b"".join(struct.pack("<12fH", 0, 0, 1, *sum(t, ()), 0) for t in triangles)
    path.write_bytes(b"\0" * 80 + struct.pack("<I", len(triangles)) + body)


@pytest.fixture
def stl(tmp_path):
    """Two triangles spanning x 10..14, y 20..28, z 5..9: off-centre and off the plate."""
    path = tmp_path / "model.stl"
    write_stl(
        path,
        [((10, 20, 5), (14, 20, 5), (14, 28, 9)), ((10, 20, 5), (14, 28, 9), (10, 28, 9))],
    )
    return path

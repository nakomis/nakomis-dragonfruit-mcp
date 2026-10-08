<p align="center"><img src="docs/logo.png" alt="nakomis-dragonfruit-mcp logo" width="200"></p>

# nakomis-dragonfruit-mcp — headless resin print preparation and slicing with the DragonFruit engine

> **Unofficial.** Not affiliated with or endorsed by the Open Resin Alliance or the
> DragonFruit project.

An [MCP](https://modelcontextprotocol.io) server that lets an AI assistant inspect,
hollow, check and slice resin prints using the engine of
[DragonFruit](https://github.com/Open-Resin-Alliance/DragonFruit), with no GUI.
It wraps `dragonfruit-cli` and a small Rust tool of its own.

## Support

If you find this useful, please consider buying me a coffee:

[![Donate with PayPal](https://www.paypalobjects.com/en_GB/i/btn/btn_donate_SM.gif)](https://www.paypal.com/donate?hosted_button_id=Q3BESC73EWVNN&custom=nakomis-dragonfruit-mcp)

## Table of Contents

<!-- toc -->

- [Status](#status)
- [Architecture Diagram](#architecture-diagram)
- [Repository Layout](#repository-layout)
- [Building and running](#building-and-running)
- [Printers](#printers)
- [Licence](#licence)
- [Architecture Diagrams](#architecture-diagrams)
- [Support](#support)

<!-- tocstop -->

## Status

Early days. The repository is scaffolded; the tools are being built. Planned tools:

- `mesh_info`: triangles, size in mm, volume in ml
- `find_islands`: where a model needs supports
- `list_printers` and `slice`: slice to the printer's own format (`.goo` for the
  Elegoo Mars 5 Ultra, `.nanodlp` for the Concepts3D Athena 8K, and anything else
  you add as a plugin)
- `preview_layer` and `inspect_print`: check the sliced output. They read `.goo` and ZIP-based
  files (`.nanodlp`); the Mars 5 Ultra's default `.ctb` (v5enc) is encrypted and cannot be read,
  so slice with `format=".goo"` to preview it
- `hollow` and `drill_holes`: hollow a model to save resin, then drill a suction-relief
  hole through each cavity's floor and a vent through its roof so it can drain
  (always follow `hollow` with `drill_holes`). Each hole starts inside the cavity far enough
  for its full diameter to open (a dome narrowing to an apex would otherwise give a slit),
  is checked by slicing the drilled mesh, and is recorded in `<output>.holes.json`
- `auto_support_and_slice`: DragonFruit's own auto-supports and raft, sliced
  into the print, with the same printer, format and material options as `slice`
  (NDFM-8; see below). Keeps supports out of drilled holes: reads
  `<stl>.holes.json` (or a `holes` list) and keeps contacts and shafts clear of
  each hole's radius + 1 mm (NDFM-16)
- `trim_islands`: remove every region of a sliced `.goo` that has nothing cured
  beneath it, which would otherwise peel off as a flake in the vat. Open lattices
  and cut edges are full of tiny ones that mesh-level checks miss. Works on the
  print file's own layers (cured core touching the held core below), re-encodes
  changed layers byte-compatibly with DragonFruit, and verifies the result
  (NDFM-20)

Supports: upstream has no headless command for them, so `ts/autosupport-slice.ts`
runs the app's own placement and support export under Node, and
`dragonfruit-mcp-tools overhangs` runs the app's overhang scan (a Tauri command
upstream). The third island family the app uses, mesh minima, isn't run, and
the overhang scan isn't fully deterministic upstream, so support counts can
differ slightly from the GUI's and between runs. Check a print before trusting it.

## Architecture Diagram

![Architecture](docs/architecture/nakomis-dragonfruit-mcp.svg)

## Repository Layout

| Path | Contents |
|---|---|
| `nakomis_dragonfruit_mcp/` | The MCP server (Python, FastMCP) |
| `tests/` | pytest; integration tests run only when `bin/` is built |
| `rust/dragonfruit-mcp-tools/` | Our Rust tool (hollowing, hole punching, overhang scan), linking DragonFruit's `dragonfruit-mesh-repair` |
| `ts/` | Our TypeScript scripts, run under DragonFruit's tsx against its own modules (auto-supports) |
| `vendor/dragonfruit/` | DragonFruit, as a submodule pinned to upstream `dev` |
| `scripts/build.sh` | Builds `bin/dragonfruit-cli` and `bin/dragonfruit-mcp-tools`, and installs DragonFruit's Node dependencies for `dragonfruit-ts-cli` |
| `docs/architecture/` | Architecture diagram source (`.drawio`) and generated SVG |
| `docs/logo.png` | The logo; the other candidates live on the `logo-candidates` branch |
| `.githooks/` | Pre-commit hook: regenerates diagram SVGs and the README table of contents |

## Building and running

```bash
git clone --recurse-submodules git@github.com:nakomis/nakomis-dragonfruit-mcp.git
cd nakomis-dragonfruit-mcp
scripts/build.sh            # needs cargo, Node, cmake and a C++ compiler; full log in /tmp/ndfm-build.log
uv run nakomis-dragonfruit-mcp
```

## Printers

A printer is a DragonFruit printer profile (an official preset id, a custom
profile JSON, or an app-exported bundle), optionally wrapped by a Python driver
for real overrides. `list_printers` shows what is available and which plugins
failed to load; `slice(stl_path, printer=..., format=..., ...)` slices with one.
Supports are not included yet.

Built in: `mars5ultra` (Elegoo Mars 5 Ultra) and `athena8k` (Concepts3D Athena 8K,
`.nanodlp`, a `.py` driver that writes a sidecar JSON of how the file was sliced).
`mars5ultra` defaults to `.goo`: that Mars 5 Ultra is proven to print `.goo`, and a `.goo`
can be inspected and previewed. `format=".ctb"` gives upstream's `.ctb` v5enc, which
DragonFruit encrypts and this server cannot read back.

`slice` centres the model on the plate in XY with its lowest point on z=0
(`place_on_plate=False` slices it as positioned), warns if it does not fit the build
volume, and writes `<print file>.ndfm.json` recording the printer, profile summary, layer
height, plate offset and so on. `export_plate_stl=True` also writes the model as it sits on
the plate. It runs in a worker thread, so a long slice does not block the server.

To add one, drop a file in `$NDFM_PRINTERS_DIR` or
`~/.config/nakomis-dragonfruit-mcp/printers/` (later directories win a name
clash, with a warning):

- `my_screen.json`: a DragonFruit profile or `{"presetId": "..."}`; the printer is named `my_screen`.
- `my_weird_printer.py`: a `Printer` subclass with `name` and `preset_id` or `profile`, and any of
  `prepare`, `extra_slice_args`, `postprocess` and `warnings` overridden (see `printers/base.py`).

The printer is chosen by the `printer` argument, then `$NDFM_PRINTER`, then
`printer = "..."` in `~/.config/nakomis-dragonfruit-mcp/config.toml`, then `mars5ultra`.

**A drop-in `.py` file is imported and runs with this server's privileges.** Only those
directories are scanned; put nothing there you would not run yourself. A plugin that fails
to load is skipped and reported by `list_printers`.

## Licence

[AGPL-3.0-or-later](LICENSE), the same licence as DragonFruit, whose crates this
project links.

## Architecture Diagrams

`docs/architecture/nakomis-dragonfruit-mcp.drawio` is the source for the diagram above.
The SVG is auto-regenerated on commit by the pre-commit hook in `.githooks/pre-commit`.

To activate the hook after cloning:

```bash
git config core.hooksPath .githooks
```

## Support

If you find this useful, please consider buying me a coffee:

[![Donate with PayPal](https://www.paypalobjects.com/en_GB/i/btn/btn_donate_SM.gif)](https://www.paypal.com/donate?hosted_button_id=Q3BESC73EWVNN&custom=nakomis-dragonfruit-mcp)

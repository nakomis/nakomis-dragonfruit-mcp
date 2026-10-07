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
- `preview_layer` and `inspect_print`: check the sliced output
- `hollow` and `drill_holes`: hollow a model to save resin, then drill a suction-relief
  hole through each cavity's floor and a vent through its roof so it can drain
  (always follow `hollow` with `drill_holes`)

Supports are out of scope: DragonFruit builds them by hand in its interface.

## Architecture Diagram

![Architecture](docs/architecture/nakomis-dragonfruit-mcp.svg)

## Repository Layout

| Path | Contents |
|---|---|
| `nakomis_dragonfruit_mcp/` | The MCP server (Python, FastMCP) |
| `tests/` | pytest; integration tests run only when `bin/` is built |
| `rust/dragonfruit-mcp-tools/` | Our Rust tool (hollowing, hole punching), linking DragonFruit's `dragonfruit-mesh-repair` |
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

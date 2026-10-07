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
- [Printer control](#printer-control)
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
- `hollow` and `drill_holes`

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
scripts/build.sh            # needs cargo and Node; full log in /tmp/ndfm-build.log
uv run nakomis-dragonfruit-mcp
```

## Printer control

`printer_status` (read-only) and `send_to_printer` talk to the Mars 5 Ultra **by way
of [Cthulhu](https://github.com/nakomis/cthulhu)**, Martin's print server, never to
the printer directly: Cthulhu owns the SDCP connection and its quirks.

| Variable | Meaning |
|---|---|
| `CTHULHU_URL` | Default `https://cthulhu.home.nakomis.com` |
| `CTHULHU_CLIENT_CERT`, `CTHULHU_CLIENT_KEY` | PEM paths of the mTLS client certificate Leia's nginx requires (no API key). Required for https URLs; an unset or missing path is a clear error |
| `CTHULHU_UPLOAD_TIMEOUT_S` | Default 3600; the printer takes roughly 100 KB/s over WiFi |

Printing is a deliberate two-step flow:

1. `send_to_printer(print_path)` only uploads a `.goo` or `.ctb`. It never overwrites
   (a taken name gets a content-hash suffix such as `logo-3f9a1c.goo`), waits for the
   printer to verify and list the file, compares Cthulhu's MD5 with the local one, and
   returns the remote name, a summary of what would be printed (layers, layer height,
   estimated time) and the exact confirmation phrase. It is refused unless the printer is
   connected, idle (or showing a finished or stopped print) and error-free.
2. `start_print(remote_filename, confirm)` starts it. `confirm` must be exactly
   `Martin said go: <remote filename>`, which the caller may pass only after Martin has
   explicitly approved printing that file in the current session, having seen the
   summary. The file must have been uploaded and MD5-verified by this server session
   (an upload recovered after a dropped connection never qualifies), still be listed on
   the printer, and the printer must again be idle and error-free. The result reports the
   status observed afterwards; `started` is true only if that file was seen to begin, and
   a start request that failed part-way is reported loudly as unknown, never as success.

> [!NOTE]
> The phrase is a convention enforced on the AI, not a cryptographic human-in-the-loop:
> a misbehaving model could simply type it. Binding it to the uploaded file name makes
> it harder to pre-fill by accident; true out-of-band confirmation (for example MCP
> elicitation, asking Martin directly) is future work.

File names are restricted to `[A-Za-z0-9._ -]`, plain `http://` is accepted only for a
loopback host, and every request except the upload itself times out after 20 seconds.
Resin volume is not in `.goo`/`.ctb` headers, so it comes from the slice results.

Tests run against a fake Cthulhu (`httpx.MockTransport`); nothing contacts a real
server or printer.

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

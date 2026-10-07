# nakomis-dragonfruit-mcp

MCP server for headless resin print preparation and slicing with the DragonFruit engine.

## Licence

AGPL-3.0-or-later, not the house CC0: our Rust tool links DragonFruit's
`dragonfruit-mesh-repair` crate. Never copy DragonFruit code into anything that
isn't AGPL. Don't use DragonFruit's logo or wordmark, and keep the "Unofficial"
disclaimer near the top of the README.

## Repository layout

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

## Upstream

`vendor/dragonfruit` tracks upstream **`dev`**, not `main`: `main`'s `dragonfruit-cli`
lags the slicing engine and may not compile. Two CLIs come from it:
`bin/dragonfruit-cli` (Rust: mesh, islands, slice run, print) and
`dragonfruit-ts-cli` (`npx tsx scripts/dragonfruit-ts-cli.ts`, run from the
submodule: scenes, supports, `scene slice`). Reference: `vendor/dragonfruit/docs/reference/cli.md`.
The TS CLI logs a harmless `[SettingsStore] Failed to load: localStorage...` stack
trace on start; don't treat it as a failure.

## Project management

Plane project `NDFM`. Branches `ndfm-<n>-<slug>`, PR titles end `(NDFM-<n>)`.

## Testing

Python: `uv run ruff check . && uv run pytest --cov` (70% minimum coverage).
Rust: `cargo test`.

## Architecture diagrams

Source: `docs/architecture/nakomis-dragonfruit-mcp.drawio` — SVG auto-regenerated on commit by `.githooks/pre-commit`.

To activate the hook after cloning:
```bash
git config core.hooksPath .githooks
```

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
| `docs/architecture/` | Architecture diagram source (`.drawio`) and generated SVG |
| `.githooks/` | Pre-commit hook: regenerates diagram SVGs and the README table of contents |

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

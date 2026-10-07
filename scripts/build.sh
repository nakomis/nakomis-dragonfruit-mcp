#!/bin/bash
# Build dragonfruit-cli (from the DragonFruit submodule) and our own
# dragonfruit-mcp-tools crate, in release mode, into bin/.
#
# Full output goes to /tmp/ndfm-build.log as well as the terminal; follow it
# with `less +F /tmp/ndfm-build.log`.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DF="$REPO_ROOT/vendor/dragonfruit"
LOG="${NDFM_BUILD_LOG:-/tmp/ndfm-build.log}"
export CARGO_TARGET_DIR="$REPO_ROOT/target"

exec > >(tee "$LOG") 2>&1

echo "--- Initialising submodules ---"
git -C "$REPO_ROOT" submodule update --init --recursive

# dragonfruit-ts-cli (scenes, supports, `scene slice`) runs from the submodule
# under tsx, so it needs upstream's node_modules.
echo "--- Installing DragonFruit's Node dependencies ---"
(cd "$DF" && npm ci --no-audit --no-fund)

# The plugin encoders (.goo, .ctb, .nanodlp, ...) are compiled in from a file
# that upstream generates and gitignores. Without it the CLI builds, but can
# only write its core formats.
echo "--- Generating DragonFruit plugin registry ---"
(cd "$DF" && npm run generate:plugin-registry && npm run generate:builtin-simple-plugins)

echo "--- Building dragonfruit-cli ---"
cargo build --release --manifest-path "$DF/rust/dragonfruit-cli/Cargo.toml"

echo "--- Building dragonfruit-mcp-tools ---"
cargo build --release --manifest-path "$REPO_ROOT/rust/dragonfruit-mcp-tools/Cargo.toml"

mkdir -p "$REPO_ROOT/bin"
cp "$CARGO_TARGET_DIR/release/dragonfruit-cli" "$CARGO_TARGET_DIR/release/dragonfruit-mcp-tools" "$REPO_ROOT/bin/"

echo "--- Done ---"
"$REPO_ROOT/bin/dragonfruit-cli" info
"$REPO_ROOT/bin/dragonfruit-mcp-tools" version
# A smoke check only: capture first, so `head` closing the pipe early can't
# fail the build under pipefail.
TS_HELP=$(cd "$DF" && npx tsx scripts/dragonfruit-ts-cli.ts --help 2>/dev/null) || true
printf '%s\n' "$TS_HELP" | head -5

#!/bin/bash
# Build the optional Rust backend for trim_islands (accel/goo-trim) in release
# mode, into bin/goo-trim. Needs only cargo; scripts/build.sh does not build it.
#
# Switch the MCP server to it with NDFM_TRIM_BACKEND=rust (or auto), or pass
# backend="rust" to the trim_islands tool.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export CARGO_TARGET_DIR="$REPO_ROOT/target"

echo "--- Building goo-trim ---"
cargo build --release --manifest-path "$REPO_ROOT/accel/goo-trim/Cargo.toml"

mkdir -p "$REPO_ROOT/bin"
cp "$CARGO_TARGET_DIR/release/goo-trim" "$REPO_ROOT/bin/"

echo "--- Done ---"
"$REPO_ROOT/bin/goo-trim" --version

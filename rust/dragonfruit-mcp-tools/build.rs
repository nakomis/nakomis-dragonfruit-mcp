//! Borrows DragonFruit's mesh-normal overhang classifier for `overhangs`.
//!
//! Upstream keeps it in the Tauri app crate (`src-tauri/src/overhang.rs`), which
//! we cannot depend on without Tauri. The classifier itself is plain Rust over
//! `dragonfruit-mesh-repair`, so this copies the file into OUT_DIR up to the
//! Tauri command that wraps it (the command and the tests after it are dropped),
//! with its inner doc comments demoted so the file can be `include!`d. The
//! submodule is never modified.

use std::{env, fs, path::PathBuf};

const SOURCE: &str = "../../vendor/dragonfruit/src-tauri/src/overhang.rs";
const CUT_AT: &str = "/// Tauri IPC command: weld a world-space triangle soup";

fn main() {
    println!("cargo:rerun-if-changed={SOURCE}");
    let text = fs::read_to_string(SOURCE).unwrap_or_else(|e| panic!("read {SOURCE}: {e}"));
    let cut = text.find(CUT_AT).unwrap_or_else(|| {
        panic!("{SOURCE} no longer has the scan_overhangs Tauri command; update build.rs")
    });
    let body: String = text[..cut]
        .lines()
        .map(|line| {
            line.strip_prefix("//!")
                .map_or_else(|| line.to_string(), |rest| format!("//{rest}"))
        })
        .collect::<Vec<_>>()
        .join("\n");
    let out = PathBuf::from(env::var("OUT_DIR").unwrap()).join("overhang.rs");
    fs::write(out, body).expect("write overhang.rs");
}

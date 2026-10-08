//! Re-encode every layer of a real DragonFruit `.goo` byte for byte.
//! Runs only with `NDFM_TEST_GOO` set to such a file.

use goo_trim::{goo, rle};
use rayon::prelude::*;

#[test]
fn every_layer_of_a_real_file_reencodes_identically() {
    let Ok(path) = std::env::var("NDFM_TEST_GOO") else {
        eprintln!("skipped: set NDFM_TEST_GOO to a DragonFruit .goo");
        return;
    };
    let file = std::fs::read(&path).unwrap();
    let g = goo::parse(&file).unwrap();
    let total = (g.width * g.height) as u64;
    let bad: Vec<usize> = (1..=g.layers.len())
        .into_par_iter()
        .filter(|&n| {
            let raw = g.layers[n - 1].data(&file);
            rle::encode_runs(&rle::decode(raw, total).unwrap()) != raw
        })
        .collect();
    assert!(bad.is_empty(), "layers that differ: {bad:?}");
    eprintln!("{}: all {} layers re-encode identically", path, g.layers.len());
}

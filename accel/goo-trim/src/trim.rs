//! `trim_islands` and `find_islands`, ported from `goo_trim.py`.
//!
//! Working up from layer 1, a region's cured core (grey >= CORE) is held when
//! it overlaps the held core of the layer below: at least `min_overlap` of its
//! pixels, and at least `min_overlap_px` of them (all of them, for a region
//! smaller than that), sit on it. Unheld cores are blanked, and so is grey that isn't within HUG_PX of a held core.
//! Layers with nothing dropped are copied byte for byte; changed layers are
//! re-encoded; the header and end marker are kept.
//!
//! The trim is a chain (each layer needs the held core of the one below), so
//! it is pipelined: a reader thread decodes layers ahead (in parallel batches),
//! the calling thread does the dependent work, and a writer thread encodes and
//! writes behind it. `find_islands` needs only each layer and the one below,
//! so it runs on all cores.

use std::fs::{self, File, OpenOptions};
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};
use std::sync::mpsc::{sync_channel, Receiver, SyncSender};
use std::sync::Arc;
use std::thread;
use std::time::{SystemTime, UNIX_EPOCH};

use rayon::prelude::*;

use crate::goo::{self, Goo, Layer, LAYER_DEF_BYTES};
use crate::mask::{self, Mask, Masks};
use crate::rle::{self, Encoder, Run};

pub const CORE: u8 = 128;
/// The defaults, as in `goo_trim.py`.
pub const MIN_OVERLAP: f64 = 0.25;
pub const MIN_OVERLAP_PX: u64 = 8;
pub const HUG_PX: usize = 3;
const BATCH: usize = 16;

/// How much of a region must sit on held core below for it to be held.
#[derive(Clone, Copy, Debug)]
pub struct Thresholds {
    pub min_overlap: f64,
    pub min_overlap_px: u64,
}

impl Default for Thresholds {
    fn default() -> Self {
        Thresholds { min_overlap: MIN_OVERLAP, min_overlap_px: MIN_OVERLAP_PX }
    }
}

impl Thresholds {
    pub fn check(&self) -> Result<(), String> {
        if !(0.0..=1.0).contains(&self.min_overlap) {
            return Err(format!("min_overlap must be between 0 and 1, not {}", self.min_overlap));
        }
        if self.min_overlap_px < 1 {
            return Err(format!("min_overlap_px must be at least 1, not {}", self.min_overlap_px));
        }
        Ok(())
    }

    fn unheld(&self, core: &Mask, below: &Mask) -> mask::Flood {
        mask::unheld_components(core, below, self.min_overlap, self.min_overlap_px)
    }
}

#[derive(Debug, Default, PartialEq, Eq)]
pub struct Report {
    pub layers: usize,
    pub layers_changed: usize,
    pub regions_dropped: u64,
    pub pixels_dropped: u64,
    /// layer, regions, pixels
    pub by_layer: Vec<(usize, u64, u64)>,
}

/// A decoded layer, ready for the dependent step.
struct Decoded {
    runs: Vec<Run>,
    masks: Option<Masks>,
}

fn decode_layer(file: &[u8], g: &Goo, n: usize) -> Result<Decoded, String> {
    let layer = &g.layers[n - 1];
    let total = (g.width * g.height) as u64;
    let runs = rle::decode(layer.data(file), total).map_err(|e| format!("layer {n}: {e}"))?;
    let masks = mask::build(&runs, g.width, g.height, CORE, |v| v > 0, true);
    Ok(Decoded { runs, masks })
}

enum Piece {
    Copy(Layer),
    Encode(Layer, Vec<Run>, Mask),
}

/// Re-encode a layer with the pixels set in `drop` blanked.
fn encode_dropped(runs: &[Run], drop: &Mask, width: usize) -> Vec<u8> {
    let mut enc = Encoder::default();
    let base = drop.wx0 << 6;
    let mut p = 0usize;
    for r in runs {
        let q = p + r.len as usize;
        if r.value == 0 {
            enc.push(0, r.len as u64);
            p = q;
            continue;
        }
        // A lit run lies inside the crop: walk it row by row through the drop bits.
        let mut at = p;
        while at < q {
            let y = at / width;
            let row_end = q.min((y + 1) * width);
            let (cy, x_end) = (y - drop.y0, row_end - y * width - base);
            let mut x = at - y * width - base;
            while x < x_end {
                let dropped = drop.get(x, cy);
                let next = drop.next_change(cy, x, x_end, dropped);
                enc.push(if dropped { 0 } else { r.value }, (next - x) as u64);
                x = next;
            }
            at = row_end;
        }
        p = q;
    }
    enc.finish()
}

fn writer(
    file: Arc<Vec<u8>>,
    g_first: usize,
    tail: bool,
    out: File,
    width: usize,
    rx: Receiver<Piece>,
) -> Result<(), String> {
    let mut w = BufWriter::with_capacity(1 << 22, out);
    let io = |e: std::io::Error| format!("writing the output: {e}");
    w.write_all(&file[..g_first]).map_err(io)?;
    for piece in rx {
        match piece {
            Piece::Copy(layer) => w.write_all(&file[layer.record()]).map_err(io)?,
            Piece::Encode(layer, runs, drop) => {
                let data = encode_dropped(&runs, &drop, width);
                w.write_all(&file[layer.def_off..layer.def_off + LAYER_DEF_BYTES]).map_err(io)?;
                w.write_all(&(data.len() as u32).to_be_bytes()).map_err(io)?;
                w.write_all(&data).map_err(io)?;
                w.write_all(b"\r\n").map_err(io)?;
            }
        }
    }
    if tail {
        w.write_all(&goo::END_MARKER).map_err(io)?;
    }
    let f = w.into_inner().map_err(|e| format!("writing the output: {}", e.error()))?;
    f.sync_all().map_err(io)
}

/// The trim itself, writing pieces to `tx` in order. Returns the report.
fn trim_layers(file: &Arc<Vec<u8>>, g: &Arc<Goo>, t: Thresholds, tx: SyncSender<Piece>) -> Result<Report, String> {
    let mut report = Report { layers: g.layers.len(), ..Default::default() };
    let (dtx, drx) = sync_channel::<Result<Decoded, String>>(2 * BATCH);
    let reader = {
        let (file, g) = (Arc::clone(file), Arc::clone(g));
        thread::spawn(move || {
            let n = g.layers.len();
            for start in (1..=n).step_by(BATCH) {
                let batch: Vec<_> = (start..(start + BATCH).min(n + 1))
                    .into_par_iter()
                    .map(|k| decode_layer(&file, &g, k))
                    .collect();
                for d in batch {
                    let failed = d.is_err();
                    if dtx.send(d).is_err() || failed {
                        return;
                    }
                }
            }
        })
    };
    let mut held: Option<Mask> = None; // the held core of the layer below
    let send = |p: Piece| tx.send(p).map_err(|_| "the writer stopped".to_string());
    let result: Result<(), String> = (|| {
        for n in 1..=g.layers.len() {
            let layer = g.layers[n - 1];
            let d = drx.recv().map_err(|_| "the reader stopped".to_string())??;
            let Some(Masks { lit, core }) = d.masks else {
                held = Some(Mask::empty());
                send(Piece::Copy(layer))?;
                continue;
            };
            let Some(below_held) = held.as_ref() else {
                held = Some(core); // layer 1 is never trimmed
                send(Piece::Copy(layer))?;
                continue;
            };
            let below = core.project(below_held);
            let flood = t.unheld(&core, &below);
            let kept = if flood.sizes.is_empty() { core } else { core.and_not(&flood.unheld) };
            let drop = lit.and_not(&kept.dilate_cross(HUG_PX));
            let pixels = drop.count();
            if pixels > 0 {
                let regions = flood.sizes.len() as u64;
                report.by_layer.push((n, regions, pixels));
                report.regions_dropped += regions;
                report.pixels_dropped += pixels;
                send(Piece::Encode(layer, d.runs, drop))?;
            } else {
                send(Piece::Copy(layer))?;
            }
            held = Some(kept);
        }
        Ok(())
    })();
    drop(drx);
    let _ = reader.join();
    result?;
    report.layers_changed = report.by_layer.len();
    Ok(report)
}

fn temp_beside(dst: &Path) -> Result<(PathBuf, File), String> {
    let dir = dst.parent().filter(|p| !p.as_os_str().is_empty()).unwrap_or(Path::new("."));
    let name = dst.file_name().ok_or("the output path has no file name")?.to_string_lossy();
    for attempt in 0..100u32 {
        let nanos = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.subsec_nanos()).unwrap_or(0);
        let tmp = dir.join(format!("{name}.{}{nanos:x}{attempt}.tmp", std::process::id()));
        match OpenOptions::new().write(true).create_new(true).open(&tmp) {
            Ok(f) => return Ok((tmp, f)),
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(e) => return Err(format!("cannot create a temporary file in {}: {e}", dir.display())),
        }
    }
    Err("could not find a free temporary file name".into())
}

/// Write `src` to `dst` with every unsupported region removed; never overwrites `dst`.
pub fn trim_file(src: &Path, dst: &Path, t: Thresholds) -> Result<Report, String> {
    t.check()?;
    if dst.symlink_metadata().is_ok() {
        return Err(format!("{} already exists", dst.display()));
    }
    let file = Arc::new(fs::read(src).map_err(|e| format!("cannot read {}: {e}", src.display()))?);
    let g = Arc::new(goo::parse(&file).map_err(|e| format!("{}: {e}", src.display()))?);
    let (tmp, out) = temp_beside(dst)?;
    let result: Result<Report, String> = (|| {
        let (tx, rx) = sync_channel::<Piece>(2 * BATCH);
        let w = {
            let (file, first, tail, width) = (Arc::clone(&file), g.layers[0].def_off, g.ends_with_marker, g.width);
            thread::spawn(move || writer(file, first, tail, out, width, rx))
        };
        let report = trim_layers(&file, &g, t, tx);
        let written = w.join().map_err(|_| "the writer panicked".to_string())?;
        // A failed writer makes trim_layers fail too, with only "the writer
        // stopped"; its own error (a full disk, say) is the one worth showing.
        written?;
        let report = report?;
        let mode = fs::metadata(src).map_err(|e| e.to_string())?.permissions();
        fs::set_permissions(&tmp, mode).map_err(|e| format!("cannot copy permissions: {e}"))?;
        // link() refuses if dst has appeared meanwhile, so nothing is overwritten.
        fs::hard_link(&tmp, dst).map_err(|e| {
            if e.kind() == std::io::ErrorKind::AlreadyExists {
                format!("{} already exists (it appeared while trimming)", dst.display())
            } else {
                format!("cannot publish {}: {e}", dst.display())
            }
        })?;
        Ok(report)
    })();
    let _ = fs::remove_file(&tmp);
    result
}

/// Core mask (cropped to the core's bounding box) of layer `n`, or None if it has no core.
fn core_of(file: &[u8], g: &Goo, n: usize) -> Result<Option<Mask>, String> {
    let layer = &g.layers[n - 1];
    let runs = rle::decode(layer.data(file), (g.width * g.height) as u64)
        .map_err(|e| format!("layer {n}: {e}"))?;
    Ok(mask::build(&runs, g.width, g.height, CORE, |v| v >= CORE, false).map(|m| m.core))
}

/// (layer, pixels) of every cured core not resting on the cured core of the
/// layer below: the same check as the Python's `find_islands`.
pub fn find_islands(file: &[u8], g: &Goo, t: Thresholds) -> Result<Vec<(usize, u64)>, String> {
    t.check()?;
    let n = g.layers.len();
    let workers = rayon::current_num_threads().max(1);
    let step = n.div_ceil(workers * 4).max(1);
    let ranges: Vec<(usize, usize)> = (2..=n).step_by(step).map(|a| (a, (a + step - 1).min(n))).collect();
    let chunks: Result<Vec<Vec<(usize, u64)>>, String> = ranges
        .into_par_iter()
        .map(|(first, last)| {
            let mut found = Vec::new();
            let mut below = core_of(file, g, first - 1)?;
            for k in first..=last {
                let core = core_of(file, g, k)?;
                if let Some(c) = &core {
                    let support = match &below {
                        Some(b) => c.project(b),
                        None => Mask::zeros_like(c),
                    };
                    found.extend(t.unheld(c, &support).sizes.into_iter().map(|px| (k, px)));
                }
                below = core;
            }
            Ok(found)
        })
        .collect();
    Ok(chunks?.into_iter().flatten().collect())
}

pub fn find_islands_in(path: &Path, t: Thresholds) -> Result<Vec<(usize, u64)>, String> {
    let file = fs::read(path).map_err(|e| format!("cannot read {}: {e}", path.display()))?;
    let g = goo::parse(&file).map_err(|e| format!("{}: {e}", path.display()))?;
    find_islands(&file, &g, t)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::goo::tests::make_goo;

    const W: usize = 40;
    const H: usize = 30;
    const BLOCK: (usize, usize, usize, usize) = (5, 15, 5, 15);
    const DOT: (usize, usize, usize, usize) = (20, 24, 30, 34);
    const BRIDGE: (usize, usize, usize, usize) = (12, 22, 14, 32);

    fn frame(boxes: &[(usize, usize, usize, usize)], grey: &[((usize, usize, usize, usize), u8)]) -> Vec<u8> {
        let mut a = vec![0u8; W * H];
        let mut paint = |(r0, r1, c0, c1): (usize, usize, usize, usize), v: u8| {
            for r in r0..r1 {
                for c in c0..c1 {
                    a[r * W + c] = v;
                }
            }
        };
        for &b in boxes {
            paint(b, 255);
        }
        for &(b, v) in grey {
            paint(b, v);
        }
        a
    }

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("goo-trim-test-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn pixels(file: &[u8], n: usize) -> Vec<u8> {
        let g = goo::parse(file).unwrap();
        let runs = rle::decode(g.layers[n - 1].data(file), (W * H) as u64).unwrap();
        runs.iter().flat_map(|r| std::iter::repeat(r.value).take(r.len as usize)).collect()
    }

    #[test]
    fn floating_dot_is_trimmed_until_it_meets_held_material() {
        let dir = scratch("dot");
        let src = dir.join("isl.goo");
        let layers = vec![
            frame(&[BLOCK], &[]),
            frame(&[BLOCK, DOT], &[]),
            frame(&[BLOCK, DOT], &[]),
            frame(&[BLOCK, DOT, BRIDGE], &[]),
        ];
        fs::write(&src, make_goo(&layers, W, H, true)).unwrap();
        assert_eq!(find_islands_in(&src, Thresholds::default()).unwrap(), vec![(2, 16)]);

        let dst = dir.join("out.goo");
        let report = trim_file(&src, &dst, Thresholds::default()).unwrap();
        assert_eq!(report.by_layer, vec![(2, 1, 16), (3, 1, 16)]);
        assert_eq!((report.regions_dropped, report.pixels_dropped, report.layers_changed), (2, 32, 2));
        let out = fs::read(&dst).unwrap();
        let expected = make_goo(
            &[frame(&[BLOCK], &[]), frame(&[BLOCK], &[]), frame(&[BLOCK], &[]), layers[3].clone()],
            W,
            H,
            true,
        );
        assert_eq!(out, expected);
        assert_eq!(find_islands_in(&dst, Thresholds::default()).unwrap(), vec![]);

        // never overwrites, and leaves no temp file behind
        assert!(trim_file(&src, &dst, Thresholds::default()).unwrap_err().contains("already exists"));
        let tmps = fs::read_dir(&dir).unwrap().filter(|e| e.as_ref().unwrap().path().extension() == Some("tmp".as_ref()));
        assert_eq!(tmps.count(), 0);
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn grey_edges_of_held_cores_stay_and_lone_grey_goes() {
        let dir = scratch("grey");
        let src = dir.join("g.goo");
        let edge = ((5, 15, 15, 16), 90);
        let lone = ((25, 27, 35, 37), 200);
        let faint = ((25, 27, 2, 4), 40);
        fs::write(&src, make_goo(&[frame(&[BLOCK], &[]), frame(&[BLOCK], &[edge, lone, faint])], W, H, false)).unwrap();
        let dst = dir.join("out.goo");
        let report = trim_file(&src, &dst, Thresholds::default()).unwrap();
        assert_eq!(report.by_layer, vec![(2, 1, 8)]);
        let out = fs::read(&dst).unwrap();
        assert!(!out.ends_with(&goo::END_MARKER));
        assert_eq!(pixels(&out, 2), frame(&[BLOCK], &[edge]));
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn everything_after_a_blank_layer_is_dropped() {
        let dir = scratch("blank");
        let src = dir.join("b.goo");
        fs::write(&src, make_goo(&[frame(&[BLOCK], &[]), frame(&[], &[]), frame(&[BLOCK], &[])], W, H, false)).unwrap();
        let dst = dir.join("out.goo");
        let report = trim_file(&src, &dst, Thresholds::default()).unwrap();
        assert_eq!(report.by_layer, vec![(3, 1, 100)]);
        assert_eq!(pixels(&fs::read(&dst).unwrap(), 3), vec![0; W * H]);
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn a_bad_layer_is_an_error_not_a_panic() {
        let dir = scratch("bad");
        let src = dir.join("bad.goo");
        let mut f = make_goo(&[frame(&[BLOCK], &[]), frame(&[BLOCK], &[])], W, H, false);
        let g = goo::parse(&f).unwrap();
        let last = g.layers[1].data_off + g.layers[1].size - 1;
        f[last] ^= 0xFF; // break layer 2's checksum
        fs::write(&src, &f).unwrap();
        let dst = dir.join("out.goo");
        let err = trim_file(&src, &dst, Thresholds::default()).unwrap_err();
        assert!(err.contains("layer 2") && err.contains("checksum"), "{err}");
        assert!(!dst.exists());
        assert_eq!(fs::read_dir(&dir).unwrap().count(), 1);
        fs::remove_dir_all(&dir).unwrap();
    }
}

//! A small software rasteriser for print previews (NDFM-14).
//!
//! Renders an STL to an RGB picture, headlessly (no display, no GPU): a camera
//! at an azimuth and elevation, the mesh fitted to the frame, a z-buffer, and
//! smooth shading from a headlight plus ambient. Vertex normals are averaged
//! over faces that share a position, except across creases sharper than
//! `crease_deg`, so organic surfaces look smooth and support edges stay crisp.
//! Triangles before `split` take the model colour, the rest the support colour
//! (a supported STL is the model's triangles followed by supports and raft).

use std::path::Path;

use rayon::prelude::*;

pub type Tri = [[f32; 3]; 3];

#[derive(Clone, Debug)]
pub struct RenderOptions {
    /// Output width and height in pixels (square).
    pub size: u32,
    /// Degrees round the vertical from the front (-Y); negative swings to the left (-X).
    pub azimuth_deg: f32,
    /// Degrees above the horizon.
    pub elevation_deg: f32,
    /// Vertical field of view in degrees; 0 for orthographic.
    pub fov_deg: f32,
    /// Faces meeting at more than this angle keep a hard edge.
    pub crease_deg: f32,
    /// Triangles before this index are the model; the rest are supports and raft.
    pub split: Option<usize>,
    pub model_rgb: [u8; 3],
    pub support_rgb: [u8; 3],
    pub background: [u8; 3],
    /// Empty border round the fitted mesh, as a fraction of the size.
    pub margin: f32,
}

impl Default for RenderOptions {
    fn default() -> Self {
        Self {
            size: 580,
            azimuth_deg: -35.0,
            elevation_deg: 28.0,
            fov_deg: 25.0,
            crease_deg: 50.0,
            split: None,
            model_rgb: [230, 56, 133],
            support_rgb: [64, 128, 242],
            background: [20, 20, 20],
            margin: 0.04,
        }
    }
}

#[derive(Debug)]
pub struct Image {
    pub width: u32,
    pub height: u32,
    /// RGB, row-major from the top.
    pub rgb: Vec<u8>,
}

/// Read a binary or ASCII STL as a triangle soup.
pub fn read_stl(path: &Path) -> Result<Vec<Tri>, String> {
    let data = std::fs::read(path).map_err(|e| format!("cannot read {}: {e}", path.display()))?;
    parse_stl(&data).map_err(|e| format!("{}: {e}", path.display()))
}

pub fn parse_stl(data: &[u8]) -> Result<Vec<Tri>, String> {
    if data.len() >= 84 {
        let count = u32::from_le_bytes([data[80], data[81], data[82], data[83]]) as usize;
        if count.checked_mul(50).and_then(|n| n.checked_add(84)) == Some(data.len()) {
            return Ok(parse_binary(data, count));
        }
    }
    let head = &data[..data.len().min(512)];
    let text_like = head.trim_ascii_start().starts_with(b"solid")
        && std::str::from_utf8(data).is_ok_and(|t| t.contains("facet"));
    if text_like {
        return parse_ascii(std::str::from_utf8(data).unwrap());
    }
    if data.len() >= 84 {
        let count = u32::from_le_bytes([data[80], data[81], data[82], data[83]]) as usize;
        if data.len() >= 84 + count.saturating_mul(50) {
            // Trailing bytes after the triangles: some exporters pad.
            return Ok(parse_binary(data, count));
        }
        return Err(format!(
            "truncated binary STL: header says {count} triangles but the file holds {}",
            (data.len() - 84) / 50
        ));
    }
    Err("too small to be an STL".into())
}

fn parse_binary(data: &[u8], count: usize) -> Vec<Tri> {
    data[84..84 + count * 50]
        .par_chunks_exact(50)
        .map(|rec| {
            let f = |i: usize| {
                let at = 12 + i * 4;
                f32::from_le_bytes([rec[at], rec[at + 1], rec[at + 2], rec[at + 3]])
            };
            [[f(0), f(1), f(2)], [f(3), f(4), f(5)], [f(6), f(7), f(8)]]
        })
        .collect()
}

fn parse_ascii(text: &str) -> Result<Vec<Tri>, String> {
    let mut tris = Vec::new();
    let mut corners: Vec<[f32; 3]> = Vec::with_capacity(3);
    for line in text.lines() {
        let mut words = line.split_whitespace();
        if words.next() != Some("vertex") {
            continue;
        }
        let mut v = [0f32; 3];
        for c in &mut v {
            *c = words
                .next()
                .and_then(|w| w.parse().ok())
                .ok_or_else(|| format!("bad vertex line: {line:?}"))?;
        }
        corners.push(v);
        if corners.len() == 3 {
            tris.push([corners[0], corners[1], corners[2]]);
            corners.clear();
        }
    }
    if !corners.is_empty() {
        return Err("ASCII STL ends part-way through a facet".into());
    }
    Ok(tris)
}

fn sub(a: [f32; 3], b: [f32; 3]) -> [f32; 3] {
    [a[0] - b[0], a[1] - b[1], a[2] - b[2]]
}
fn dot(a: [f32; 3], b: [f32; 3]) -> f32 {
    a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
}
fn cross(a: [f32; 3], b: [f32; 3]) -> [f32; 3] {
    [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    ]
}
fn normalise(a: [f32; 3]) -> [f32; 3] {
    let l = dot(a, a).sqrt();
    if l > 0.0 {
        [a[0] / l, a[1] / l, a[2] / l]
    } else {
        [0.0; 3]
    }
}

/// Per-corner normals: each corner's face normal averaged (area-weighted) with
/// those of the other faces at the same position whose normals lie within the
/// crease angle of its own.
pub fn corner_normals(tris: &[Tri], crease_deg: f32) -> Vec<[f32; 3]> {
    // Unnormalised face normals: their length is twice the area, the weighting.
    let faces: Vec<[f32; 3]> = tris
        .par_iter()
        .map(|t| cross(sub(t[1], t[0]), sub(t[2], t[0])))
        .collect();
    let units: Vec<[f32; 3]> = faces.par_iter().map(|&n| normalise(n)).collect();
    if crease_deg <= 0.0 {
        return (0..tris.len() * 3)
            .into_par_iter()
            .map(|c| units[c / 3])
            .collect();
    }
    let cos_crease = crease_deg.min(180.0).to_radians().cos();

    // Group corners by exact position (STL repeats shared vertices bit for bit).
    let key = |c: usize| {
        let p = tris[c / 3][c % 3];
        // +0.0 folds -0.0 into 0.0 so they weld.
        let b = |x: f32| (x + 0.0).to_bits();
        [b(p[0]), b(p[1]), b(p[2])]
    };
    let mut order: Vec<([u32; 3], u32)> = (0..tris.len() * 3)
        .into_par_iter()
        .map(|c| (key(c), c as u32))
        .collect();
    order.par_sort_unstable_by_key(|&(k, _)| k);

    // Where each run of equal positions starts in `order`, and where the last ends.
    let mut starts: Vec<usize> = (0..order.len())
        .into_par_iter()
        .filter(|&i| i == 0 || order[i].0 != order[i - 1].0)
        .collect();
    starts.push(order.len());

    let mut out = vec![[0f32; 3]; tris.len() * 3];
    let dest = SharedOut(out.as_mut_ptr());
    starts.par_windows(2).for_each(|w| {
        let group = &order[w[0]..w[1]];
        for &(_, c) in group {
            let own = units[c as usize / 3];
            let mut sum = [0f32; 3];
            for &(_, other) in group {
                let f = other as usize / 3;
                if dot(units[f], own) >= cos_crease {
                    let n = faces[f];
                    sum = [sum[0] + n[0], sum[1] + n[1], sum[2] + n[2]];
                }
            }
            let n = normalise(sum);
            let n = if n == [0.0; 3] { own } else { n };
            // SAFETY: `order` is a permutation of the corners, so every `c` is in
            // bounds and written by exactly one group, by one task.
            unsafe { dest.write(c as usize, n) };
        }
    });
    out
}

/// The normals buffer, shared across tasks that write disjoint corners.
#[derive(Clone, Copy)]
struct SharedOut(*mut [f32; 3]);
unsafe impl Send for SharedOut {}
unsafe impl Sync for SharedOut {}
impl SharedOut {
    /// # Safety
    /// `at` is in bounds and no other task touches it.
    unsafe fn write(self, at: usize, n: [f32; 3]) {
        *self.0.add(at) = n;
    }
}

struct Camera {
    eye: [f32; 3],
    right: [f32; 3],
    up: [f32; 3],
    forward: [f32; 3],
    perspective: bool,
}

impl Camera {
    /// Screen-space x, y and a depth key that is larger for nearer points and
    /// linear across the screen (1/z in perspective, -z orthographic).
    fn project(&self, p: [f32; 3]) -> [f32; 3] {
        let d = sub(p, self.eye);
        let (x, y, z) = (dot(d, self.right), dot(d, self.up), dot(d, self.forward));
        if self.perspective {
            [x / z, y / z, 1.0 / z]
        } else {
            [x, y, -z]
        }
    }
}

fn camera_for(tris: &[Tri], opts: &RenderOptions) -> Result<Camera, String> {
    if tris
        .par_iter()
        .any(|t| t.iter().flatten().any(|v| !v.is_finite()))
    {
        return Err("the mesh has non-finite coordinates".into());
    }
    let (lo, hi) = tris
        .par_iter()
        .flat_map_iter(|t| t.iter().copied())
        .fold(
            || ([f32::INFINITY; 3], [f32::NEG_INFINITY; 3]),
            |(mut lo, mut hi), p| {
                for i in 0..3 {
                    lo[i] = lo[i].min(p[i]);
                    hi[i] = hi[i].max(p[i]);
                }
                (lo, hi)
            },
        )
        .reduce(
            || ([f32::INFINITY; 3], [f32::NEG_INFINITY; 3]),
            |a, b| {
                (
                    [a.0[0].min(b.0[0]), a.0[1].min(b.0[1]), a.0[2].min(b.0[2])],
                    [a.1[0].max(b.1[0]), a.1[1].max(b.1[1]), a.1[2].max(b.1[2])],
                )
            },
        );
    if !(lo.iter().chain(hi.iter()).all(|v| v.is_finite())) {
        return Err("the mesh has non-finite coordinates".into());
    }
    let centre = [
        (lo[0] + hi[0]) / 2.0,
        (lo[1] + hi[1]) / 2.0,
        (lo[2] + hi[2]) / 2.0,
    ];
    let radius = (dot(sub(hi, lo), sub(hi, lo))).sqrt() / 2.0;
    if radius <= 0.0 {
        return Err("the mesh has no extent".into());
    }
    let (az, el) = (
        opts.azimuth_deg.to_radians(),
        opts.elevation_deg.to_radians(),
    );
    // From the target towards the camera: front is -Y, left is -X, up is +Z.
    let towards = [az.sin() * el.cos(), -az.cos() * el.cos(), el.sin()];
    let forward = [-towards[0], -towards[1], -towards[2]];
    let world_up = if el.cos().abs() < 1e-4 {
        [0.0, 1.0, 0.0]
    } else {
        [0.0, 0.0, 1.0]
    };
    let right = normalise(cross(forward, world_up));
    let up = cross(right, forward);
    let perspective = opts.fov_deg > 0.0;
    // Far enough that the bounding sphere sits inside the view cone, in front of the eye.
    let dist = if perspective {
        radius / (opts.fov_deg.clamp(1.0, 120.0).to_radians() / 2.0).sin()
    } else {
        radius * 3.0
    };
    let eye = [
        centre[0] + towards[0] * dist,
        centre[1] + towards[1] * dist,
        centre[2] + towards[2] * dist,
    ];
    Ok(Camera {
        eye,
        right,
        up,
        forward,
        perspective,
    })
}

/// Rows per strip: the image is drawn in horizontal strips, one task each.
const STRIP: usize = 8;
/// Triangles per binning task.
const BIN_CHUNK: usize = 1 << 16;

pub fn render(tris: &[Tri], opts: &RenderOptions) -> Result<Image, String> {
    if tris.is_empty() {
        return Err("the mesh has no triangles".into());
    }
    if opts.size < 8 || opts.size > 16384 {
        return Err("size must be between 8 and 16384".into());
    }
    let camera = camera_for(tris, opts)?;
    let normals = corner_normals(tris, opts.crease_deg);
    let mut proj: Vec<[f32; 3]> = tris
        .par_iter()
        .flat_map_iter(|t| t.iter().map(|&p| camera.project(p)))
        .collect();

    // Fit what the camera sees into the frame.
    let (min, max) = proj
        .par_iter()
        .fold(
            || ([f32::INFINITY; 2], [f32::NEG_INFINITY; 2]),
            |(mut lo, mut hi), p| {
                lo = [lo[0].min(p[0]), lo[1].min(p[1])];
                hi = [hi[0].max(p[0]), hi[1].max(p[1])];
                (lo, hi)
            },
        )
        .reduce(
            || ([f32::INFINITY; 2], [f32::NEG_INFINITY; 2]),
            |a, b| {
                (
                    [a.0[0].min(b.0[0]), a.0[1].min(b.0[1])],
                    [a.1[0].max(b.1[0]), a.1[1].max(b.1[1])],
                )
            },
        );
    let extent = (max[0] - min[0]).max(max[1] - min[1]);
    if extent.is_nan() || extent <= 0.0 {
        return Err("the mesh has no visible extent from this view".into());
    }
    let size = opts.size as f32;
    let scale = size * (1.0 - 2.0 * opts.margin.clamp(0.0, 0.45)) / extent;
    let (cx, cy) = ((min[0] + max[0]) / 2.0, (min[1] + max[1]) / 2.0);
    proj.par_iter_mut().for_each(|p| {
        p[0] = (p[0] - cx) * scale + size / 2.0;
        p[1] = size / 2.0 - (p[1] - cy) * scale;
    });

    // Bin triangles by the strips their rows touch: each chunk of triangles
    // bins its own, and a strip draws from every chunk's bin for it.
    let n = opts.size as usize;
    let strips = n.div_ceil(STRIP);
    let chunk_bins: Vec<Vec<Vec<u32>>> = proj
        .par_chunks(3 * BIN_CHUNK)
        .enumerate()
        .map(|(ci, chunk)| {
            let mut bins: Vec<Vec<u32>> = vec![Vec::new(); strips];
            for (i, t) in chunk.chunks_exact(3).enumerate() {
                let ys = [t[0][1], t[1][1], t[2][1]];
                let y0 = ys[0].min(ys[1]).min(ys[2]).floor().max(0.0) as usize;
                let y1 = ys[0].max(ys[1]).max(ys[2]).ceil();
                if y1 < 0.0 || y0 >= n {
                    continue;
                }
                let y1 = (y1 as usize).min(n - 1);
                let id = (ci * BIN_CHUNK + i) as u32;
                for bin in &mut bins[y0 / STRIP..=y1 / STRIP] {
                    bin.push(id);
                }
            }
            bins
        })
        .collect();

    let colour = |rgb: [u8; 3]| [rgb[0] as f32, rgb[1] as f32, rgb[2] as f32];
    let (model, support) = (colour(opts.model_rgb), colour(opts.support_rgb));
    let split = opts.split.unwrap_or(usize::MAX);
    let view = camera.forward;

    let mut rgb = vec![0u8; n * n * 3];
    rgb.par_chunks_mut(STRIP * n * 3)
        .enumerate()
        .for_each(|(s, out)| {
            let row0 = s * STRIP;
            let rows = out.len() / (n * 3);
            let mut depth = vec![f32::NEG_INFINITY; rows * n];
            for px in out.chunks_exact_mut(3) {
                px.copy_from_slice(&opts.background);
            }
            for &t in chunk_bins.iter().flat_map(|bins| bins[s].iter()) {
                let t = t as usize;
                let [a, b, c] = [proj[t * 3], proj[t * 3 + 1], proj[t * 3 + 2]];
                let area = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
                if area.abs() < 1e-12 {
                    continue;
                }
                let inv = 1.0 / area;
                let x0 = a[0].min(b[0]).min(c[0]).floor().max(0.0) as usize;
                let x1 = (a[0].max(b[0]).max(c[0]).ceil() as isize).min(n as isize - 1);
                let y0 = (a[1].min(b[1]).min(c[1]).floor().max(row0 as f32)) as usize;
                let y1 = (a[1].max(b[1]).max(c[1]).ceil() as isize).min((row0 + rows) as isize - 1);
                if x1 < x0 as isize || y1 < y0 as isize {
                    continue;
                }
                let base = if t < split { model } else { support };
                let ns = [normals[t * 3], normals[t * 3 + 1], normals[t * 3 + 2]];
                for y in y0..=y1 as usize {
                    let py = y as f32 + 0.5;
                    for x in x0..=x1 as usize {
                        let px = x as f32 + 0.5;
                        // Barycentric weights from signed sub-areas; all of one sign inside.
                        let w0 = ((b[0] - px) * (c[1] - py) - (b[1] - py) * (c[0] - px)) * inv;
                        let w1 = ((c[0] - px) * (a[1] - py) - (c[1] - py) * (a[0] - px)) * inv;
                        let w2 = 1.0 - w0 - w1;
                        if w0 < 0.0 || w1 < 0.0 || w2 < 0.0 {
                            continue;
                        }
                        let z = w0 * a[2] + w1 * b[2] + w2 * c[2];
                        let at = (y - row0) * n + x;
                        if z <= depth[at] {
                            continue;
                        }
                        depth[at] = z;
                        let mut nrm = normalise([
                            w0 * ns[0][0] + w1 * ns[1][0] + w2 * ns[2][0],
                            w0 * ns[0][1] + w1 * ns[1][1] + w2 * ns[2][1],
                            w0 * ns[0][2] + w1 * ns[1][2] + w2 * ns[2][2],
                        ]);
                        // Winding is not trusted: light whichever side faces us.
                        if dot(nrm, view) > 0.0 {
                            nrm = [-nrm[0], -nrm[1], -nrm[2]];
                        }
                        let px_rgb = shade(base, -dot(nrm, view));
                        out[at * 3..at * 3 + 3].copy_from_slice(&px_rgb);
                    }
                }
            }
        });
    Ok(Image {
        width: opts.size,
        height: opts.size,
        rgb,
    })
}

/// Headlight plus ambient, with a little specular sheen.
fn shade(base: [f32; 3], facing: f32) -> [u8; 3] {
    const AMBIENT: f32 = 0.28;
    const DIFFUSE: f32 = 0.72;
    const SPECULAR: f32 = 0.18;
    let facing = facing.clamp(0.0, 1.0);
    let lit = AMBIENT + DIFFUSE * facing;
    let spec = SPECULAR * 255.0 * facing.powi(24);
    let ch = |c: f32| (c * lit + spec).round().clamp(0.0, 255.0) as u8;
    [ch(base[0]), ch(base[1]), ch(base[2])]
}

pub fn write_png(image: &Image, path: &Path) -> Result<(), String> {
    let file =
        std::fs::File::create(path).map_err(|e| format!("cannot write {}: {e}", path.display()))?;
    let mut encoder = png::Encoder::new(std::io::BufWriter::new(file), image.width, image.height);
    encoder.set_color(png::ColorType::Rgb);
    encoder.set_depth(png::BitDepth::Eight);
    let mut writer = encoder
        .write_header()
        .map_err(|e| format!("cannot write {}: {e}", path.display()))?;
    writer
        .write_image_data(&image.rgb)
        .map_err(|e| format!("cannot write {}: {e}", path.display()))
}

/// "r,g,b" with each 0-255.
pub fn parse_rgb(text: &str) -> Result<[u8; 3], String> {
    let parts: Vec<&str> = text.split(',').map(str::trim).collect();
    if parts.len() != 3 {
        return Err(format!("expected r,g,b, got {text:?}"));
    }
    let mut out = [0u8; 3];
    for (o, p) in out.iter_mut().zip(parts) {
        *o = p
            .parse()
            .map_err(|_| format!("expected r,g,b with each 0-255, got {text:?}"))?;
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Straight on from the front, orthographic: screen x is world x, screen up is world z.
    fn front() -> RenderOptions {
        RenderOptions {
            size: 64,
            azimuth_deg: 0.0,
            elevation_deg: 0.0,
            fov_deg: 0.0,
            margin: 0.0,
            ..RenderOptions::default()
        }
    }

    fn px(img: &Image, x: u32, y: u32) -> [u8; 3] {
        let at = ((y * img.width + x) * 3) as usize;
        [img.rgb[at], img.rgb[at + 1], img.rgb[at + 2]]
    }

    /// An axis-aligned square in the XZ plane at depth y, as two triangles.
    fn square(x0: f32, z0: f32, side: f32, y: f32) -> Vec<Tri> {
        let (x1, z1) = (x0 + side, z0 + side);
        vec![
            [[x0, y, z0], [x1, y, z0], [x1, y, z1]],
            [[x0, y, z0], [x1, y, z1], [x0, y, z1]],
        ]
    }

    #[test]
    fn a_facing_triangle_covers_its_half_of_the_frame_in_the_model_colour() {
        // Lower-left half of the frame, straight on to the headlight.
        let tri = vec![[[0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [0.0, 0.0, 10.0]]];
        let img = render(&tri, &front()).unwrap();
        let lit = px(&img, 8, 56);
        assert_eq!(lit, shade([230.0, 56.0, 133.0], 1.0));
        assert!(lit[0] > lit[2] && lit[2] > lit[1], "magenta: {lit:?}");
        assert_eq!(px(&img, 56, 8), [20, 20, 20], "upper right is background");
        let covered = (0..64)
            .flat_map(|y| (0..64).map(move |x| (x, y)))
            .filter(|&(x, y)| px(&img, x, y) != [20, 20, 20])
            .count();
        // Half of 64 x 64, give or take the diagonal.
        assert!((covered as i32 - 2048).abs() < 80, "{covered} pixels");
    }

    #[test]
    fn the_z_buffer_keeps_the_nearer_square_whichever_is_drawn_first() {
        // A big far square (model) and a small near one (support) in front of its centre.
        let far = square(0.0, 0.0, 10.0, 5.0);
        let near = square(3.0, 3.0, 4.0, -5.0);
        for near_first in [false, true] {
            let (tris, split, near_is_model) = if near_first {
                ([near.clone(), far.clone()].concat(), 2, true)
            } else {
                ([far.clone(), near.clone()].concat(), 2, false)
            };
            let opts = RenderOptions {
                split: Some(split),
                ..front()
            };
            let img = render(&tris, &opts).unwrap();
            let centre = px(&img, 32, 32);
            let corner = px(&img, 3, 3);
            let (near_rgb, far_rgb) = if near_is_model {
                (opts.model_rgb, opts.support_rgb)
            } else {
                (opts.support_rgb, opts.model_rgb)
            };
            let full = |rgb: [u8; 3]| shade([rgb[0] as f32, rgb[1] as f32, rgb[2] as f32], 1.0);
            assert_eq!(centre, full(near_rgb), "near_first={near_first}");
            assert_eq!(corner, full(far_rgb), "near_first={near_first}");
        }
    }

    #[test]
    fn triangles_after_the_split_take_the_support_colour() {
        let tris = [square(0.0, 0.0, 5.0, 0.0), square(5.0, 5.0, 5.0, 0.0)].concat();
        let opts = RenderOptions {
            split: Some(2),
            ..front()
        };
        let img = render(&tris, &opts).unwrap();
        let lower_left = px(&img, 10, 54);
        let upper_right = px(&img, 54, 10);
        assert!(
            lower_left[0] > lower_left[2],
            "model is magenta: {lower_left:?}"
        );
        assert!(
            upper_right[2] > upper_right[0],
            "supports are blue: {upper_right:?}"
        );
    }

    #[test]
    fn smooth_normals_blend_across_a_shallow_fold_but_not_a_crease() {
        // Two faces sharing an edge, folded by 20 degrees: smoothed together.
        let (s, c) = 20f32.to_radians().sin_cos();
        let shallow = vec![
            [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
            [[0.0, 0.0, 0.0], [0.0, 1.0, 0.0], [-c, 0.0, s]],
        ];
        let n = corner_normals(&shallow, 50.0);
        assert!(
            dot(n[0], [0.0, 0.0, 1.0]) < 0.9999,
            "the shared corner leans: {:?}",
            n[0]
        );
        // A right-angled crease stays hard.
        let sharp = vec![
            [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
            [[0.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
        ];
        let n = corner_normals(&sharp, 50.0);
        assert!((dot(n[0], [0.0, 0.0, 1.0]) - 1.0).abs() < 1e-6);
        assert!((dot(n[3], [1.0, 0.0, 0.0]).abs() - 1.0).abs() < 1e-6);
    }

    #[test]
    fn empty_and_degenerate_meshes_are_refused() {
        assert!(render(&[], &front()).unwrap_err().contains("no triangles"));
        let point = vec![[[1.0, 2.0, 3.0]; 3]; 4];
        assert!(render(&point, &front()).unwrap_err().contains("no extent"));
        // A line seen end on.
        let line = vec![[[0.0, 0.0, 0.0], [0.0, 5.0, 0.0], [0.0, 5.0, 0.0]]];
        assert!(render(&line, &front())
            .unwrap_err()
            .contains("no visible extent"));
        let nan = vec![[[f32::NAN, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]]];
        assert!(render(&nan, &front()).unwrap_err().contains("non-finite"));
    }

    #[test]
    fn the_default_three_quarter_view_renders_a_box_on_the_background() {
        // A 10 mm cube from the default camera: the frame's corners are background,
        // its centre is shaded model colour.
        let mut tris = Vec::new();
        for (a, b) in [(0usize, 1usize), (1, 2), (0, 2)] {
            for side in [0.0f32, 10.0] {
                let p = |u: f32, v: f32| {
                    let mut q = [0.0f32; 3];
                    q[a] = u;
                    q[b] = v;
                    q[3 - a - b] = side;
                    q
                };
                tris.push([p(0.0, 0.0), p(10.0, 0.0), p(10.0, 10.0)]);
                tris.push([p(0.0, 0.0), p(10.0, 10.0), p(0.0, 10.0)]);
            }
        }
        let img = render(
            &tris,
            &RenderOptions {
                size: 100,
                ..RenderOptions::default()
            },
        )
        .unwrap();
        assert_eq!(px(&img, 0, 0), [20, 20, 20]);
        assert_eq!(px(&img, 99, 99), [20, 20, 20]);
        let centre = px(&img, 50, 50);
        assert!(centre[0] > centre[2] && centre[2] > centre[1], "{centre:?}");
    }

    #[test]
    fn stl_parsing_reads_binary_and_ascii_and_refuses_truncation() {
        let mut bin = vec![0u8; 80];
        bin.extend(1u32.to_le_bytes());
        bin.extend([0u8; 12]);
        for v in [1.0f32, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0] {
            bin.extend(v.to_le_bytes());
        }
        bin.extend([0u8; 2]);
        let tris = parse_stl(&bin).unwrap();
        assert_eq!(
            tris,
            vec![[[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [7.0, 8.0, 9.0]]]
        );
        assert!(parse_stl(&bin[..100]).unwrap_err().contains("truncated"));

        let ascii = "solid t\n facet normal 0 0 1\n  outer loop\n   vertex 0 0 0\n   vertex 1 0 0\n   vertex 0 1 0\n  endloop\n endfacet\nendsolid t\n";
        let tris = parse_stl(ascii.as_bytes()).unwrap();
        assert_eq!(tris.len(), 1);
        assert_eq!(tris[0][1], [1.0, 0.0, 0.0]);
        assert!(parse_stl(b"tiny").is_err());
    }

    #[test]
    fn rgb_arguments_parse() {
        assert_eq!(parse_rgb("230, 56,133").unwrap(), [230, 56, 133]);
        assert!(parse_rgb("1,2").is_err());
        assert!(parse_rgb("1,2,300").is_err());
    }
}

//! Automatic drain-hole placement for a hollowed model printed bottom-up on an MSLA
//! printer (the part hangs from the build plate).
//!
//! The cavity end nearest the plate is printed first and closes first, forming a cup
//! that opens towards the film: peeling it off pulls a vacuum. A hole through the
//! floor of that end is the essential suction relief. A second hole at the far end
//! (through the roof) lets air in while liquid drains out and lets IPA be flushed
//! through. Every separate cavity gets its own pair.

use std::collections::HashMap;

use dragonfruit_mesh_repair::{IndexedMesh, Vec3};

use crate::ops::{axis_name, ray_hits, ray_tri, PlacedHole, Shells, EXIT_MARGIN_MM};

/// Cavities below this fraction of the biggest one are voxel crumbs, not worth a hole.
const CAVITY_CRUMB_FRACTION: f64 = 0.01;
/// How far into the cavity (along the hole) a hole starts, so it begins in the void.
const START_INSET_MM: f32 = 0.3;
/// Candidate positions are taken from this band at the cavity's extreme end.
const CANDIDATE_BAND_MM: f32 = 3.0;
const CANDIDATE_CELL_MM: f32 = 1.0;
const MAX_CANDIDATES: usize = 300;
/// How picky a placement is. The skin under the hole footprint (and a margin around it)
/// may vary only so much in depth, or the hole would break through on a slope or on
/// surface detail, and the exit surface must face the hole squarely (cosine).
#[derive(Clone, Copy)]
struct Strictness {
    flatness_mm: f32,
    squareness: f32,
    margin_mm: f32,
    name: &'static str,
}
const STRICT: Strictness = Strictness {
    flatness_mm: 1.0,
    squareness: 0.8,
    margin_mm: 1.0,
    name: "strict",
};
/// For rough skin, such as a crown of curled bracts, where nothing is flat: still
/// vertical and still through the skin, but the exit may be on a bumpy surface.
const RELAXED: Strictness = Strictness {
    flatness_mm: 3.0,
    squareness: 0.5,
    margin_mm: 0.0,
    name: "relaxed",
};
/// Hairline offset so rays don't run exactly along mesh vertices and edges.
const JITTER: [f32; 2] = [0.0137, 0.0071];

/// One of the six axis-aligned "down" directions: the way the part hangs from the plate
/// is "up", so down is towards the plate. Default `-z`.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Axis {
    pub k: usize,
    pub sign: f32,
}

impl Axis {
    pub fn parse(s: &str) -> Result<Self, String> {
        let t = s.trim().to_ascii_lowercase();
        let (sign, name) = match t.strip_prefix('-') {
            Some(rest) => (-1.0, rest),
            None => (1.0, t.trim_start_matches('+')),
        };
        let k = match name {
            "x" => 0,
            "y" => 1,
            "z" => 2,
            _ => {
                return Err(format!(
                    "down axis must be one of +/-x, +/-y, +/-z, got {s:?}"
                ))
            }
        };
        Ok(Self { k, sign })
    }

    fn unit(self, sign: f32) -> [f32; 3] {
        let mut d = [0.0; 3];
        d[self.k] = sign * self.sign;
        d
    }
    fn down(self) -> [f32; 3] {
        self.unit(1.0)
    }
    fn up(self) -> [f32; 3] {
        self.unit(-1.0)
    }
    /// The two axes across the hole, in increasing order.
    fn plane(self) -> (usize, usize) {
        match self.k {
            0 => (1, 2),
            1 => (0, 2),
            _ => (0, 1),
        }
    }
    /// Height above the plate (increases up).
    fn height(self, p: [f32; 3]) -> f32 {
        -self.sign * p[self.k]
    }
}

fn v3(p: [f32; 3]) -> Vec3 {
    Vec3::new(p[0], p[1], p[2])
}
fn add(p: [f32; 3], d: [f32; 3], s: f32) -> [f32; 3] {
    [p[0] + d[0] * s, p[1] + d[1] * s, p[2] + d[2] * s]
}

/// Triangles binned by their footprint across the hole axis, so a ray along the axis
/// tests only the few triangles in its column instead of the whole mesh.
struct Columns<'a> {
    mesh: &'a IndexedMesh,
    a: usize,
    b: usize,
    lo: [f32; 2],
    cell: f32,
    nx: usize,
    ny: usize,
    cells: Vec<Vec<u32>>,
}

impl<'a> Columns<'a> {
    fn new(mesh: &'a IndexedMesh, axis: Axis) -> Self {
        let (a, b) = axis.plane();
        let bb = mesh.bbox();
        let (min, max) = (
            [bb.min.x, bb.min.y, bb.min.z],
            [bb.max.x, bb.max.y, bb.max.z],
        );
        let extent = (max[a] - min[a]).max(max[b] - min[b]);
        let cell = (extent / 1000.0).max(1.0);
        let nx = ((max[a] - min[a]) / cell) as usize + 1;
        let ny = ((max[b] - min[b]) / cell) as usize + 1;
        let mut s = Self {
            mesh,
            a,
            b,
            lo: [min[a], min[b]],
            cell,
            nx,
            ny,
            cells: vec![Vec::new(); nx * ny],
        };
        for (i, t) in mesh.triangles.iter().enumerate() {
            let pts = t.map(|v| {
                let p = mesh.positions[v as usize];
                let p = [p.x, p.y, p.z];
                (p[a], p[b])
            });
            let (x0, x1) = (
                pts.iter().map(|p| p.0).fold(f32::MAX, f32::min),
                pts.iter().map(|p| p.0).fold(f32::MIN, f32::max),
            );
            let (y0, y1) = (
                pts.iter().map(|p| p.1).fold(f32::MAX, f32::min),
                pts.iter().map(|p| p.1).fold(f32::MIN, f32::max),
            );
            let (cx0, cx1) = (s.ix(x0), s.ix(x1));
            let (cy0, cy1) = (s.iy(y0), s.iy(y1));
            for cy in cy0..=cy1 {
                for cx in cx0..=cx1 {
                    s.cells[cy * nx + cx].push(i as u32);
                }
            }
        }
        s
    }
    fn ix(&self, v: f32) -> usize {
        (((v - self.lo[0]) / self.cell).floor().max(0.0) as usize).min(self.nx - 1)
    }
    fn iy(&self, v: f32) -> usize {
        (((v - self.lo[1]) / self.cell).floor().max(0.0) as usize).min(self.ny - 1)
    }
    /// Hits `(t, triangle, leaving)` of an axis-parallel ray, nearest first.
    fn hits(&self, origin: [f32; 3], dir: [f32; 3]) -> Vec<(f32, usize, bool)> {
        let cell = &self.cells[self.iy(origin[self.b]) * self.nx + self.ix(origin[self.a])];
        let mut hits: Vec<_> = cell
            .iter()
            .filter_map(|&i| {
                ray_tri(self.mesh, i as usize, v3(origin), v3(dir)).map(|(t, l)| (t, i as usize, l))
            })
            .collect();
        hits.sort_by(|x, y| x.0.total_cmp(&y.0));
        hits
    }
}

#[derive(Debug, Clone, Copy, PartialEq)]
enum End {
    /// The plate-side end: suction relief.
    Low,
    /// The far end: vent.
    High,
}

struct Candidate {
    start: [f32; 3],
    dir: [f32; 3],
    exit_mm: f32,
    spread_mm: f32,
    score: f32,
    strictness: &'static str,
}

struct Placer<'a> {
    shells: &'a Shells,
    cols: &'a Columns<'a>,
    axis: Axis,
    radius: f32,
    strict: Strictness,
    /// Coordinate on the hole axis below (in the plate direction of) the whole mesh.
    below: f32,
}

impl Placer<'_> {
    /// How far the ray leaves from `start` along `dir` before it exits the outer skin,
    /// and how squarely it meets it. Other cavities' walls are not the outside.
    fn exit(&self, start: [f32; 3], dir: [f32; 3]) -> Option<(f32, f32)> {
        let hit = self
            .cols
            .hits(start, dir)
            .into_iter()
            .find(|h| h.2 && self.shells.volumes[self.shells.triangle_shell[h.1]] > 0.0)?;
        let m = self.cols.mesh;
        let n = m.tri_normal(hit.1 as u32);
        Some((hit.0, n.x * dir[0] + n.y * dir[1] + n.z * dir[2]))
    }

    /// Try a hole at column (`a`, `b`) at one end of cavity `cav`.
    fn evaluate(
        &self,
        cav: usize,
        a: f32,
        b: f32,
        end: End,
        extreme: f32,
    ) -> Result<Candidate, String> {
        let (ia, ib) = self.axis.plane();
        let up = self.axis.up();
        let mut origin = [0.0; 3];
        origin[ia] = a;
        origin[ib] = b;
        origin[self.axis.k] = self.below;
        let hits: Vec<_> = self
            .cols
            .hits(origin, up)
            .into_iter()
            .filter(|h| self.shells.triangle_shell[h.1] == cav)
            .collect();
        let (surface, dir, inset) = match end {
            End::Low => (hits.iter().find(|h| h.2), self.axis.down(), START_INSET_MM),
            End::High => (hits.iter().rev().find(|h| !h.2), up, -START_INSET_MM),
        };
        let surface = surface.ok_or("the column does not cross the cavity")?;
        let on_surface = add(origin, up, surface.0);
        let start = add(on_surface, up, inset);
        let (exit_mm, squareness) = self.exit(start, dir).ok_or("no way out of the skin")?;
        if squareness < self.strict.squareness {
            return Err("the skin there is not square to the hole".into());
        }
        let mut spread: f32 = 0.0;
        for ring in [
            0.5 * self.radius,
            self.radius,
            self.radius + self.strict.margin_mm,
        ] {
            for i in 0..8 {
                let ang = i as f32 * std::f32::consts::FRAC_PI_4;
                let mut p = start;
                p[ia] += ring * ang.cos();
                p[ib] += ring * ang.sin();
                let (d, _) = self
                    .exit(p, dir)
                    .ok_or("the footprint runs off the edge of the part")?;
                spread = spread.max((d - exit_mm).abs());
            }
        }
        if spread > self.strict.flatness_mm {
            return Err(format!("the skin under the hole varies by {spread:.1} mm"));
        }
        let off_extreme = (self.axis.height(on_surface) - extreme).abs();
        Ok(Candidate {
            start,
            dir,
            exit_mm,
            spread_mm: spread,
            score: exit_mm + 2.0 * spread + 0.3 * off_extreme,
            strictness: self.strict.name,
        })
    }
}

pub struct AutoDrain {
    pub holes: Vec<PlacedHole>,
    pub warnings: Vec<String>,
    /// Cavities found (crumbs included), so the caller can check the count.
    pub cavities: usize,
}

fn placed(
    c: &Candidate,
    radius: f32,
    cav: usize,
    purpose: &str,
    note: String,
    length: f32,
) -> PlacedHole {
    PlacedHole {
        x: c.start[0],
        y: c.start[1],
        z: c.start[2],
        radius_mm: radius,
        direction: c.dir,
        axis: axis_name(c.dir),
        length_mm: length,
        purpose: purpose.into(),
        cavity: Some(cav),
        note,
    }
}

/// Place a suction-relief hole and a vent for every real cavity: vertical (along the
/// down axis) holes from the cavity's plate-side and far ends out through the skin,
/// at the position nearest each extreme where the skin is a clear, flat, square wall
/// with margin all round. If a cavity has no such path, it gets a horizontal pair at
/// its lowest point instead, with a warning. `xy` pins the position of the vertical
/// holes (coordinates across the axis, in increasing axis order) and disables the
/// fallback.
pub fn auto_drain_holes(
    mesh: &IndexedMesh,
    radius_mm: f32,
    axis: Axis,
    xy: Option<[f32; 2]>,
) -> Result<AutoDrain, String> {
    let shells = Shells::of(mesh);
    let cavities = shells.cavity_ids();
    if cavities.is_empty() {
        return Err("the mesh has no internal cavity to drain; hollow it first".into());
    }
    let biggest = cavities
        .iter()
        .map(|&c| -shells.volumes[c])
        .fold(0.0, f64::max);
    let cols = Columns::new(mesh, axis);
    let bb = mesh.bbox();
    let (lo, hi) = (
        [bb.min.x, bb.min.y, bb.min.z],
        [bb.max.x, bb.max.y, bb.max.z],
    );
    let below = if axis.up()[axis.k] > 0.0 {
        lo[axis.k] - 1.0
    } else {
        hi[axis.k] + 1.0
    };
    let placer = |strict: Strictness| Placer {
        shells: &shells,
        cols: &cols,
        axis,
        radius: radius_mm,
        strict,
        below,
    };
    let (ia, ib) = axis.plane();
    let mut out = AutoDrain {
        holes: Vec::new(),
        warnings: Vec::new(),
        cavities: cavities.len(),
    };
    if xy.is_some() && cavities.len() > 1 {
        out.warnings
            .push("xy applies to every cavity; with several cavities it may suit only one".into());
    }

    for &cav in &cavities {
        let volume_ml = -shells.volumes[cav] / 1000.0;
        if -shells.volumes[cav] < biggest * CAVITY_CRUMB_FRACTION {
            out.warnings
                .push(format!("ignored a tiny cavity of {volume_ml:.3} ml"));
            continue;
        }
        // Vertices of this cavity, as (a, b, height).
        let mut pts: Vec<(f32, f32, f32)> = Vec::new();
        for (ti, t) in mesh.triangles.iter().enumerate() {
            if shells.triangle_shell[ti] == cav {
                for &vi in t {
                    let p = mesh.positions[vi as usize];
                    let p = [p.x, p.y, p.z];
                    pts.push((p[ia], p[ib], axis.height(p)));
                }
            }
        }
        let h_min = pts.iter().map(|p| p.2).fold(f32::MAX, f32::min);
        let h_max = pts.iter().map(|p| p.2).fold(f32::MIN, f32::max);

        let best = |end: End| -> Result<Candidate, String> {
            let extreme = if end == End::Low { h_min } else { h_max };
            let spots: Vec<(f32, f32)> = match xy {
                Some([a, b]) => vec![(a, b)],
                None => {
                    // Per 1 mm cell, the vertex closest to the extreme, within the band.
                    let mut cells: HashMap<(i32, i32), (f32, f32, f32)> = HashMap::new();
                    for &(a, b, h) in &pts {
                        let off = (h - extreme).abs();
                        if off > CANDIDATE_BAND_MM {
                            continue;
                        }
                        let key = (
                            (a / CANDIDATE_CELL_MM).floor() as i32,
                            (b / CANDIDATE_CELL_MM).floor() as i32,
                        );
                        let e = cells.entry(key).or_insert((a, b, off));
                        if off < e.2 {
                            *e = (a, b, off);
                        }
                    }
                    let mut v: Vec<_> = cells.into_iter().collect();
                    v.sort_by_key(|x| x.0);
                    let stride = v.len().div_ceil(MAX_CANDIDATES).max(1);
                    v.into_iter()
                        .step_by(stride)
                        .map(|(_, (a, b, _))| (a + JITTER[0], b + JITTER[1]))
                        .collect()
                }
            };
            let mut reason = String::from("no candidate position");
            // Strict first; the relaxed tier only if nothing clean exists at all.
            for strict in [STRICT, RELAXED] {
                let placer = placer(strict);
                let mut why: HashMap<String, usize> = HashMap::new();
                let mut best: Option<Candidate> = None;
                for &(a, b) in &spots {
                    match placer.evaluate(cav, a, b, end, extreme) {
                        Ok(c) => {
                            if best.as_ref().is_none_or(|x| c.score < x.score) {
                                best = Some(c);
                            }
                        }
                        Err(e) => *why.entry(e).or_default() += 1,
                    }
                }
                if let Some(c) = best {
                    return Ok(c);
                }
                let mut r: Vec<_> = why.into_iter().collect();
                r.sort_by_key(|x| std::cmp::Reverse(x.1));
                if let Some(x) = r.first() {
                    reason = x.0.clone();
                }
            }
            Err(reason)
        };

        let low = best(End::Low);
        let high = best(End::High);
        match (low, high) {
            (Ok(l), Ok(h)) => {
                for (c, purpose, what) in [
                    (&l, "suction relief", "floor at the plate-side end"),
                    (&h, "vent", "roof at the far end"),
                ] {
                    if c.strictness != STRICT.name {
                        out.warnings.push(format!(
                            "cavity {cav}: the {purpose} hole is vertical but exits on rough skin (no flat spot within reach); check it is not on a visible face or detail"
                        ));
                    }
                    out.holes.push(placed(
                        c,
                        radius_mm,
                        cav,
                        purpose,
                        format!(
                            "cavity {cav} ({volume_ml:.1} ml): through the {what}; {:.1} mm of wall, skin flat within {:.2} mm",
                            c.exit_mm, c.spread_mm
                        ),
                        c.exit_mm + EXIT_MARGIN_MM,
                    ));
                }
            }
            (low, high) => {
                let reason = |r: &Result<Candidate, String>, name: &str| {
                    r.as_ref().err().map(|e| format!("{name}: {e}"))
                };
                let why: Vec<String> = [reason(&low, "suction relief"), reason(&high, "vent")]
                    .into_iter()
                    .flatten()
                    .collect();
                if xy.is_some() {
                    return Err(format!(
                        "no clear {} path at the given xy for cavity {cav} ({})",
                        axis_name(axis.down()),
                        why.join("; ")
                    ));
                }
                out.warnings.push(format!(
                    "cavity {cav}: no clear vertical path ({}); fell back to a horizontal pair through the side wall at the cavity's lowest point, so check where they exit",
                    why.join("; ")
                ));
                out.holes
                    .extend(horizontal_pair(mesh, &shells, axis, cav, radius_mm, &pts)?);
            }
        }
    }
    if out.holes.is_empty() {
        return Err("no cavity was big enough to drain".into());
    }
    Ok(out)
}

/// The fallback: from just above the cavity's lowest point, horizontally out through
/// the wall in both directions of whichever across-axis has the thinner total path.
fn horizontal_pair(
    mesh: &IndexedMesh,
    shells: &Shells,
    axis: Axis,
    cav: usize,
    radius: f32,
    pts: &[(f32, f32, f32)],
) -> Result<Vec<PlacedHole>, String> {
    let (ia, ib) = axis.plane();
    let low = pts
        .iter()
        .min_by(|x, y| x.2.total_cmp(&y.2))
        .ok_or("cavity has no triangles")?;
    // The middle of the lowest band of the cavity, not a corner of it.
    let band: Vec<_> = pts.iter().filter(|p| p.2 < low.2 + 0.5).collect();
    let n = band.len() as f32;
    let mut start = [0.0; 3];
    start[ia] = band.iter().map(|p| p.0).sum::<f32>() / n;
    start[ib] = band.iter().map(|p| p.1).sum::<f32>() / n;
    // Height `low.2 + radius`, converted back to a coordinate along the axis.
    start[axis.k] = -axis.sign * (low.2 + radius);
    let exit = |dir: [f32; 3]| -> Option<f32> {
        let hits = ray_hits(mesh, v3(start), v3(dir));
        if hits.first().map(|h| shells.triangle_shell[h.1]) != Some(cav) {
            return None;
        }
        hits.iter()
            .find(|h| h.2 && shells.volumes[shells.triangle_shell[h.1]] > 0.0)
            .map(|h| h.0)
    };
    let mut best: Option<(f32, usize, f32, f32)> = None;
    for k in [ia, ib] {
        let mut d = [0.0; 3];
        d[k] = 1.0;
        let back = [-d[0], -d[1], -d[2]];
        if let (Some(d1), Some(d2)) = (exit(d), exit(back)) {
            if best.is_none_or(|b| d1 + d2 < b.0) {
                best = Some((d1 + d2, k, d1, d2));
            }
        }
    }
    let (_, k, d1, d2) = best.ok_or(format!(
        "cavity {cav}: no horizontal line from its lowest point reaches the outside on both sides"
    ))?;
    Ok([
        (1.0_f32, d1, "drain and suction relief"),
        (-1.0, d2, "vent"),
    ]
    .into_iter()
    .map(|(sign, d, purpose)| {
        let mut dir = [0.0; 3];
        dir[k] = sign;
        PlacedHole {
            x: start[0],
            y: start[1],
            z: start[2],
            radius_mm: radius,
            direction: dir,
            axis: axis_name(dir),
            length_mm: d + EXIT_MARGIN_MM,
            purpose: format!("{purpose} (horizontal fallback)"),
            cavity: Some(cav),
            note: format!("cavity {cav}: {d:.1} mm from the hole start to the outside"),
        }
    })
    .collect())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ops::testutil::*;
    use crate::ops::{punch, stats};

    fn z_down() -> Axis {
        Axis::parse("-z").unwrap()
    }

    #[test]
    fn axis_names_parse() {
        assert_eq!(Axis::parse("-Z").unwrap(), Axis { k: 2, sign: -1.0 });
        assert_eq!(Axis::parse("+x").unwrap().up(), [-1.0, 0.0, 0.0]);
        assert_eq!(Axis::parse("y").unwrap().down(), [0.0, 1.0, 0.0]);
        assert!(Axis::parse("w").is_err());
    }

    #[test]
    fn needs_a_cavity() {
        assert!(auto_drain_holes(&cube(20.0), 2.0, z_down(), None).is_err());
    }

    #[test]
    fn two_cavities_get_a_vertical_pair_each_and_drain() {
        let mesh = two_cavities();
        let drain = auto_drain_holes(&mesh, 2.0, z_down(), None).unwrap();
        assert_eq!(drain.cavities, 2);
        assert!(drain.warnings.is_empty(), "{:?}", drain.warnings);
        assert_eq!(drain.holes.len(), 4);
        let shells = Shells::of(&mesh);
        for cav in shells.cavity_ids() {
            let mine: Vec<_> = drain
                .holes
                .iter()
                .filter(|h| h.cavity == Some(cav))
                .collect();
            assert_eq!(mine.len(), 2);
            assert_eq!(mine[0].purpose, "suction relief");
            assert_eq!(mine[0].axis, "-z");
            assert_eq!(mine[1].purpose, "vent");
            assert_eq!(mine[1].axis, "+z");
            // Suction relief starts at the cavity floor (z = 8), the vent at its roof (z = 30).
            assert!((mine[0].z - 8.3).abs() < 0.05, "z {}", mine[0].z);
            assert!((mine[1].z - 29.7).abs() < 0.05, "z {}", mine[1].z);
            // Through 8 mm of floor and 10 mm of roof, plus the exit margin.
            assert!(
                (mine[0].length_mm - (8.3 + 1.0)).abs() < 0.1,
                "{}",
                mine[0].length_mm
            );
            assert!(
                (mine[1].length_mm - (10.3 + 1.0)).abs() < 0.1,
                "{}",
                mine[1].length_mm
            );
            // Clear of the base outline by at least radius + 1.
            for h in &mine {
                assert!(h.x > 3.0 && h.x < 37.0 && h.y > 3.0 && h.y < 37.0);
            }
        }
        let before = stats(&mesh);
        let after = stats(&punch(mesh, &drain.holes).mesh);
        assert_eq!(after.cavities, 0);
        assert_eq!((after.boundary_edges, after.non_manifold_edges), (0, 0));
        assert!(after.watertight);
        // Each of the four holes cuts about pi r^2 x (8 + 10) / 2 = 113 mm3 of solid.
        let removed = before.volume_mm3 - after.volume_mm3;
        assert!(removed > 300.0 && removed < 600.0, "removed {removed}");
    }

    #[test]
    fn exit_ignores_another_cavitys_wall() {
        // A cavity directly above another: the lower one's roof is not the outside.
        let mesh = merge(vec![
            cube(40.0),
            cuboid([10.0, 10.0, 6.0], [30.0, 30.0, 14.0], true),
            cuboid([10.0, 10.0, 18.0], [30.0, 30.0, 26.0], true),
        ]);
        let shells = Shells::of(&mesh);
        let cols = Columns::new(&mesh, z_down());
        let placer = Placer {
            shells: &shells,
            cols: &cols,
            axis: z_down(),
            radius: 2.0,
            strict: STRICT,
            below: -1.0,
        };
        // From inside the lower cavity going up: its roof, solid, the upper cavity's
        // floor and roof, solid, then the top face at z = 40.
        let (d, _) = placer.exit([20.0, 20.0, 10.0], [0.0, 0.0, 1.0]).unwrap();
        assert!((d - 30.0).abs() < 1e-3, "exit at {d}");
        let (d, _) = placer.exit([20.0, 20.0, 22.0], [0.0, 0.0, -1.0]).unwrap();
        assert!((d - 22.0).abs() < 1e-3, "exit at {d}");
    }

    #[test]
    fn xy_pins_the_vertical_holes() {
        let mesh = merge(vec![
            cube(40.0),
            cuboid([6.0, 12.0, 8.0], [34.0, 28.0, 30.0], true),
        ]);
        let drain = auto_drain_holes(&mesh, 2.0, z_down(), Some([20.0, 20.0])).unwrap();
        assert!(drain
            .holes
            .iter()
            .all(|h| (h.x - 20.0).abs() < 1e-3 && (h.y - 20.0).abs() < 1e-3));
        // A column that misses the cavity is an error, not a silent fallback.
        let err = auto_drain_holes(&mesh, 2.0, z_down(), Some([2.0, 2.0]))
            .err()
            .unwrap();
        assert!(err.contains("no clear"), "{err}");
    }

    #[test]
    fn other_orientations_use_their_own_axis() {
        let mesh = two_cavities();
        // Plate on the -X side: holes run along x.
        let drain = auto_drain_holes(&mesh, 1.5, Axis::parse("-x").unwrap(), None).unwrap();
        assert!(drain.holes.iter().all(|h| h.axis == "-x" || h.axis == "+x"));
        assert_eq!(drain.holes.len(), 4);
        assert_eq!(stats(&punch(mesh, &drain.holes).mesh).cavities, 0);
    }

    #[test]
    fn a_curved_skin_falls_back_to_horizontal_holes_with_a_warning() {
        // A cube cavity inside an octahedral skin: every face slopes at 54 degrees under
        // any hole, so there is no square, flat spot to drill vertically.
        let r = 20.0;
        let positions = vec![
            Vec3::new(r, 0.0, 0.0),
            Vec3::new(-r, 0.0, 0.0),
            Vec3::new(0.0, r, 0.0),
            Vec3::new(0.0, -r, 0.0),
            Vec3::new(0.0, 0.0, r),
            Vec3::new(0.0, 0.0, -r),
        ];
        let mut triangles = Vec::new();
        for (ix, sx) in [(0u32, 1.0), (1, -1.0)] {
            for (iy, sy) in [(2u32, 1.0), (3, -1.0)] {
                for (iz, sz) in [(4u32, 1.0), (5, -1.0)] {
                    let flip = sx * sy * sz < 0.0;
                    triangles.push(if flip { [ix, iz, iy] } else { [ix, iy, iz] });
                }
            }
        }
        let skin = IndexedMesh {
            positions,
            triangles,
        };
        let mesh = merge(vec![skin, cuboid([-6.0; 3], [6.0; 3], true)]);
        let drain = auto_drain_holes(&mesh, 2.0, z_down(), None).unwrap();
        assert!(
            drain
                .warnings
                .iter()
                .any(|w| w.contains("fell back to a horizontal pair")),
            "{:?}",
            drain.warnings
        );
        assert!(drain
            .holes
            .iter()
            .all(|h| h.purpose.contains("horizontal fallback")));
        assert!(drain.holes.iter().all(|h| h.direction[2] == 0.0));
    }
}

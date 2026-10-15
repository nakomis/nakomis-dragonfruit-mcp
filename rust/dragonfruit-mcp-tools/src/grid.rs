//! A uniform 3-D grid of a mesh's triangles, for the two questions a hole in any
//! direction asks (NDFM-19): what does a ray from here cross, and is any surface
//! within `d` of this point. `drain::Columns` answers the first only along an axis.

use std::collections::HashSet;

use dragonfruit_mesh_repair::{IndexedMesh, Vec3};

use crate::ops::ray_tri;

/// Cells along the longest side at most, so a big model costs a few MB, not hundreds.
const MAX_CELLS_PER_AXIS: f32 = 200.0;
/// Cells are at least this big (mm): a hollowed mesh's triangles are about a voxel.
const MIN_CELL_MM: f32 = 0.25;

pub struct Grid<'a> {
    pub mesh: &'a IndexedMesh,
    lo: [f32; 3],
    hi: [f32; 3],
    cell: f32,
    n: [usize; 3],
    /// Compressed rows: the triangles of cell `c` are `items[start[c]..start[c + 1]]`.
    start: Vec<u32>,
    items: Vec<u32>,
}

pub fn sub(a: [f32; 3], b: [f32; 3]) -> [f32; 3] {
    [a[0] - b[0], a[1] - b[1], a[2] - b[2]]
}
pub fn dot(a: [f32; 3], b: [f32; 3]) -> f32 {
    a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
}
pub fn cross(a: [f32; 3], b: [f32; 3]) -> [f32; 3] {
    [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    ]
}
pub fn add(p: [f32; 3], d: [f32; 3], s: f32) -> [f32; 3] {
    [p[0] + d[0] * s, p[1] + d[1] * s, p[2] + d[2] * s]
}
pub fn unit(d: [f32; 3]) -> [f32; 3] {
    let l = dot(d, d).sqrt();
    [d[0] / l, d[1] / l, d[2] / l]
}

/// Two unit vectors square to `dir` and to each other. For an axis-aligned `dir` they
/// are the other two axes in increasing order, as `drain::Axis::plane` gives.
pub fn basis(dir: [f32; 3]) -> ([f32; 3], [f32; 3]) {
    if let Some(k) = (0..3).find(|&k| (dir[k].abs() - 1.0).abs() < 1e-4) {
        let (a, b) = match k {
            0 => (1, 2),
            1 => (0, 2),
            _ => (0, 1),
        };
        let mut u = [0.0; 3];
        let mut v = [0.0; 3];
        u[a] = 1.0;
        v[b] = 1.0;
        return (u, v);
    }
    // Cross with the axis the direction is least along.
    let k = (0..3)
        .min_by(|&i, &j| dir[i].abs().total_cmp(&dir[j].abs()))
        .unwrap_or(0);
    let mut e = [0.0; 3];
    e[k] = 1.0;
    let u = unit(cross(dir, e));
    (u, cross(dir, u))
}

fn corners(mesh: &IndexedMesh, t: usize) -> [[f32; 3]; 3] {
    mesh.triangles[t].map(|v| {
        let p = mesh.positions[v as usize];
        [p.x, p.y, p.z]
    })
}

/// Squared distance from `p` to triangle `abc` (Ericson, Real-Time Collision Detection 5.1.5).
pub fn point_triangle_dist2(p: [f32; 3], [a, b, c]: [[f32; 3]; 3]) -> f32 {
    let d2 = |q: [f32; 3]| {
        let d = sub(p, q);
        dot(d, d)
    };
    let (ab, ac, ap) = (sub(b, a), sub(c, a), sub(p, a));
    let (d1, d2_) = (dot(ab, ap), dot(ac, ap));
    if d1 <= 0.0 && d2_ <= 0.0 {
        return d2(a);
    }
    let bp = sub(p, b);
    let (d3, d4) = (dot(ab, bp), dot(ac, bp));
    if d3 >= 0.0 && d4 <= d3 {
        return d2(b);
    }
    let vc = d1 * d4 - d3 * d2_;
    if vc <= 0.0 && d1 >= 0.0 && d3 <= 0.0 {
        return d2(add(a, ab, d1 / (d1 - d3)));
    }
    let cp = sub(p, c);
    let (d5, d6) = (dot(ab, cp), dot(ac, cp));
    if d6 >= 0.0 && d5 <= d6 {
        return d2(c);
    }
    let vb = d5 * d2_ - d1 * d6;
    if vb <= 0.0 && d2_ >= 0.0 && d6 <= 0.0 {
        return d2(add(a, ac, d2_ / (d2_ - d6)));
    }
    let va = d3 * d6 - d5 * d4;
    if va <= 0.0 && (d4 - d3) >= 0.0 && (d5 - d6) >= 0.0 {
        return d2(add(b, sub(c, b), (d4 - d3) / ((d4 - d3) + (d5 - d6))));
    }
    let denom = 1.0 / (va + vb + vc);
    let (v, w) = (vb * denom, vc * denom);
    d2(add(add(a, ab, v), ac, w))
}

impl<'a> Grid<'a> {
    pub fn new(mesh: &'a IndexedMesh) -> Self {
        let bb = mesh.bbox();
        let lo = [bb.min.x, bb.min.y, bb.min.z];
        let hi = [bb.max.x, bb.max.y, bb.max.z];
        let extent = (0..3).map(|k| hi[k] - lo[k]).fold(0.0, f32::max);
        let cell = (extent / MAX_CELLS_PER_AXIS).max(MIN_CELL_MM);
        let n = [0, 1, 2].map(|k| ((hi[k] - lo[k]) / cell) as usize + 1);
        let mut g = Self {
            mesh,
            lo,
            hi,
            cell,
            n,
            start: Vec::new(),
            items: Vec::new(),
        };
        // Two passes: count, then fill, so the cells are one allocation.
        let ranges: Vec<([usize; 3], [usize; 3])> = (0..mesh.triangles.len())
            .map(|t| {
                let c = corners(mesh, t);
                let lo =
                    [0, 1, 2].map(|k| g.index(k, c.iter().map(|p| p[k]).fold(f32::MAX, f32::min)));
                let hi =
                    [0, 1, 2].map(|k| g.index(k, c.iter().map(|p| p[k]).fold(f32::MIN, f32::max)));
                (lo, hi)
            })
            .collect();
        let cells = n[0] * n[1] * n[2];
        let mut count = vec![0u32; cells + 1];
        for (lo, hi) in &ranges {
            g.each_cell(*lo, *hi, |c| count[c + 1] += 1);
        }
        for c in 0..cells {
            count[c + 1] += count[c];
        }
        let mut fill = count.clone();
        let mut items = vec![0u32; count[cells] as usize];
        for (t, (lo, hi)) in ranges.iter().enumerate() {
            g.each_cell(*lo, *hi, |c| {
                items[fill[c] as usize] = t as u32;
                fill[c] += 1;
            });
        }
        g.start = count;
        g.items = items;
        g
    }

    fn index(&self, k: usize, v: f32) -> usize {
        (((v - self.lo[k]) / self.cell).floor().max(0.0) as usize).min(self.n[k] - 1)
    }

    fn flat(&self, c: [usize; 3]) -> usize {
        (c[2] * self.n[1] + c[1]) * self.n[0] + c[0]
    }

    fn each_cell(&self, lo: [usize; 3], hi: [usize; 3], mut f: impl FnMut(usize)) {
        for z in lo[2]..=hi[2] {
            for y in lo[1]..=hi[1] {
                for x in lo[0]..=hi[0] {
                    f(self.flat([x, y, z]));
                }
            }
        }
    }

    fn cell_items(&self, c: usize) -> &[u32] {
        &self.items[self.start[c] as usize..self.start[c + 1] as usize]
    }

    /// Every triangle a ray from `origin` along unit `dir` crosses within `max_t`, as
    /// `(t, triangle, leaving_solid)`, nearest first.
    pub fn hits(&self, origin: [f32; 3], dir: [f32; 3], max_t: f32) -> Vec<(f32, usize, bool)> {
        let mut hits = Vec::new();
        self.walk(origin, dir, max_t, |h| {
            hits.push(h);
            true
        });
        hits
    }

    /// Feed `visit` the triangles a ray from `origin` along unit `dir` crosses within
    /// `max_t`, nearest first, as `(t, triangle, leaving_solid)`, until it returns false.
    /// Walks the cells the ray passes through (Amanatides and Woo), so stopping early
    /// skips the rest of the ray.
    pub fn walk(
        &self,
        origin: [f32; 3],
        dir: [f32; 3],
        max_t: f32,
        mut visit: impl FnMut((f32, usize, bool)) -> bool,
    ) {
        // Clip the ray to the grid's box (slabs), padded so faces on the box are kept.
        let pad = 1e-3;
        let (mut t0, mut t1) = (0.0_f32, max_t);
        for k in 0..3 {
            let (lo, hi) = (self.lo[k] - pad, self.hi[k] + pad);
            if dir[k].abs() < 1e-12 {
                if origin[k] < lo || origin[k] > hi {
                    return;
                }
            } else {
                let (a, b) = ((lo - origin[k]) / dir[k], (hi - origin[k]) / dir[k]);
                t0 = t0.max(a.min(b));
                t1 = t1.min(a.max(b));
            }
        }
        if t0 > t1 {
            return;
        }
        let p = add(origin, dir, t0);
        let mut c = [0, 1, 2].map(|k| self.index(k, p[k]));
        let mut step = [0i64; 3];
        let mut next = [f32::MAX; 3];
        let mut delta = [f32::MAX; 3];
        for k in 0..3 {
            if dir[k] > 1e-12 {
                step[k] = 1;
                let edge = self.lo[k] + (c[k] + 1) as f32 * self.cell;
                next[k] = t0 + (edge - p[k]) / dir[k];
                delta[k] = self.cell / dir[k];
            } else if dir[k] < -1e-12 {
                step[k] = -1;
                let edge = self.lo[k] + c[k] as f32 * self.cell;
                next[k] = t0 + (edge - p[k]) / dir[k];
                delta[k] = -self.cell / dir[k];
            }
        }
        let (o, d) = (
            Vec3::new(origin[0], origin[1], origin[2]),
            Vec3::new(dir[0], dir[1], dir[2]),
        );
        let mut seen: HashSet<u32> = HashSet::new();
        // Hits found but not yet passed on: one beyond the current cell may still have
        // a nearer one waiting in a later cell.
        let mut pending: Vec<(f32, usize, bool)> = Vec::new();
        loop {
            for &i in self.cell_items(self.flat(c)) {
                if seen.insert(i) {
                    if let Some((t, l)) = ray_tri(self.mesh, i as usize, o, d) {
                        if t <= max_t {
                            pending.push((t, i as usize, l));
                        }
                    }
                }
            }
            let k = (0..3)
                .min_by(|&i, &j| next[i].total_cmp(&next[j]))
                .unwrap_or(0);
            let moved = c[k] as i64 + step[k];
            let last = next[k] > t1 || moved < 0 || moved >= self.n[k] as i64;
            // Everything nearer than the far side of this cell has been found.
            let done_to = if last { f32::INFINITY } else { next[k] };
            pending.sort_by(|x, y| y.0.total_cmp(&x.0));
            while pending.last().is_some_and(|h| h.0 < done_to) {
                let h = pending.pop().unwrap_or_default();
                if !visit(h) {
                    return;
                }
            }
            if last {
                return;
            }
            c[k] = moved as usize;
            next[k] += delta[k];
        }
    }

    /// Whether any triangle that `keep` accepts lies closer than `d` to `p`.
    pub fn any_within(&self, p: [f32; 3], d: f32, keep: impl Fn(usize) -> bool) -> bool {
        let lo = [0, 1, 2].map(|k| self.index(k, p[k] - d));
        let hi = [0, 1, 2].map(|k| self.index(k, p[k] + d));
        let d2 = d * d;
        for z in lo[2]..=hi[2] {
            for y in lo[1]..=hi[1] {
                for x in lo[0]..=hi[0] {
                    for &t in self.cell_items(self.flat([x, y, z])) {
                        let t = t as usize;
                        if keep(t) && point_triangle_dist2(p, corners(self.mesh, t)) < d2 {
                            return true;
                        }
                    }
                }
            }
        }
        false
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ops::ray_hits;
    use crate::ops::testutil::*;

    #[test]
    fn grid_rays_agree_with_brute_force_in_any_direction() {
        let mesh = two_cavities();
        let grid = Grid::new(&mesh);
        let origin = [11.03, 20.07, 19.01];
        for (a, e) in [
            (0.0_f32, 0.0_f32),
            (0.7, 0.3),
            (2.0, -0.9),
            (4.0, 1.2),
            (5.5, 0.0),
        ] {
            let dir = [a.cos() * e.cos(), a.sin() * e.cos(), e.sin()];
            let want = ray_hits(
                &mesh,
                Vec3::new(origin[0], origin[1], origin[2]),
                Vec3::new(dir[0], dir[1], dir[2]),
            );
            let got = grid.hits(origin, dir, 1000.0);
            assert_eq!(
                got.iter().map(|h| h.1).collect::<Vec<_>>(),
                want.iter().map(|h| h.1).collect::<Vec<_>>(),
                "direction {dir:?}"
            );
        }
        // A ray that starts outside the box still finds the box.
        assert_eq!(
            grid.hits([-5.0, 20.03, 20.01], [1.0, 0.0, 0.0], 100.0)
                .len(),
            6
        );
        // And max_t cuts it short.
        assert_eq!(
            grid.hits([-5.0, 20.03, 20.01], [1.0, 0.0, 0.0], 12.0).len(),
            2
        );
    }

    #[test]
    fn distance_to_a_triangle_covers_face_edge_and_corner() {
        let tri = [[0.0, 0.0, 0.0], [4.0, 0.0, 0.0], [0.0, 4.0, 0.0]];
        assert!((point_triangle_dist2([1.0, 1.0, 3.0], tri) - 9.0).abs() < 1e-5);
        assert!((point_triangle_dist2([2.0, -2.0, 0.0], tri) - 4.0).abs() < 1e-5);
        assert!((point_triangle_dist2([-3.0, -4.0, 0.0], tri) - 25.0).abs() < 1e-5);
        assert!((point_triangle_dist2([3.0, 3.0, 0.0], tri) - 2.0).abs() < 1e-5);
    }

    #[test]
    fn any_within_sees_only_what_it_is_asked_about() {
        let mesh = cube(20.0);
        let grid = Grid::new(&mesh);
        assert!(grid.any_within([10.0, 10.0, 1.5], 2.0, |_| true));
        assert!(!grid.any_within([10.0, 10.0, 2.5], 2.0, |_| true));
        // The bottom face is triangles 0 and 1: without them nothing is near.
        assert!(!grid.any_within([10.0, 10.0, 1.5], 2.0, |t| t > 1));
    }

    #[test]
    fn basis_is_square_to_the_direction() {
        assert_eq!(basis([0.0, 0.0, -1.0]), ([1.0, 0.0, 0.0], [0.0, 1.0, 0.0]));
        let d = unit([0.3, -0.5, 0.8]);
        let (u, v) = basis(d);
        for (x, y) in [(u, d), (v, d), (u, v)] {
            assert!(dot(x, y).abs() < 1e-5);
        }
        assert!((dot(u, u) - 1.0).abs() < 1e-5 && (dot(v, v) - 1.0).abs() < 1e-5);
    }
}

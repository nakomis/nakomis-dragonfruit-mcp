//! The vent at a cavity's highest point, by the shortest safe route out (NDFM-19).
//!
//! A void with one opening cups on every lift (it cannot swap resin) and holds its
//! resin afterwards by surface tension; it needs a drain at its lowest point and a
//! vent at its highest. The vent need not be vertical: the first Chi's vent, 3.6 mm
//! out of the side of his head, drained the bird-shaped void during the print, where a
//! vertical one would have run the length of the head.
//!
//! The route is a straight bore from the top of the void, in whichever upward or
//! sideways direction reaches the outside soonest, provided it never comes closer to
//! the model's surface than it is to its own exit: a bore that runs along just under
//! a flank can break out early through a thin skin, or leave a wall that cures through.
//! Each point on it must keep at least `min(r + VENT_WALL_MM, VENT_EXIT_TAPER * left)`
//! from every surface but its own cavity's, `left` being how far it still has to go;
//! near the exit the allowance shrinks so the bore can come out at an angle.

use dragonfruit_mesh_repair::IndexedMesh;
use rayon::prelude::*;

use crate::drain::{Axis, Columns, EXTEND_MAX_MM, EXTEND_STEP_MM, FULL_WIDTH_MARGIN_MM};
use crate::grid::{add, basis, Grid};
use crate::ops::Shells;

/// Solid kept round the bore (beyond its radius) everywhere but near the exit.
pub const VENT_WALL_MM: f32 = 1.0;
/// Near the exit the bore may come this close to the surface, times the distance left.
const VENT_EXIT_TAPER: f32 = 0.7;
/// Vent starts are taken where the cavity's roof is this close to its top.
const VENT_BAND_MM: f32 = 1.0;
const VENT_CELL_MM: f32 = 1.0;
/// At most about this many starts, from a roof grid and the top vertices.
const VENT_MAX_STARTS: usize = 200;
/// Directions: azimuth every 15 degrees at these elevations above horizontal, and
/// straight up. Never downwards: the vent is for air to come in at the top.
const VENT_AZIMUTH_STEP_DEG: f32 = 15.0;
const VENT_ELEVATIONS_DEG: [f32; 6] = [0.0, 15.0, 30.0, 45.0, 60.0, 75.0];
/// Routes are tried this many at a time, in parallel.
const EVAL_BATCH: usize = 64;
/// The route is sampled this often along its length.
const VENT_SAMPLE_MM: f32 = 0.25;
/// How far below the top vertex the start begins, so it is in the void.
const START_INSET_MM: f32 = 0.3;
const JITTER: [f32; 2] = [0.0137, 0.0071];

/// A vent chosen by `shortest_vent`.
#[derive(Debug, Clone)]
pub struct Route {
    pub start: [f32; 3],
    pub dir: [f32; 3],
    /// From the start to where the bore leaves the outer skin.
    pub exit_mm: f32,
    /// How far the start was lowered (beyond the inset) so the bore's whole disc is in
    /// the void; the void's top is this much higher than the top of the bore.
    pub drop_mm: f32,
    pub full_width: bool,
    /// From the start to where the whole bore, not just its axis, is out of the skin:
    /// a bore leaving a face at a slant clears it r tan(angle) after its axis does.
    pub through_mm: f32,
}

struct Ctx<'a> {
    shells: &'a Shells,
    grid: &'a Grid<'a>,
    axis: Axis,
    cav: usize,
    radius: f32,
    /// Coordinate on the axis well beyond the mesh, for its length limit on rays.
    max_t: f32,
}

impl Ctx<'_> {
    /// From `start` along `dir`: where the bore leaves the outer skin, and the stretches
    /// (as `t` ranges) it spends in its own cavity. None if it never comes out.
    fn exit(&self, start: [f32; 3], dir: [f32; 3]) -> Option<(f32, Vec<(f32, f32)>)> {
        let mut inside = Vec::new();
        let mut from = Some(0.0);
        let mut exit = None;
        self.grid.walk(start, dir, self.max_t, |(t, tri, leaving)| {
            let shell = self.shells.triangle_shell[tri];
            if shell == self.cav {
                // A cavity's normals point into its void: hitting one we are not leaving
                // material through means we are leaving the void.
                match (from, leaving) {
                    (Some(f), false) => {
                        inside.push((f, t));
                        from = None;
                    }
                    (None, true) => from = Some(t),
                    _ => {}
                }
                true
            } else if leaving && self.shells.volumes[shell] > 0.0 {
                if let Some(f) = from {
                    inside.push((f, t));
                }
                exit = Some(t);
                false
            } else {
                true
            }
        });
        exit.map(|t| (t, inside))
    }

    /// Whether `p` is in the void: the first wall of the cavity met going up is one the
    /// ray runs into material through (as `drain::in_cavity`, through the 3-D grid).
    fn in_void(&self, p: [f32; 3]) -> bool {
        let mut inside = false;
        self.grid
            .walk(p, self.axis.up(), self.max_t, |(_, tri, leaving)| {
                if self.shells.triangle_shell[tri] == self.cav {
                    inside = !leaving;
                    return false;
                }
                true
            });
        inside
    }

    /// Whether the bore's disc (radius plus margin) at `p`, square to `dir`, is all void.
    fn disc_in_void(&self, p: [f32; 3], rim: &[[f32; 3]]) -> bool {
        self.in_void(p) && rim.iter().all(|&o| self.in_void(add(p, o, 1.0)))
    }

    /// Offsets round the bore's rim (radius plus margin), square to `dir`, highest
    /// first: under a roof the top of the rim is the first to leave the void.
    fn rim(&self, dir: [f32; 3]) -> Vec<[f32; 3]> {
        let (u, v) = basis(dir);
        let ring = self.radius + FULL_WIDTH_MARGIN_MM;
        let mut rim: Vec<[f32; 3]> = (0..16)
            .map(|i| {
                let ang = 0.1 + i as f32 * std::f32::consts::FRAC_PI_8;
                add(add([0.0; 3], u, ring * ang.cos()), v, ring * ang.sin())
            })
            .collect();
        rim.sort_by(|x, y| self.axis.height(*y).total_cmp(&self.axis.height(*x)));
        rim
    }

    /// How far along `dir` the bore's whole disc is out of the skin: the latest exit of
    /// its axis and of eight points round its rim.
    fn through(&self, start: [f32; 3], dir: [f32; 3], exit_mm: f32) -> f32 {
        let (u, v) = basis(dir);
        (0..8)
            .filter_map(|i| {
                let ang = i as f32 * std::f32::consts::FRAC_PI_4;
                let q = add(
                    add(start, u, self.radius * ang.cos()),
                    v,
                    self.radius * ang.sin(),
                );
                self.exit(q, dir).map(|e| e.0)
            })
            .fold(exit_mm, f32::max)
    }

    /// Lower `start` (towards the plate) until the bore's disc is in the void.
    fn lower(&self, start: [f32; 3], dir: [f32; 3]) -> ([f32; 3], f32, bool) {
        let down = self.axis.down();
        let rim = self.rim(dir);
        let steps = (EXTEND_MAX_MM / EXTEND_STEP_MM) as usize;
        for i in 0..=steps {
            let s = i as f32 * EXTEND_STEP_MM;
            let p = add(start, down, s);
            if self.disc_in_void(p, &rim) {
                return (p, s, true);
            }
        }
        (start, 0.0, false)
    }

    /// Whether the bore keeps its distance from every surface but its own cavity's.
    fn clear(&self, start: [f32; 3], dir: [f32; 3], exit_mm: f32, inside: &[(f32, f32)]) -> bool {
        let wall = self.radius + VENT_WALL_MM;
        let n = (exit_mm / VENT_SAMPLE_MM).ceil() as usize;
        (0..=n).all(|i| {
            let t = (i as f32 * VENT_SAMPLE_MM).min(exit_mm);
            if inside.iter().any(|&(a, b)| t >= a && t <= b) {
                return true;
            }
            let need = wall.min(VENT_EXIT_TAPER * (exit_mm - t));
            need <= 0.0
                || !self.grid.any_within(add(start, dir, t), need, |tri| {
                    self.shells.triangle_shell[tri] != self.cav
                })
        })
    }
}

/// The candidate directions: up, and round the compass at each elevation.
fn directions(axis: Axis) -> Vec<[f32; 3]> {
    let (ia, ib) = axis.plane();
    let up = axis.up();
    let mut dirs = vec![up];
    for e in VENT_ELEVATIONS_DEG {
        let (se, ce) = e.to_radians().sin_cos();
        let mut a: f32 = 0.0;
        while a < 360.0 {
            let (sa, ca) = a.to_radians().sin_cos();
            let mut d = [0.0; 3];
            d[ia] = ce * ca;
            d[ib] = ce * sa;
            d[axis.k] = se * up[axis.k];
            dirs.push(d);
            a += VENT_AZIMUTH_STEP_DEG;
        }
    }
    dirs
}

/// The shortest clear route out from the top of cavity `cav`. `pts` are the cavity's
/// vertices as (a, b, height) across and along `axis`. Routes whose whole disc cannot
/// open into the void come last; among the rest, the shortest wins, counting how far
/// the start had to be lowered (air above the bore's top stays trapped).
#[allow(clippy::too_many_arguments)]
pub fn shortest_vent(
    mesh: &IndexedMesh,
    shells: &Shells,
    cols: &Columns,
    grid: &Grid,
    axis: Axis,
    cav: usize,
    radius: f32,
    pts: &[(f32, f32, f32)],
) -> Result<Route, String> {
    let bb = mesh.bbox();
    let diagonal = ((bb.max.x - bb.min.x).powi(2)
        + (bb.max.y - bb.min.y).powi(2)
        + (bb.max.z - bb.min.z).powi(2))
    .sqrt();
    let ctx = Ctx {
        shells,
        grid,
        axis,
        cav,
        radius,
        max_t: diagonal + 1.0,
    };
    let top = pts.iter().map(|p| p.2).fold(f32::MIN, f32::max);
    let (ia, ib) = axis.plane();
    let up = axis.up();
    let at = |a: f32, b: f32, h: f32| {
        let mut p = [0.0; 3];
        p[ia] = a;
        p[ib] = b;
        p[axis.k] = -axis.sign * h;
        p
    };
    // Starts: the cavity's roof sampled on a grid over its footprint (a vertex sits on
    // an edge or corner, where the bore's disc would half miss the void), plus its
    // vertices, wherever the roof is within the band at the top.
    let (a0, a1) = pts
        .iter()
        .fold((f32::MAX, f32::MIN), |(l, h), p| (l.min(p.0), h.max(p.0)));
    let (b0, b1) = pts
        .iter()
        .fold((f32::MAX, f32::MIN), |(l, h), p| (l.min(p.1), h.max(p.1)));
    let step = ((a1 - a0) * (b1 - b0) / VENT_MAX_STARTS as f32)
        .sqrt()
        .max(VENT_CELL_MM);
    let mut columns: Vec<(f32, f32)> = pts
        .iter()
        .filter(|p| top - p.2 <= VENT_BAND_MM)
        .map(|p| (p.0, p.1))
        .collect();
    let mut a = a0 + step / 2.0;
    while a < a1 {
        let mut b = b0 + step / 2.0;
        while b < b1 {
            columns.push((a, b));
            b += step;
        }
        a += step;
    }
    let below = -axis.sign * (pts.iter().map(|p| p.2).fold(f32::MAX, f32::min) - 1.0);
    let mut starts: Vec<[f32; 3]> = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for (a, b) in columns {
        let (a, b) = (a + JITTER[0], b + JITTER[1]);
        if !seen.insert(((a / 0.25).floor() as i32, (b / 0.25).floor() as i32)) {
            continue;
        }
        let mut origin = at(a, b, 0.0);
        origin[axis.k] = below;
        // The roof over this column: the last wall of the cavity met going up that the
        // ray runs into material through.
        let roof = cols
            .hits(origin, up)
            .into_iter()
            .rfind(|h| shells.triangle_shell[h.1] == cav && !h.2);
        if let Some((t, _, _)) = roof {
            let h = axis.height(add(origin, up, t));
            if top - h <= VENT_BAND_MM {
                starts.push(at(a, b, h - START_INSET_MM));
            }
        }
    }
    if starts.len() > VENT_MAX_STARTS {
        // Highest first, then spread evenly over the rest.
        starts.sort_by(|x, y| axis.height(*y).total_cmp(&axis.height(*x)));
        let stride = starts.len().div_ceil(VENT_MAX_STARTS);
        starts = starts.into_iter().step_by(stride).collect();
    }

    // Every route's length from its un-lowered start, shortest first: lowering only
    // ever lengthens an upward route, so once a raw length passes the best score so
    // far, nothing later can beat it.
    let dirs = directions(axis);
    let mut raw: Vec<(f32, usize, usize)> = Vec::new();
    for (si, &s) in starts.iter().enumerate() {
        for (di, &d) in dirs.iter().enumerate() {
            if let Some((t, _)) = ctx.exit(s, d) {
                raw.push((t, si, di));
            }
        }
    }
    if raw.is_empty() {
        return Err("no straight line from the top of the cavity reaches the outside".into());
    }
    raw.sort_by(|x, y| x.0.total_cmp(&y.0).then((x.1, x.2).cmp(&(y.1, y.2))));
    // In batches, in parallel, then merged in order, so the early stop still holds.
    let mut best: Option<(f32, Route)> = None;
    let mut rejected = 0usize;
    'batches: for batch in raw.chunks(EVAL_BATCH) {
        let routes: Vec<(f32, Option<Route>)> = batch
            .par_iter()
            .map(|&(len, si, di)| {
                if best
                    .as_ref()
                    .is_some_and(|(score, r)| r.full_width && len > *score)
                {
                    return (len, None);
                }
                let dir = dirs[di];
                let (start, drop_mm, full_width) = ctx.lower(starts[si], dir);
                let route = ctx.exit(start, dir).and_then(|(exit_mm, inside)| {
                    ctx.clear(start, dir, exit_mm, &inside).then_some(Route {
                        start,
                        dir,
                        exit_mm,
                        drop_mm,
                        full_width,
                        through_mm: exit_mm,
                    })
                });
                (len, route)
            })
            .collect();
        for (len, route) in routes {
            if best
                .as_ref()
                .is_some_and(|(score, r)| r.full_width && len > *score)
            {
                break 'batches;
            }
            let Some(r) = route else {
                rejected += 1;
                continue;
            };
            let score = r.exit_mm + r.drop_mm;
            if best
                .as_ref()
                .is_none_or(|(s, b)| (!r.full_width, score) < (!b.full_width, *s))
            {
                best = Some((score, r));
            }
        }
    }
    best.map(|(_, mut r)| {
        r.through_mm = ctx.through(r.start, r.dir, r.exit_mm);
        r
    })
    .ok_or_else(|| {
        format!(
            "every straight route from the top of the cavity ({rejected} tried) comes within {:.1} mm of the surface before its exit",
            radius + VENT_WALL_MM
        )
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ops::testutil::*;

    fn route(mesh: &IndexedMesh, radius: f32) -> Result<Route, String> {
        let axis = Axis::parse("-z").unwrap();
        let shells = Shells::of(mesh);
        let cols = Columns::new(mesh, axis);
        let grid = Grid::new(mesh);
        let cav = shells.cavity_ids()[0];
        let mut pts = Vec::new();
        for (ti, t) in mesh.triangles.iter().enumerate() {
            if shells.triangle_shell[ti] == cav {
                for &vi in t {
                    let p = mesh.positions[vi as usize];
                    pts.push((p.x, p.y, p.z));
                }
            }
        }
        shortest_vent(mesh, &shells, &cols, &grid, axis, cav, radius, &pts)
    }

    #[test]
    fn a_cavity_near_one_side_vents_sideways_at_its_top() {
        // 40 mm cube; cavity x 4..20, y 10..30, z 6..34: 6 mm of roof, 4 mm of wall on -x.
        let mesh = merge(vec![
            cube(40.0),
            cuboid([4.0, 10.0, 6.0], [20.0, 30.0, 34.0], true),
        ]);
        let r = route(&mesh, 1.0).unwrap();
        assert!(r.full_width);
        // Out through the -x wall, horizontally: 4 mm plus the corner it starts from.
        assert!(r.dir[0] < -0.95, "{r:?}");
        assert!(r.exit_mm < 5.0, "{r:?}");
        // At the top: the bore's top edge just under the roof (r + margin + inset).
        assert!(
            r.start[2] > 34.0 - 1.0 - FULL_WIDTH_MARGIN_MM - 0.3 - EXTEND_STEP_MM - 0.01,
            "{r:?}"
        );
        // Clear of the y walls by the wall allowance all along.
        assert!(
            r.start[1] >= 10.0 + 1.0 && r.start[1] <= 30.0 - 1.0,
            "{r:?}"
        );
    }

    #[test]
    fn a_thin_roof_vents_straight_up() {
        // 3 mm of roof against 10 mm of side wall either way.
        let mesh = merge(vec![
            cube(40.0),
            cuboid([10.0, 10.0, 6.0], [30.0, 30.0, 37.0], true),
        ]);
        let r = route(&mesh, 1.0).unwrap();
        assert!(r.dir[2] > 0.99, "{r:?}");
        assert!((r.exit_mm - 3.3).abs() < 0.1, "{r:?}");
        assert_eq!(r.drop_mm, 0.0);
    }

    #[test]
    fn a_route_grazing_a_flank_is_refused() {
        // Straight up a solid 20 mm cube, 1.5 mm in from its -x face: under r + 1 = 2 mm
        // of wall all the way, so refused; 5 mm in it is clear (the top face only comes
        // close as the exit does).
        let mesh = cube(20.0);
        let shells = Shells::of(&mesh);
        let axis = Axis::parse("-z").unwrap();
        let grid = Grid::new(&mesh);
        let ctx = Ctx {
            shells: &shells,
            grid: &grid,
            axis,
            cav: usize::MAX,
            radius: 1.0,
            max_t: 100.0,
        };
        let up = [0.0, 0.0, 1.0];
        // `exit` takes the start to be in the void; here it is in solid, so no void.
        for (x, ok) in [(1.5, false), (5.0, true)] {
            let start = [x, 10.0, 5.0];
            let (exit, _) = ctx.exit(start, up).unwrap();
            assert!((exit - 15.0).abs() < 1e-3);
            assert_eq!(ctx.clear(start, up, exit, &[]), ok, "x {x}");
        }
        // Through a thin wall square on is fine: the wall is only near the exit.
        let side = [1.5, 10.0, 10.0];
        let (exit, _) = ctx.exit(side, [-1.0, 0.0, 0.0]).unwrap();
        assert!(ctx.clear(side, [-1.0, 0.0, 0.0], exit, &[]));
        // A slanted exit: the rim clears the face r tan 45 = 1 mm after the axis.
        let d = 0.5_f32.sqrt();
        let slant = [5.0, 10.0, 5.0];
        let (exit, _) = ctx.exit(slant, [-d, 0.0, d]).unwrap();
        let through = ctx.through(slant, [-d, 0.0, d], exit);
        assert!((through - exit - 1.0).abs() < 0.05, "{exit} {through}");
    }

    #[test]
    fn directions_point_up_or_sideways_never_down() {
        let dirs = directions(Axis::parse("-z").unwrap());
        assert_eq!(dirs.len(), 1 + 6 * 24);
        assert!(dirs.iter().all(|d| d[2] >= 0.0));
        let dirs = directions(Axis::parse("+x").unwrap());
        assert!(dirs.iter().all(|d| d[0] <= 0.0));
    }
}

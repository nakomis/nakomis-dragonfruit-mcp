//! The geometry behind the `hollow` and `punch` subcommands: calls into
//! DragonFruit's mesh-repair crate the same way the desktop app's Tauri
//! wrappers do, plus the measurements and the drain-hole placement the app
//! leaves to the user.

use dragonfruit_mesh_repair::analysis::analyze_lightweight;
use dragonfruit_mesh_repair::{
    hollow_voxel, punch_cylinders, HolePunchOptions, HolePunchSpec, HollowOptions, HollowOutcome,
    IndexedMesh, Vec3,
};
use serde::{Deserialize, Serialize};

/// The desktop app's defaults (`useHollowingManager.ts`, `useHolePunchManager.ts`).
pub const DEFAULT_VOXEL_MM: f32 = 0.65;
pub const DEFAULT_HOLE_RADIUS_MM: f32 = 2.0;
/// Extra length past the outer skin so the cylinder's cap never leaves a membrane.
const EXIT_MARGIN_MM: f32 = 1.0;
/// Cavities below this fraction of the biggest one are voxel crumbs, not worth a hole.
const CAVITY_CRUMB_FRACTION: f64 = 0.01;

/// The app's `computeVoxelResolution`: cells along the longest axis, clamped to 24..=192.
pub fn voxel_resolution(voxel_mm: f32, mesh: &IndexedMesh) -> u16 {
    let b = mesh.bbox();
    let extent = (b.max.x - b.min.x)
        .max(b.max.y - b.min.y)
        .max(b.max.z - b.min.z);
    ((extent / voxel_mm.max(0.05)).round() as i64).clamp(24, 192) as u16
}

#[derive(Debug, Clone, Serialize)]
pub struct MeshStats {
    pub triangles: usize,
    pub volume_mm3: f64,
    pub volume_ml: f64,
    /// Connected shells. A hollowed model is the outer skin plus one per sealed cavity.
    pub shells: usize,
    /// Shells that are voids (inward-facing, negative volume).
    pub cavities: usize,
    pub watertight: bool,
    pub boundary_edges: usize,
    pub non_manifold_edges: usize,
    pub bbox_min: [f32; 3],
    pub bbox_max: [f32; 3],
}

/// Per-shell connected components and signed volumes (union-find over shared vertices).
pub struct Shells {
    pub triangle_shell: Vec<usize>,
    pub volumes: Vec<f64>,
}

impl Shells {
    pub fn of(mesh: &IndexedMesh) -> Self {
        let mut parent: Vec<u32> = (0..mesh.positions.len() as u32).collect();
        fn find(parent: &mut [u32], mut i: u32) -> u32 {
            while parent[i as usize] != i {
                parent[i as usize] = parent[parent[i as usize] as usize];
                i = parent[i as usize];
            }
            i
        }
        for t in &mesh.triangles {
            let (a, b, c) = (t[0], t[1], t[2]);
            for (x, y) in [(a, b), (b, c)] {
                let (rx, ry) = (find(&mut parent, x), find(&mut parent, y));
                if rx != ry {
                    parent[rx as usize] = ry;
                }
            }
        }
        let mut ids = std::collections::HashMap::new();
        let mut triangle_shell = Vec::with_capacity(mesh.triangles.len());
        let mut volumes: Vec<f64> = Vec::new();
        for t in &mesh.triangles {
            let root = find(&mut parent, t[0]);
            let next = ids.len();
            let id = *ids.entry(root).or_insert(next);
            if id == volumes.len() {
                volumes.push(0.0);
            }
            let (a, b, c) = (
                mesh.positions[t[0] as usize],
                mesh.positions[t[1] as usize],
                mesh.positions[t[2] as usize],
            );
            volumes[id] += f64::from(a.dot(b.cross(c))) / 6.0;
            triangle_shell.push(id);
        }
        Self {
            triangle_shell,
            volumes,
        }
    }

    pub fn cavity_ids(&self) -> Vec<usize> {
        (0..self.volumes.len())
            .filter(|&i| self.volumes[i] < -1e-3)
            .collect()
    }
}

pub fn stats(mesh: &IndexedMesh) -> MeshStats {
    let a = analyze_lightweight(mesh);
    let shells = Shells::of(mesh);
    MeshStats {
        triangles: mesh.triangle_count(),
        volume_mm3: a.signed_volume,
        volume_ml: a.signed_volume / 1000.0,
        shells: shells.volumes.len(),
        cavities: shells.cavity_ids().len(),
        watertight: a.is_watertight && a.non_manifold_edges == 0,
        boundary_edges: a.boundary_edges,
        non_manifold_edges: a.non_manifold_edges,
        bbox_min: a.bbox_min,
        bbox_max: a.bbox_max,
    }
}

/// Binary STL, buffered. The crate's own `stl::write_binary` makes one syscall per
/// float, which takes most of a minute for the half-million triangles of a
/// hollowed model.
pub fn write_binary_stl(mesh: &IndexedMesh, path: &std::path::Path) -> std::io::Result<()> {
    use std::io::Write;
    let mut out = std::io::BufWriter::with_capacity(1 << 20, std::fs::File::create(path)?);
    out.write_all(&[0u8; 80])?;
    out.write_all(&(mesh.triangles.len() as u32).to_le_bytes())?;
    for face in 0..mesh.triangles.len() as u32 {
        let n = mesh.tri_normal(face);
        let [a, b, c] = mesh.tri_positions(face);
        for f in [n.x, n.y, n.z, a.x, a.y, a.z, b.x, b.y, b.z, c.x, c.y, c.z] {
            out.write_all(&f.to_le_bytes())?;
        }
        out.write_all(&0u16.to_le_bytes())?;
    }
    out.flush()
}

/// Hollow `mesh` exactly as the app's "Apply" does.
pub fn hollow(mesh: IndexedMesh, options: &HollowOptions) -> HollowOutcome {
    hollow_voxel(mesh, options)
}

/// A drain hole in model coordinates (mm), the unit this tool speaks.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Hole {
    pub x: f32,
    pub y: f32,
    pub z: f32,
    #[serde(default, alias = "radius_mm")]
    pub radius: Option<f32>,
    #[serde(default)]
    pub direction: Option<[f32; 3]>,
    /// Punch depth from (x, y, z) along `direction`. Default: through everything on the axis.
    #[serde(default, alias = "length_mm")]
    pub length: Option<f32>,
}

/// A hole with every field resolved, plus why it is where it is.
#[derive(Debug, Clone, Serialize)]
pub struct PlacedHole {
    pub x: f32,
    pub y: f32,
    pub z: f32,
    pub radius_mm: f32,
    pub direction: [f32; 3],
    pub length_mm: f32,
    pub note: String,
}

fn normalise(d: [f32; 3]) -> [f32; 3] {
    let l = (d[0] * d[0] + d[1] * d[1] + d[2] * d[2]).sqrt();
    if l <= 1e-9 {
        [0.0, 0.0, -1.0]
    } else {
        [d[0] / l, d[1] / l, d[2] / l]
    }
}

/// Every triangle the ray `origin + t*dir` crosses (t > 0), nearest first, as
/// `(t, triangle index, leaving_solid)`. Normals point out of the material, so a
/// hit whose normal faces along the ray is the ray leaving material.
fn ray_hits(mesh: &IndexedMesh, origin: Vec3, dir: Vec3) -> Vec<(f32, usize, bool)> {
    let mut hits = Vec::new();
    for (i, t) in mesh.triangles.iter().enumerate() {
        let (a, b, c) = (
            mesh.positions[t[0] as usize],
            mesh.positions[t[1] as usize],
            mesh.positions[t[2] as usize],
        );
        // Moller-Trumbore.
        let (e1, e2) = (b.sub(a), c.sub(a));
        let p = dir.cross(e2);
        let det = e1.dot(p);
        if det.abs() < 1e-12 {
            continue;
        }
        let inv = 1.0 / det;
        let s = origin.sub(a);
        let u = s.dot(p) * inv;
        if !(0.0..=1.0).contains(&u) {
            continue;
        }
        let q = s.cross(e1);
        let v = dir.dot(q) * inv;
        if v < 0.0 || u + v > 1.0 {
            continue;
        }
        let tt = e2.dot(q) * inv;
        if tt > 1e-4 {
            let n = e1.cross(e2);
            hits.push((tt, i, n.dot(dir) > 0.0));
        }
    }
    hits.sort_by(|x, y| x.0.total_cmp(&y.0));
    hits
}

/// Distance from `origin`, which must lie inside cavity `cavity`, along `dir` to the
/// outside of the model; `None` if the ray doesn't start in that cavity or never leaves.
fn exit_distance(
    mesh: &IndexedMesh,
    shells: &Shells,
    cavity: usize,
    origin: Vec3,
    dir: Vec3,
) -> Option<f32> {
    let hits = ray_hits(mesh, origin, dir);
    let first = hits.first()?;
    if shells.triangle_shell[first.1] != cavity {
        return None;
    }
    hits.iter()
        .find(|h| h.2 && shells.triangle_shell[h.1] != cavity)
        .map(|h| h.0)
}

/// Where to drill so a hollowed print drains, and why there.
///
/// The app has no automatic placement. For an upright print, resin drains from
/// the lowest point of the cavity, so for each real cavity this finds that point
/// and drills horizontally from just above the floor (the hole's bottom edge is
/// flush with it) out through the wall. It tries the four horizontal directions,
/// measures the material each would cut through, and takes the opposite pair
/// (+/-X or +/-Y) with the thinner total: one hole drains, the other vents so the
/// drain doesn't glug. Thin walls mean the least damage to surface detail, so raised
/// lettering on one face steers the holes to the other axis. Straight down is
/// avoided: it would open into the raft or supports.
pub fn auto_base_holes(
    mesh: &IndexedMesh,
    radius_mm: f32,
) -> Result<(Vec<PlacedHole>, Vec<String>), String> {
    let shells = Shells::of(mesh);
    let cavities = shells.cavity_ids();
    if cavities.is_empty() {
        return Err("the mesh has no internal cavity to drain; hollow it first".into());
    }
    let biggest = cavities
        .iter()
        .map(|&c| -shells.volumes[c])
        .fold(0.0, f64::max);
    let mut holes = Vec::new();
    let mut warnings = Vec::new();
    for &cav in &cavities {
        if -shells.volumes[cav] < biggest * CAVITY_CRUMB_FRACTION {
            warnings.push(format!(
                "ignored a tiny cavity of {:.3} ml",
                -shells.volumes[cav] / 1000.0
            ));
            continue;
        }
        // Lowest vertex of this cavity's surface.
        let mut low: Option<Vec3> = None;
        for (ti, t) in mesh.triangles.iter().enumerate() {
            if shells.triangle_shell[ti] != cav {
                continue;
            }
            for &vi in t {
                let p = mesh.positions[vi as usize];
                if low.is_none_or(|l| p.z < l.z) {
                    low = Some(p);
                }
            }
        }
        let low = low.ok_or("cavity has no triangles")?;
        let start = Vec3::new(low.x, low.y, low.z + radius_mm);
        let axes: [[f32; 3]; 2] = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]];
        let mut best: Option<(f32, [f32; 3], f32, f32)> = None;
        for axis in axes {
            let fwd = Vec3::new(axis[0], axis[1], axis[2]);
            let back = fwd.scale(-1.0);
            let (d1, d2) = (
                exit_distance(mesh, &shells, cav, start, fwd),
                exit_distance(mesh, &shells, cav, start, back),
            );
            if let (Some(d1), Some(d2)) = (d1, d2) {
                if best.is_none_or(|b| d1 + d2 < b.0) {
                    best = Some((d1 + d2, axis, d1, d2));
                }
            }
        }
        let (_, axis, d1, d2) = best.ok_or(
            "no horizontal line from the lowest point of the cavity reaches the outside on both sides",
        )?;
        for (sign, d) in [(1.0_f32, d1), (-1.0, d2)] {
            let dir = [axis[0] * sign, axis[1] * sign, axis[2] * sign];
            holes.push(PlacedHole {
                x: start.x,
                y: start.y,
                z: start.z,
                radius_mm,
                direction: dir,
                length_mm: d + EXIT_MARGIN_MM,
                note: format!(
                    "lowest cavity point z={:.2}; {:.2} mm from the hole start to the outside, towards {}",
                    low.z,
                    d,
                    match (dir[0] + dir[1], axis[0] != 0.0) {
                        (s, true) if s > 0.0 => "+X",
                        (_, true) => "-X",
                        (s, false) if s > 0.0 => "+Y",
                        _ => "-Y",
                    }
                ),
            });
        }
    }
    Ok((holes, warnings))
}

/// Resolve user-supplied holes: radius and direction defaults as the app, and a
/// length that runs out through everything on the axis when not given.
pub fn resolve_holes(mesh: &IndexedMesh, holes: &[Hole], default_radius: f32) -> Vec<PlacedHole> {
    holes
        .iter()
        .map(|h| {
            let direction = normalise(h.direction.unwrap_or([0.0, 0.0, -1.0]));
            let origin = Vec3::new(h.x, h.y, h.z);
            let length_mm = h.length.unwrap_or_else(|| {
                let far = ray_hits(
                    mesh,
                    origin,
                    Vec3::new(direction[0], direction[1], direction[2]),
                )
                .last()
                .map_or(0.0, |x| x.0);
                far + EXIT_MARGIN_MM
            });
            PlacedHole {
                x: h.x,
                y: h.y,
                z: h.z,
                radius_mm: h.radius.unwrap_or(default_radius),
                direction,
                length_mm,
                note: "as requested".into(),
            }
        })
        .collect()
}

/// Punch `holes` into `mesh`. The kernel wants centres normalised to the input
/// mesh's bounding box, as the app's `useHolePunchManager` does.
pub fn punch(mesh: IndexedMesh, holes: &[PlacedHole]) -> dragonfruit_mesh_repair::HolePunchOutcome {
    let b = mesh.bbox();
    let norm = |v: f32, lo: f32, hi: f32| {
        if hi - lo <= 1e-9 {
            0.5
        } else {
            (v - lo) / (hi - lo)
        }
    };
    let punches = holes
        .iter()
        .map(|h| HolePunchSpec {
            center_norm: [
                norm(h.x, b.min.x, b.max.x),
                norm(h.y, b.min.y, b.max.y),
                norm(h.z, b.min.z, b.max.z),
            ],
            radius_mm: h.radius_mm,
            radius_y_mm: None,
            direction: Some(h.direction),
            length_mm: Some(h.length_mm),
        })
        .collect();
    punch_cylinders(mesh, &HolePunchOptions { punches })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// An axis-aligned cube from (0,0,0) to (s,s,s), outward-facing, welded.
    pub fn cube(s: f32) -> IndexedMesh {
        let p = |x: f32, y: f32, z: f32| Vec3::new(x * s, y * s, z * s);
        let positions = vec![
            p(0., 0., 0.),
            p(1., 0., 0.),
            p(1., 1., 0.),
            p(0., 1., 0.),
            p(0., 0., 1.),
            p(1., 0., 1.),
            p(1., 1., 1.),
            p(0., 1., 1.),
        ];
        let triangles = vec![
            [0, 2, 1],
            [0, 3, 2],
            [4, 5, 6],
            [4, 6, 7],
            [0, 1, 5],
            [0, 5, 4],
            [1, 2, 6],
            [1, 6, 5],
            [2, 3, 7],
            [2, 7, 6],
            [3, 0, 4],
            [3, 4, 7],
        ];
        IndexedMesh {
            positions,
            triangles,
        }
    }

    fn hollow_cube() -> IndexedMesh {
        let options = HollowOptions {
            voxel_resolution: 40,
            ..HollowOptions::default()
        };
        hollow(cube(20.0), &options).mesh
    }

    #[test]
    fn cube_volume_is_exact() {
        let s = stats(&cube(20.0));
        assert!((s.volume_mm3 - 8000.0).abs() < 1.0);
        assert!(s.watertight);
        assert_eq!((s.shells, s.cavities), (1, 0));
    }

    #[test]
    fn hollowing_a_cube_leaves_roughly_the_walls() {
        let s = stats(&hollow_cube());
        // 20^3 - 16^3 = 3904 mm3, give or take voxel quantisation.
        assert!(
            (s.volume_mm3 - 3904.0).abs() < 3904.0 * 0.15,
            "volume {}",
            s.volume_mm3
        );
        assert_eq!(
            (s.shells, s.cavities),
            (2, 1),
            "outer skin plus one sealed cavity"
        );
        assert!(s.watertight);
    }

    #[test]
    fn stl_round_trips() {
        let path = std::env::temp_dir().join(format!("ndfm-roundtrip-{}.stl", std::process::id()));
        write_binary_stl(&cube(20.0), &path).unwrap();
        let back = dragonfruit_mesh_repair::io::stl::load(&path).unwrap();
        std::fs::remove_file(&path).ok();
        assert_eq!(back.triangle_count(), 12);
        assert!((back.signed_volume() - 8000.0).abs() < 1.0);
    }

    #[test]
    fn resolution_follows_the_apps_clamp() {
        assert_eq!(voxel_resolution(0.65, &cube(70.0)), 108);
        assert_eq!(voxel_resolution(0.65, &cube(5.0)), 24);
        assert_eq!(voxel_resolution(0.05, &cube(70.0)), 192);
    }

    #[test]
    fn auto_base_needs_a_cavity() {
        assert!(auto_base_holes(&cube(20.0), 2.0).is_err());
    }

    #[test]
    fn auto_base_holes_open_the_cavity() {
        let hollowed = hollow_cube();
        let (holes, _) = auto_base_holes(&hollowed, 1.5).unwrap();
        assert_eq!(holes.len(), 2);
        // Opposite directions along one horizontal axis, at the cavity floor.
        for k in 0..3 {
            assert!((holes[0].direction[k] + holes[1].direction[k]).abs() < 1e-6);
        }
        assert_eq!(holes[0].direction[2], 0.0);
        assert!(holes[0].z < 10.0, "near the bottom, got z={}", holes[0].z);
        let punched = punch(hollowed, &holes).mesh;
        let s = stats(&punched);
        assert_eq!(s.cavities, 0, "cavity should now be open to the outside");
        assert_eq!(s.shells, 1);
        assert!(s.volume_mm3 < 3904.0 * 1.15);
    }

    #[test]
    fn manual_holes_default_to_the_app_direction_and_pass_through() {
        let holes = [Hole {
            x: 10.0,
            y: 10.0,
            z: 20.0,
            radius: None,
            direction: None,
            length: None,
        }];
        let placed = resolve_holes(&cube(20.0), &holes, 2.0);
        assert_eq!(placed[0].direction, [0.0, 0.0, -1.0]);
        assert_eq!(placed[0].radius_mm, 2.0);
        // From the top face straight through 20 mm of cube, plus the exit margin.
        assert!(placed[0].length_mm > 20.0);
    }
}

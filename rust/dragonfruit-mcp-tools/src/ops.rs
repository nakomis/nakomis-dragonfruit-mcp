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
pub const EXIT_MARGIN_MM: f32 = 1.0;

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
///
/// Written to a sibling temp file and renamed, so a failure never leaves a truncated
/// STL where a good one may have been.
pub fn write_binary_stl(mesh: &IndexedMesh, path: &std::path::Path) -> std::io::Result<()> {
    let mut tmp_name = path.as_os_str().to_owned();
    tmp_name.push(".tmp");
    let tmp = std::path::PathBuf::from(tmp_name);
    let result = write_stl_to(mesh, &tmp).and_then(|()| std::fs::rename(&tmp, path));
    if result.is_err() {
        std::fs::remove_file(&tmp).ok();
    }
    result
}

fn write_stl_to(mesh: &IndexedMesh, path: &std::path::Path) -> std::io::Result<()> {
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
#[serde(deny_unknown_fields)]
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

/// A hole with every field resolved, plus where it came from and why it is there.
#[derive(Debug, Clone, Serialize)]
pub struct PlacedHole {
    pub x: f32,
    pub y: f32,
    pub z: f32,
    pub radius_mm: f32,
    pub direction: [f32; 3],
    /// "-z", "+x" and so on for an axis-aligned hole, "custom" otherwise.
    pub axis: String,
    pub length_mm: f32,
    /// "suction relief", "vent", "manual", ...
    pub purpose: String,
    /// Which cavity (index into the mesh's shells) an automatic hole serves.
    pub cavity: Option<usize>,
    /// How far an automatic hole's start was pushed into the cavity (mm) so its full
    /// diameter opens into the void.
    pub extension_mm: f32,
    pub note: String,
}

pub fn axis_name(d: [f32; 3]) -> String {
    for (k, c) in ["x", "y", "z"].iter().enumerate() {
        if (d[k].abs() - 1.0).abs() < 1e-4 {
            return format!("{}{c}", if d[k] > 0.0 { '+' } else { '-' });
        }
    }
    "custom".into()
}

fn normalise(d: [f32; 3]) -> [f32; 3] {
    let l = (d[0] * d[0] + d[1] * d[1] + d[2] * d[2]).sqrt();
    if l <= 1e-9 {
        [0.0, 0.0, -1.0]
    } else {
        [d[0] / l, d[1] / l, d[2] / l]
    }
}

/// One ray against one triangle (Moller-Trumbore): `(t, leaving_solid)`. Normals point
/// out of the material, so a hit whose normal faces along the ray is the ray leaving it.
pub fn ray_tri(mesh: &IndexedMesh, i: usize, origin: Vec3, dir: Vec3) -> Option<(f32, bool)> {
    let t = &mesh.triangles[i];
    let (a, b, c) = (
        mesh.positions[t[0] as usize],
        mesh.positions[t[1] as usize],
        mesh.positions[t[2] as usize],
    );
    let (e1, e2) = (b.sub(a), c.sub(a));
    let p = dir.cross(e2);
    let det = e1.dot(p);
    if det.abs() < 1e-12 {
        return None;
    }
    let inv = 1.0 / det;
    let s = origin.sub(a);
    let u = s.dot(p) * inv;
    if !(0.0..=1.0).contains(&u) {
        return None;
    }
    let q = s.cross(e1);
    let v = dir.dot(q) * inv;
    if v < 0.0 || u + v > 1.0 {
        return None;
    }
    let tt = e2.dot(q) * inv;
    (tt > 1e-4).then(|| (tt, e1.cross(e2).dot(dir) > 0.0))
}

/// Every triangle the ray crosses (t > 0), nearest first, as `(t, triangle, leaving_solid)`.
pub fn ray_hits(mesh: &IndexedMesh, origin: Vec3, dir: Vec3) -> Vec<(f32, usize, bool)> {
    let mut hits: Vec<_> = (0..mesh.triangles.len())
        .filter_map(|i| ray_tri(mesh, i, origin, dir).map(|(t, l)| (t, i, l)))
        .collect();
    hits.sort_by(|x, y| x.0.total_cmp(&y.0));
    hits
}

/// Whether `p` lies inside the outer skin (in a cavity still counts): the first
/// outer-skin surface met along +Z must be one the ray is leaving.
pub fn inside_skin(mesh: &IndexedMesh, shells: &Shells, p: Vec3) -> bool {
    ray_hits(mesh, p, Vec3::new(0.0, 0.0, 1.0))
        .iter()
        .find(|h| shells.volumes[shells.triangle_shell[h.1]] > 0.0)
        .is_some_and(|h| h.2)
}

/// Resolve user-supplied holes: radius and direction defaults as the app, and a
/// length that runs out through everything on the axis when not given. Warns about
/// holes whose start point is outside the mesh (they are probably in the wrong units
/// or coordinates).
pub fn resolve_holes(
    mesh: &IndexedMesh,
    holes: &[Hole],
    default_radius: f32,
) -> (Vec<PlacedHole>, Vec<String>) {
    let shells = Shells::of(mesh);
    let mut warnings = Vec::new();
    let placed = holes
        .iter()
        .enumerate()
        .map(|(i, h)| {
            let direction = normalise(h.direction.unwrap_or([0.0, 0.0, -1.0]));
            let origin = Vec3::new(h.x, h.y, h.z);
            // Nudged into the part along the hole, so a start on the surface (as the app
            // places them) counts as inside.
            let nudged = Vec3::new(
                origin.x + direction[0] * 0.05,
                origin.y + direction[1] * 0.05,
                origin.z + direction[2] * 0.05,
            );
            if !inside_skin(mesh, &shells, nudged) {
                warnings.push(format!(
                    "hole {i} starts outside the mesh at ({}, {}, {}); holes are in model millimetres",
                    h.x, h.y, h.z
                ));
            }
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
                axis: axis_name(direction),
                direction,
                length_mm,
                purpose: "manual".into(),
                cavity: None,
                extension_mm: 0.0,
                note: "as requested".into(),
            }
        })
        .collect();
    (placed, warnings)
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

/// What the caller must hear about a punch result.
pub fn punch_warnings(before: &MeshStats, after: &MeshStats) -> Vec<String> {
    let mut w = Vec::new();
    if after.triangles == before.triangles && (after.volume_mm3 - before.volume_mm3).abs() < 1e-6 {
        w.push("the punch changed nothing: the holes may miss the model".into());
    }
    if after.cavities > 0 {
        w.push(format!(
            "{} sealed cavit{} remain{}: resin will be trapped there and can suction-cup the print; \
             the holes did not connect {} to the outside",
            after.cavities,
            if after.cavities == 1 { "y" } else { "ies" },
            if after.cavities == 1 { "s" } else { "" },
            if after.cavities == 1 { "it" } else { "them" },
        ));
    }
    if !after.watertight {
        w.push("the punched mesh is not watertight".into());
    }
    w
}

/// Meshes for tests: boxes, optionally inside-out (a cavity), welded per box.
#[cfg(test)]
pub mod testutil {
    use super::*;

    pub fn cuboid(min: [f32; 3], max: [f32; 3], inward: bool) -> IndexedMesh {
        let p = |x: usize, y: usize, z: usize| {
            Vec3::new(
                if x == 0 { min[0] } else { max[0] },
                if y == 0 { min[1] } else { max[1] },
                if z == 0 { min[2] } else { max[2] },
            )
        };
        let positions = vec![
            p(0, 0, 0),
            p(1, 0, 0),
            p(1, 1, 0),
            p(0, 1, 0),
            p(0, 0, 1),
            p(1, 0, 1),
            p(1, 1, 1),
            p(0, 1, 1),
        ];
        let mut triangles: Vec<[u32; 3]> = vec![
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
        if inward {
            for t in &mut triangles {
                t.swap(1, 2);
            }
        }
        IndexedMesh {
            positions,
            triangles,
        }
    }

    pub fn cube(s: f32) -> IndexedMesh {
        cuboid([0.0; 3], [s, s, s], false)
    }

    /// Several meshes in one, as separate shells.
    pub fn merge(parts: Vec<IndexedMesh>) -> IndexedMesh {
        let mut out = IndexedMesh::default();
        for m in parts {
            let base = out.positions.len() as u32;
            out.positions.extend(m.positions);
            out.triangles.extend(
                m.triangles
                    .iter()
                    .map(|t| [t[0] + base, t[1] + base, t[2] + base]),
            );
        }
        out
    }

    /// A 40 mm cube with two sealed cubic cavities side by side.
    pub fn two_cavities() -> IndexedMesh {
        merge(vec![
            cube(40.0),
            cuboid([6.0, 12.0, 8.0], [16.0, 28.0, 30.0], true),
            cuboid([24.0, 12.0, 8.0], [34.0, 28.0, 30.0], true),
        ])
    }
}

#[cfg(test)]
mod tests {
    use super::testutil::*;
    use super::*;

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
        assert_eq!((s.boundary_edges, s.non_manifold_edges), (0, 0));
    }

    #[test]
    fn hollowing_leaves_the_outer_skin_untouched() {
        let hollowed = hollow_cube();
        let shells = Shells::of(&hollowed);
        let outer = shells.volumes.iter().cloned().fold(f64::MIN, f64::max);
        assert!((outer - 8000.0).abs() < 1.0, "outer shell volume {outer}");
        let b = hollowed.bbox();
        assert_eq!((b.min.x, b.max.x, b.min.z, b.max.z), (0.0, 20.0, 0.0, 20.0));
    }

    #[test]
    fn the_cavity_keeps_off_the_skin_by_about_the_wall() {
        let hollowed = hollow_cube();
        let shells = Shells::of(&hollowed);
        let cav = shells.cavity_ids()[0];
        let mut nearest = f32::MAX;
        for (ti, t) in hollowed.triangles.iter().enumerate() {
            if shells.triangle_shell[ti] != cav {
                continue;
            }
            for &vi in t {
                let p = hollowed.positions[vi as usize];
                for c in [p.x, p.y, p.z] {
                    nearest = nearest.min(c).min(20.0 - c);
                }
            }
        }
        // 2 mm wall, less up to a voxel (0.5 mm here) of quantisation.
        assert!(
            nearest > 1.4,
            "cavity comes within {nearest} mm of the skin"
        );
    }

    #[test]
    fn parts_thinner_than_twice_the_wall_stay_solid() {
        let slab = cuboid([0.0, 0.0, 0.0], [20.0, 20.0, 3.0], false);
        let options = HollowOptions {
            voxel_resolution: 40,
            ..HollowOptions::default()
        };
        let s = stats(&hollow(slab, &options).mesh);
        assert_eq!(s.cavities, 0);
        assert!(
            (s.volume_mm3 - 1200.0).abs() < 1.0,
            "volume {}",
            s.volume_mm3
        );
    }

    #[test]
    fn stl_round_trips_and_leaves_no_temp_file() {
        let path = std::env::temp_dir().join(format!("ndfm-roundtrip-{}.stl", std::process::id()));
        write_binary_stl(&cube(20.0), &path).unwrap();
        let back = dragonfruit_mesh_repair::io::stl::load(&path).unwrap();
        let tmp = path.with_extension("stl.tmp");
        let tmp_left = tmp.exists();
        std::fs::remove_file(&path).ok();
        assert_eq!(back.triangle_count(), 12);
        assert!((back.signed_volume() - 8000.0).abs() < 1.0);
        assert!(!tmp_left);
    }

    #[test]
    fn resolution_follows_the_apps_clamp() {
        assert_eq!(voxel_resolution(0.65, &cube(70.0)), 108);
        assert_eq!(voxel_resolution(0.65, &cube(5.0)), 24);
        assert_eq!(voxel_resolution(0.05, &cube(70.0)), 192);
    }

    fn hole(x: f32, y: f32, z: f32) -> Hole {
        Hole {
            x,
            y,
            z,
            radius: None,
            direction: None,
            length: None,
        }
    }

    #[test]
    fn manual_holes_default_to_the_app_direction_and_pass_through() {
        let (placed, warnings) = resolve_holes(&cube(20.0), &[hole(10.0, 10.0, 20.0)], 2.0);
        assert_eq!(placed[0].direction, [0.0, 0.0, -1.0]);
        assert_eq!(placed[0].axis, "-z");
        assert_eq!(placed[0].radius_mm, 2.0);
        // From the top face straight through 20 mm of cube, plus the exit margin.
        assert!(placed[0].length_mm > 20.0);
        assert!(warnings.is_empty());
    }

    #[test]
    fn a_hole_starting_outside_the_mesh_is_flagged() {
        let (_, warnings) = resolve_holes(&cube(20.0), &[hole(50.0, 10.0, 10.0)], 2.0);
        assert_eq!(warnings.len(), 1);
        assert!(warnings[0].contains("hole 0 starts outside"));
        // A start inside a cavity is fine.
        let (_, w) = resolve_holes(&two_cavities(), &[hole(11.0, 20.0, 20.0)], 2.0);
        assert!(w.is_empty(), "{w:?}");
    }

    #[test]
    fn unknown_hole_fields_are_rejected() {
        let r: Result<Hole, _> = serde_json::from_str(r#"{"x":1,"y":2,"z":3,"radius_mmm":2}"#);
        assert!(r.is_err());
    }

    #[test]
    fn a_partial_open_still_warns_about_the_cavity_left() {
        let mesh = two_cavities();
        let before = stats(&mesh);
        assert_eq!(before.cavities, 2);
        // Open only the left cavity, straight down through its floor.
        let (placed, _) = resolve_holes(&mesh, &[hole(11.0, 20.0, 9.0)], 2.0);
        let after = stats(&punch(mesh, &placed).mesh);
        assert_eq!(after.cavities, 1);
        let warnings = punch_warnings(&before, &after);
        assert!(
            warnings
                .iter()
                .any(|w| w.starts_with("1 sealed cavity remains")),
            "{warnings:?}"
        );
        // Fully drained: nothing to say.
        let ok = MeshStats {
            cavities: 0,
            ..after.clone()
        };
        assert!(punch_warnings(&before, &ok).is_empty());
    }
}

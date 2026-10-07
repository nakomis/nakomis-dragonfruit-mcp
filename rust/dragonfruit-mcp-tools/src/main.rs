//! Mesh operations that `dragonfruit-cli` doesn't expose: hollowing and hole
//! punching (NDFM-6), both calling DragonFruit's mesh-repair crate the way its
//! desktop app does.
//!
//! `overhangs` runs DragonFruit's mesh-normal overhang classifier (NDFM-8),
//! which upstream only exposes as a Tauri command; see build.rs.

mod drain;
mod ops;

#[allow(dead_code, clippy::all)]
mod overhang {
    include!(concat!(env!("OUT_DIR"), "/overhang.rs"));
}

use std::path::{Path, PathBuf};
use std::time::Instant;

use clap::{Parser, Subcommand};
use dragonfruit_mesh_repair::io::load_mesh_from_path;
use dragonfruit_mesh_repair::{HollowMode, HollowOptions};
use serde_json::{json, Value};

use ops::{Hole, PlacedHole};

#[derive(Parser)]
#[command(name = "dragonfruit-mcp-tools", version)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Print the version as JSON.
    Version,
    /// Hollow a model, leaving a sealed internal cavity (voxel method, as the app).
    Hollow {
        input: PathBuf,
        /// Output STL.
        #[arg(short, long)]
        output: PathBuf,
        /// Wall thickness in mm (the app's default is 2.0).
        #[arg(long)]
        wall_mm: Option<f32>,
        /// Voxel size in mm; smaller is finer and slower (the app's default is 0.65).
        #[arg(long)]
        voxel_mm: Option<f32>,
        /// JSON file of DragonFruit HollowOptions (camelCase, e.g. shellThicknessMm),
        /// applied first; --wall-mm and --voxel-mm override it when given.
        #[arg(long)]
        options_json: Option<PathBuf>,
        /// Print the report as JSON.
        #[arg(long)]
        json: bool,
    },
    /// Punch cylindrical drain holes through a model.
    Punch {
        input: PathBuf,
        /// Output STL.
        #[arg(short, long)]
        output: PathBuf,
        /// Holes as a JSON array (inline, or a path to a file) of
        /// {x, y, z, radius?, direction?, length?} in model millimetres.
        #[arg(
            long,
            conflicts_with = "auto_drain",
            required_unless_present = "auto_drain"
        )]
        holes: Option<String>,
        /// Place a suction-relief hole through the floor and a vent through the roof of
        /// every cavity (vertical, along the down axis), falling back to a horizontal
        /// pair where there is no clear vertical path.
        #[arg(long)]
        auto_drain: bool,
        /// Which way is down, towards the build plate: -z (default), +z, -x, +x, -y, +y.
        #[arg(long, default_value = "-z", allow_hyphen_values = true)]
        down_axis: String,
        /// Pin the vertical holes at this position across the down axis (mm, in x/y/z
        /// order of the two remaining axes), e.g. to keep clear of supports.
        #[arg(
            long,
            num_args = 2,
            value_names = ["A", "B"],
            allow_negative_numbers = true,
            requires = "auto_drain"
        )]
        xy: Option<Vec<f32>>,
        /// Hole radius in mm, for --auto-drain and holes without their own (the app's default is 2.0).
        #[arg(long, default_value_t = ops::DEFAULT_HOLE_RADIUS_MM)]
        radius_mm: f32,
        /// Print the report as JSON.
        #[arg(long)]
        json: bool,
    },
    /// Classify overhang regions on a world-space mesh, as the app's
    /// `scan_overhangs` does, and print the scan as JSON.
    Overhangs {
        /// A binary STL, or a positions.bin of f32 triangle vertices.
        input: std::path::PathBuf,
        /// Surface angle from horizontal at and below which a face needs support.
        #[arg(long, default_value_t = 45.0)]
        angle: f32,
        /// Footprint mask resolution (the app uses 0.25 mm).
        #[arg(long, default_value_t = 0.25)]
        px_mm: f32,
        /// Whether the print has a raft (only changes the stability report).
        #[arg(long)]
        has_raft: bool,
    },
}

fn ms(t: Instant) -> f64 {
    (t.elapsed().as_secs_f64() * 1000.0 * 10.0).round() / 10.0
}

fn load(path: &Path) -> Result<dragonfruit_mesh_repair::IndexedMesh, String> {
    if !path.exists() {
        return Err(format!("not found: {}", path.display()));
    }
    load_mesh_from_path(path).map_err(|e| format!("cannot read {}: {e}", path.display()))
}

fn write(mesh: &dragonfruit_mesh_repair::IndexedMesh, path: &Path) -> Result<(), String> {
    ops::write_binary_stl(mesh, path).map_err(|e| format!("cannot write {}: {e}", path.display()))
}

fn emit(report: &Value, as_json: bool, summary: impl FnOnce() -> String) {
    if as_json {
        println!("{report}");
    } else {
        println!("{}", summary());
    }
}

fn hollow_cmd(
    input: &Path,
    output: &Path,
    wall_mm: Option<f32>,
    voxel_mm: Option<f32>,
    options_json: Option<&Path>,
    as_json: bool,
) -> Result<(), String> {
    let total = Instant::now();
    let mut options = match options_json {
        Some(p) => {
            let text = std::fs::read_to_string(p).map_err(|e| format!("{}: {e}", p.display()))?;
            serde_json::from_str::<HollowOptions>(&text)
                .map_err(|e| format!("invalid hollow options JSON: {e}"))?
        }
        None => HollowOptions::default(),
    };
    if let Some(w) = wall_mm {
        options.shell_thickness_mm = w;
    }
    if options.shell_thickness_mm <= 0.0 {
        return Err("wall thickness must be positive".into());
    }

    let t = Instant::now();
    let mesh = load(input)?;
    let load_ms = ms(t);
    if mesh.triangle_count() == 0 {
        return Err("the mesh has no triangles".into());
    }
    // Same derivation as the app: voxel size -> cells along the longest axis.
    // An options file's own resolution stands unless --voxel-mm is given.
    if voxel_mm.is_some() || options_json.is_none() {
        let v = voxel_mm.unwrap_or(ops::DEFAULT_VOXEL_MM);
        options.voxel_resolution = ops::voxel_resolution(v, &mesh);
    }

    let t = Instant::now();
    let before = ops::stats(&mesh);
    let mut analyse_ms = ms(t);
    let t = Instant::now();
    let outcome = ops::hollow(mesh, &options);
    let hollow_ms = ms(t);
    let t = Instant::now();
    let after = ops::stats(&outcome.mesh);
    analyse_ms += ms(t);
    let t = Instant::now();
    write(&outcome.mesh, output)?;
    let write_ms = ms(t);

    let mut warnings: Vec<String> = Vec::new();
    // Only the cavity modes leave a sealed void; infill and open-face shells are another story.
    if options.mode == HollowMode::Cavity {
        if outcome.report.removed_voxels == 0 || after.cavities == 0 {
            warnings.push(
                "no cavity was created: the model is thinner than twice the wall everywhere, or the wall is too thick".into(),
            );
        } else {
            warnings.push(
                "the cavity is sealed: uncured resin is trapped and the print can suction-cup; run drill_holes (punch --auto-drain) to add drain holes".into(),
            );
        }
    }
    if !before.watertight {
        warnings.push(format!(
            "the input is not clean ({} non-manifold and {} open edges); hollowing may have altered it",
            before.non_manifold_edges, before.boundary_edges
        ));
    }
    if !after.watertight {
        warnings.push("the hollowed mesh is not watertight".into());
    }
    if after.volume_mm3 >= before.volume_mm3 {
        warnings.push("the hollowed volume is not smaller than the original".into());
    }

    let saved = before.volume_ml - after.volume_ml;
    let report = json!({
        "input": input,
        "output": output,
        "wall_mm": options.shell_thickness_mm,
        "voxel_mm": outcome.report.voxel_size_mm,
        "voxel_resolution": options.voxel_resolution,
        "mode": options.mode,
        "before": before,
        "after": after,
        "volume_saved_ml": saved,
        "volume_saved_pct": if before.volume_ml > 0.0 { saved / before.volume_ml * 100.0 } else { 0.0 },
        "crate_report": outcome.report,
        "timing_ms": {"load": load_ms, "hollow": hollow_ms, "analyse": analyse_ms, "write": write_ms, "total": ms(total)},
        "warnings": warnings,
    });
    emit(&report, as_json, || {
        format!(
            "hollowed {} -> {}: {:.2} ml -> {:.2} ml (saved {:.2} ml) in {:.0} ms\n{}",
            input.display(),
            output.display(),
            before.volume_ml,
            after.volume_ml,
            saved,
            ms(total),
            warnings.join("\n")
        )
    });
    Ok(())
}

fn parse_holes(arg: &str) -> Result<Vec<Hole>, String> {
    let text = if arg.trim_start().starts_with(['[', '{']) {
        arg.to_string()
    } else {
        std::fs::read_to_string(arg).map_err(|e| format!("--holes {arg}: {e}"))?
    };
    let value: Value =
        serde_json::from_str(&text).map_err(|e| format!("invalid holes JSON: {e}"))?;
    let value = if value.is_object() {
        json!([value])
    } else {
        value
    };
    serde_json::from_value(value).map_err(|e| format!("invalid holes JSON: {e}"))
}

fn punch_cmd(
    input: &Path,
    output: &Path,
    holes_arg: Option<&str>,
    auto_drain: bool,
    down_axis: &str,
    xy: Option<&[f32]>,
    radius_mm: f32,
    as_json: bool,
) -> Result<(), String> {
    let total = Instant::now();
    if radius_mm <= 0.0 {
        return Err("hole radius must be positive".into());
    }
    let t = Instant::now();
    let mesh = load(input)?;
    let load_ms = ms(t);
    let t = Instant::now();
    let before = ops::stats(&mesh);
    let mut analyse_ms = ms(t);

    let mut warnings: Vec<String> = Vec::new();
    let mut cavities_found = before.cavities;
    let placed: Vec<PlacedHole> = if auto_drain {
        let axis = drain::Axis::parse(down_axis)?;
        let xy = xy.map(|v| [v[0], v[1]]);
        let found = drain::auto_drain_holes(&mesh, radius_mm, axis, xy)?;
        cavities_found = found.cavities;
        warnings.extend(found.warnings);
        found.holes
    } else {
        let holes = parse_holes(holes_arg.unwrap_or("[]"))?;
        let (placed, w) = ops::resolve_holes(&mesh, &holes, radius_mm);
        warnings.extend(w);
        placed
    };
    if placed.is_empty() {
        return Err("no holes to punch".into());
    }

    let t = Instant::now();
    let outcome = ops::punch(mesh, &placed);
    let punch_ms = ms(t);
    let t = Instant::now();
    let after = ops::stats(&outcome.mesh);
    analyse_ms += ms(t);
    let t = Instant::now();
    write(&outcome.mesh, output)?;
    let write_ms = ms(t);

    warnings.extend(ops::punch_warnings(&before, &after));

    let saved = before.volume_ml - after.volume_ml;
    let report = json!({
        "input": input,
        "output": output,
        "auto_drain": auto_drain,
        "cavities_found": cavities_found,
        "holes": placed,
        "before": before,
        "after": after,
        "volume_removed_ml": saved,
        "crate_report": outcome.report,
        "timing_ms": {"load": load_ms, "punch": punch_ms, "analyse": analyse_ms, "write": write_ms, "total": ms(total)},
        "warnings": warnings,
    });
    emit(&report, as_json, || {
        format!(
            "punched {} hole(s): cavities {} -> {}, {:.2} ml -> {:.2} ml in {:.0} ms\n{}",
            placed.len(),
            before.cavities,
            after.cavities,
            before.volume_ml,
            after.volume_ml,
            ms(total),
            warnings.join("\n")
        )
    });
    Ok(())
}

fn main() {
    let cli = Cli::parse();
    let result = match cli.command {
        Command::Version => {
            let out = json!({
                "name": env!("CARGO_PKG_NAME"),
                "version": env!("CARGO_PKG_VERSION"),
                "mesh_repair_linked": linked_mesh_repair(),
            });
            println!("{out}");
            Ok(())
        }
        Command::Hollow {
            input,
            output,
            wall_mm,
            voxel_mm,
            options_json,
            json,
        } => hollow_cmd(
            &input,
            &output,
            wall_mm,
            voxel_mm,
            options_json.as_deref(),
            json,
        ),
        Command::Punch {
            input,
            output,
            holes,
            auto_drain,
            down_axis,
            xy,
            radius_mm,
            json,
        } => punch_cmd(
            &input,
            &output,
            holes.as_deref(),
            auto_drain,
            &down_axis,
            xy.as_deref(),
            radius_mm,
            json,
        ),
        Command::Overhangs {
            input,
            angle,
            px_mm,
            has_raft,
        } => overhangs_cmd(&input, angle, px_mm, has_raft),
    };
    if let Err(e) = result {
        eprintln!("error: {e}");
        std::process::exit(1);
    }
}

/// Classify overhang regions as the app's `scan_overhangs` does; print the scan as JSON.
fn overhangs_cmd(input: &Path, angle: f32, px_mm: f32, has_raft: bool) -> Result<(), String> {
    let positions = read_positions(input)?;
    let (regions, stability) =
        overhang::overhang_and_stability_from_soup(&positions, angle, px_mm, has_raft);
    let scan = overhang::OverhangScan { regions, stability };
    let text = serde_json::to_string(&scan).map_err(|e| format!("serialise overhang scan: {e}"))?;
    println!("{text}");
    Ok(())
}

/// Flat `[x, y, z, ...]` triangle vertices from a binary STL or a positions.bin.
fn read_positions(path: &std::path::Path) -> Result<Vec<f32>, String> {
    let data = std::fs::read(path).map_err(|e| format!("read {}: {e}", path.display()))?;
    let floats = |bytes: &[u8]| -> Vec<f32> {
        bytes
            .chunks_exact(4)
            .map(|b| f32::from_le_bytes([b[0], b[1], b[2], b[3]]))
            .collect()
    };
    if path.extension().is_some_and(|e| e == "bin") {
        if data.len() % 36 != 0 {
            return Err(format!("{}: not whole triangles", path.display()));
        }
        return Ok(floats(&data));
    }
    if data.len() < 84 {
        return Err(format!("{}: too small for a binary STL", path.display()));
    }
    let count = u32::from_le_bytes([data[80], data[81], data[82], data[83]]) as usize;
    if data.len() < 84 + count * 50 {
        return Err(format!(
            "{}: not a binary STL (ASCII STL is not supported)",
            path.display()
        ));
    }
    let mut out = Vec::with_capacity(count * 9);
    for t in 0..count {
        let start = 84 + t * 50 + 12;
        out.extend(floats(&data[start..start + 36]));
    }
    Ok(out)
}

/// Touch a mesh-repair type so the crate is genuinely linked, not just declared.
fn linked_mesh_repair() -> bool {
    std::mem::size_of::<dragonfruit_mesh_repair::Vec3>() > 0
}

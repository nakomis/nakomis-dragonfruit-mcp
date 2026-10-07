//! Mesh operations that `dragonfruit-cli` doesn't expose: hollowing and hole
//! punching (NDFM-6), both calling DragonFruit's mesh-repair crate the way its
//! desktop app does.

mod ops;

use std::path::{Path, PathBuf};
use std::time::Instant;

use clap::{Parser, Subcommand};
use dragonfruit_mesh_repair::io::load_mesh_from_path;
use dragonfruit_mesh_repair::HollowOptions;
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
        /// JSON file of DragonFruit HollowOptions (camelCase), applied first;
        /// --wall-mm and --voxel-mm override it.
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
            conflicts_with = "auto_base",
            required_unless_present = "auto_base"
        )]
        holes: Option<String>,
        /// Place two drain holes at the lowest point of the cavity.
        #[arg(long)]
        auto_base: bool,
        /// Hole radius in mm, for --auto-base and holes without their own (the app's default is 2.0).
        #[arg(long, default_value_t = ops::DEFAULT_HOLE_RADIUS_MM)]
        radius_mm: f32,
        /// Print the report as JSON.
        #[arg(long)]
        json: bool,
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
    if outcome.report.removed_voxels == 0 || after.cavities == 0 {
        warnings.push(
            "no cavity was created: the model is thinner than twice the wall everywhere, or the wall is too thick".into(),
        );
    } else {
        warnings.push(
            "the cavity is sealed: uncured resin is trapped and the print can suction-cup; run punch to add drain holes".into(),
        );
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
    auto_base: bool,
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
    let placed: Vec<PlacedHole> = if auto_base {
        let (holes, w) = ops::auto_base_holes(&mesh, radius_mm)?;
        warnings.extend(w);
        holes
    } else {
        let holes = parse_holes(holes_arg.unwrap_or("[]"))?;
        ops::resolve_holes(&mesh, &holes, radius_mm)
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

    // The manifold boolean can add more triangles than it removes, so judge by the mesh.
    if after.triangles == before.triangles && (after.volume_mm3 - before.volume_mm3).abs() < 1e-6 {
        warnings.push("the punch changed nothing: the holes may miss the model".into());
    }
    if before.cavities > 0 && after.cavities >= before.cavities {
        warnings
            .push("a sealed cavity remains: the holes did not connect it to the outside".into());
    }
    if !after.watertight {
        warnings.push("the punched mesh is not watertight".into());
    }

    let saved = before.volume_ml - after.volume_ml;
    let report = json!({
        "input": input,
        "output": output,
        "auto_base": auto_base,
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
            auto_base,
            radius_mm,
            json,
        } => punch_cmd(
            &input,
            &output,
            holes.as_deref(),
            auto_base,
            radius_mm,
            json,
        ),
    };
    if let Err(e) = result {
        eprintln!("error: {e}");
        std::process::exit(1);
    }
}

/// Touch a mesh-repair type so the crate is genuinely linked, not just declared.
fn linked_mesh_repair() -> bool {
    std::mem::size_of::<dragonfruit_mesh_repair::Vec3>() > 0
}

//! `goo-trim trim <in.goo> <out.goo> [--verify]` and `goo-trim islands <file.goo>`,
//! both taking `--min-overlap` and `--min-overlap-px`:
//! one JSON object on stdout; errors on stderr with a non-zero exit.

use std::path::PathBuf;
use std::process::ExitCode;

use clap::{Parser, Subcommand};
use serde_json::json;

use goo_trim::trim::{self, Thresholds};

#[derive(Parser)]
#[command(name = "goo-trim", version, about = "Remove unsupported regions from a .goo, fast")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Write <output> with every region that has nothing cured beneath it removed.
    Trim {
        input: PathBuf,
        /// Never overwritten: refused if it exists.
        output: PathBuf,
        /// Re-read the output and count the islands the independent check still finds.
        #[arg(long)]
        verify: bool,
        #[command(flatten)]
        overlap: Overlap,
    },
    /// Every cured core not resting on the cured core of the layer below.
    Islands {
        file: PathBuf,
        #[command(flatten)]
        overlap: Overlap,
    },
}

#[derive(clap::Args)]
struct Overlap {
    /// Share of a region's core that must sit on held core below.
    #[arg(long, default_value_t = trim::MIN_OVERLAP)]
    min_overlap: f64,
    /// Pixels of it that must, at least (all of a region smaller than this).
    #[arg(long, default_value_t = trim::MIN_OVERLAP_PX)]
    min_overlap_px: u64,
}

impl Overlap {
    fn thresholds(&self) -> Thresholds {
        Thresholds { min_overlap: self.min_overlap, min_overlap_px: self.min_overlap_px }
    }
}

fn run(cli: Cli) -> Result<serde_json::Value, String> {
    match cli.command {
        Command::Trim { input, output, verify, overlap } => {
            let t = overlap.thresholds();
            let r = trim::trim_file(&input, &output, t)?;
            let by_layer: Vec<_> = r.by_layer.iter().map(|&(n, regions, px)| json!([n, regions, px])).collect();
            let mut out = json!({
                "layers": r.layers,
                "layers_changed": r.layers_changed,
                "regions_dropped": r.regions_dropped,
                "pixels_dropped": r.pixels_dropped,
                "by_layer": by_layer,
            });
            if verify {
                let left = trim::find_islands_in(&output, t)?;
                out["islands_left"] = json!(left.len());
                out["largest_island_left_px"] = json!(left.iter().map(|&(_, px)| px).max().unwrap_or(0));
            }
            Ok(out)
        }
        Command::Islands { file, overlap } => {
            let found = trim::find_islands_in(&file, overlap.thresholds())?;
            Ok(json!({ "islands": found.iter().map(|&(n, px)| json!([n, px])).collect::<Vec<_>>() }))
        }
    }
}

fn main() -> ExitCode {
    match run(Cli::parse()) {
        Ok(v) => {
            println!("{v}");
            ExitCode::SUCCESS
        }
        Err(e) => {
            eprintln!("goo-trim: {e}");
            ExitCode::FAILURE
        }
    }
}

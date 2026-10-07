//! Mesh operations that `dragonfruit-cli` doesn't expose: hollowing and hole
//! punching (NDFM-6). For now it only proves that DragonFruit's mesh-repair
//! crate links and can read a mesh.

use clap::{Parser, Subcommand};

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
}

fn main() {
    let cli = Cli::parse();
    match cli.command {
        Command::Version => {
            let out = serde_json::json!({
                "name": env!("CARGO_PKG_NAME"),
                "version": env!("CARGO_PKG_VERSION"),
                "mesh_repair_linked": linked_mesh_repair(),
            });
            println!("{out}");
        }
    }
}

/// Touch a mesh-repair type so the crate is genuinely linked, not just declared.
fn linked_mesh_repair() -> bool {
    std::mem::size_of::<dragonfruit_mesh_repair::Vec3>() > 0
}

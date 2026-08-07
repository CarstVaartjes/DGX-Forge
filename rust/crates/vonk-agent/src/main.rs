#![forbid(unsafe_code)]

use std::{
    io::{self, Read},
    path::PathBuf,
};

use clap::{Parser, Subcommand};
use url::Url;
use vonk_agent::{
    config::{AgentConfig, DEFAULT_CONFIG_PATH},
    pair::{EnrollmentOutcome, collect_evidence, pair},
};

#[derive(Parser)]
#[command(
    name = "vonk-agent",
    version,
    about = "Vonk Forge outbound Spark agent"
)]
struct Cli {
    #[arg(long, default_value = DEFAULT_CONFIG_PATH)]
    config: PathBuf,
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    Pair {
        #[arg(long)]
        controller: Url,
        #[arg(long)]
        ca_sha256: String,
        #[arg(long, default_value_t = false)]
        token_stdin: bool,
    },
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let cli = Cli::parse();
    let config = AgentConfig::load(&cli.config)?;
    match cli.command {
        Command::Pair {
            controller,
            ca_sha256,
            token_stdin,
        } => {
            if !token_stdin {
                return Err("pairing token must be supplied through --token-stdin".into());
            }
            let mut token = String::new();
            io::stdin().take(4096).read_to_string(&mut token)?;
            let executable = std::env::current_exe()?;
            let evidence = collect_evidence(&executable)?;
            match pair(&config, &controller, token.trim(), &ca_sha256, evidence).await? {
                EnrollmentOutcome::Pending(pending) => {
                    println!("pairing {} is {}", pending.id, pending.state);
                }
                EnrollmentOutcome::Issued => println!("paired {}", config.node_id),
            }
        }
    }
    Ok(())
}

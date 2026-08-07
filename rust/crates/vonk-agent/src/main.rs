#![forbid(unsafe_code)]

use std::{
    io::{self, Read},
    path::PathBuf,
    time::{SystemTime, UNIX_EPOCH},
};

use clap::{Parser, Subcommand};
use url::Url;
use vonk_agent::{
    client::AgentHttpClient,
    config::{AgentConfig, DEFAULT_CONFIG_PATH},
    executor::{RejectingExecutor, run_once},
    pair::{EnrollmentOutcome, collect_evidence, pair},
    state::{StateStore, backoff_delay},
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
    Run,
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
        Command::Run => run_agent(&config).await?,
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

async fn run_agent(config: &AgentConfig) -> Result<(), Box<dyn std::error::Error>> {
    let client = AgentHttpClient::from_config(config)?;
    let mut state = StateStore::open(&config.data_dir.join("state.sqlite"), &config.node_id)?;
    state.recover_interrupted()?;
    let executor = RejectingExecutor;
    let mut failures = 0_u32;
    loop {
        let operation = run_once(
            &client,
            &mut state,
            &executor,
            &[],
            config.poll_max_seconds.min(60),
        );
        tokio::select! {
            result = operation => match result {
                Ok(()) => failures = 0,
                Err(error) if matches!(&error, vonk_agent::executor::LoopError::Client(inner) if inner.retryable()) => {
                    failures = failures.saturating_add(1);
                    let entropy = SystemTime::now().duration_since(UNIX_EPOCH)?.subsec_nanos() as u64;
                    tokio::time::sleep(backoff_delay(
                        failures,
                        entropy,
                        config.poll_min_seconds,
                        config.poll_max_seconds,
                    )).await;
                }
                Err(error) => return Err(error.into()),
            },
            signal = tokio::signal::ctrl_c() => {
                signal?;
                return Ok(());
            }
        }
    }
}

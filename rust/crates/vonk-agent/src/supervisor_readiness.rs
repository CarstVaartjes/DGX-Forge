use std::collections::BTreeMap;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use serde::Serialize;
use thiserror::Error;
use vonk_agent_protocol::canonical_json;

const CHALLENGE_NAME: &str = "activation-challenge";
const MAX_CHALLENGE_BYTES: u64 = 65;
static PUBLICATION_SEQUENCE: AtomicU64 = AtomicU64::new(1);

#[derive(Debug, Error)]
pub enum SupervisorReadinessError {
    #[error("supervisor readiness environment is invalid")]
    InvalidEnvironment,
    #[error("supervisor activation challenge is invalid")]
    InvalidChallenge,
    #[error("supervisor readiness path is unsafe")]
    UnsafePath,
    #[error("supervisor readiness publication failed")]
    Io(#[from] std::io::Error),
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ReadinessDocument {
    schema_version: u8,
    generation: u64,
    slot: String,
    artifact_sha256: String,
    state_schema: u32,
    challenge: String,
    pid: u32,
}

pub struct SupervisorReadiness {
    document: Option<ReadinessDocument>,
    runtime_root: PathBuf,
}

impl SupervisorReadiness {
    pub fn from_process_environment() -> Result<Self, SupervisorReadinessError> {
        Self::from_environment(
            &std::env::vars().collect(),
            Path::new("/run/vonk-forge-agent"),
        )
    }

    pub fn from_environment(
        environment: &BTreeMap<String, String>,
        runtime_root: &Path,
    ) -> Result<Self, SupervisorReadinessError> {
        let names = [
            "VONK_SUPERVISOR_GENERATION",
            "VONK_SUPERVISOR_SLOT",
            "VONK_SUPERVISOR_SHA256",
            "VONK_SUPERVISOR_STATE_SCHEMA",
        ];
        let present = names
            .iter()
            .filter(|name| environment.contains_key(**name))
            .count();
        if present == 0 {
            return Ok(Self {
                document: None,
                runtime_root: runtime_root.to_path_buf(),
            });
        }
        if present != names.len() {
            return Err(SupervisorReadinessError::InvalidEnvironment);
        }
        let generation = parse_positive(environment, names[0])?;
        let slot = environment[names[1]].clone();
        let artifact_sha256 = environment[names[2]].clone();
        let state_schema: u32 = parse_positive(environment, names[3])?
            .try_into()
            .map_err(|_| SupervisorReadinessError::InvalidEnvironment)?;
        if !matches!(slot.as_str(), "a" | "b") || !lower_hex(&artifact_sha256, 64) {
            return Err(SupervisorReadinessError::InvalidEnvironment);
        }
        let credentials = environment
            .get("CREDENTIALS_DIRECTORY")
            .ok_or(SupervisorReadinessError::InvalidChallenge)?;
        let challenge = read_challenge(Path::new(credentials))?;
        Ok(Self {
            document: Some(ReadinessDocument {
                schema_version: 1,
                generation,
                slot,
                artifact_sha256,
                state_schema,
                challenge,
                pid: std::process::id(),
            }),
            runtime_root: runtime_root.to_path_buf(),
        })
    }

    pub fn report(&mut self) -> Result<bool, SupervisorReadinessError> {
        let Some(document) = self.document.take() else {
            return Ok(false);
        };
        match self.publish_document(&document) {
            Ok(()) => Ok(true),
            Err(error) => {
                self.document = Some(document);
                Err(error)
            }
        }
    }

    fn publish_document(
        &self,
        document: &ReadinessDocument,
    ) -> Result<(), SupervisorReadinessError> {
        let metadata = fs::symlink_metadata(&self.runtime_root)?;
        let effective_uid = rustix::process::geteuid().as_raw();
        if metadata.file_type().is_symlink()
            || !metadata.is_dir()
            || metadata.uid() != effective_uid
            || metadata.permissions().mode() & 0o777 != 0o700
        {
            return Err(SupervisorReadinessError::UnsafePath);
        }
        let raw =
            canonical_json(document).map_err(|_| SupervisorReadinessError::InvalidEnvironment)?;
        let sequence = PUBLICATION_SEQUENCE.fetch_add(1, Ordering::Relaxed);
        let temporary = self
            .runtime_root
            .join(format!(".readiness.{}.{sequence}.new", std::process::id()));
        let destination = self.runtime_root.join("readiness.json");
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&temporary)?;
        file.write_all(&raw)?;
        file.sync_all()?;
        let staged = file.metadata()?;
        if !staged.is_file()
            || staged.nlink() != 1
            || staged.uid() != effective_uid
            || staged.permissions().mode() & 0o777 != 0o600
        {
            let _ = fs::remove_file(&temporary);
            return Err(SupervisorReadinessError::UnsafePath);
        }
        if let Err(error) = fs::rename(&temporary, destination) {
            let _ = fs::remove_file(&temporary);
            return Err(SupervisorReadinessError::Io(error));
        }
        OpenOptions::new()
            .read(true)
            .open(&self.runtime_root)?
            .sync_all()?;
        Ok(())
    }
}

fn read_challenge(directory: &Path) -> Result<String, SupervisorReadinessError> {
    if !directory.is_absolute()
        || directory
            .components()
            .any(|component| !matches!(component, Component::RootDir | Component::Normal(_)))
    {
        return Err(SupervisorReadinessError::InvalidChallenge);
    }
    let directory_metadata = fs::symlink_metadata(directory)?;
    if directory_metadata.file_type().is_symlink() || !directory_metadata.is_dir() {
        return Err(SupervisorReadinessError::InvalidChallenge);
    }
    let path = directory.join(CHALLENGE_NAME);
    let metadata = fs::symlink_metadata(&path)?;
    if metadata.file_type().is_symlink()
        || !metadata.is_file()
        || metadata.nlink() != 1
        || metadata.len() == 0
        || metadata.len() > MAX_CHALLENGE_BYTES
        || metadata.permissions().mode() & 0o022 != 0
    {
        return Err(SupervisorReadinessError::InvalidChallenge);
    }
    let value = fs::read_to_string(path)?.trim_end().to_owned();
    if !lower_hex(&value, 64) {
        return Err(SupervisorReadinessError::InvalidChallenge);
    }
    Ok(value)
}

fn parse_positive(
    environment: &BTreeMap<String, String>,
    name: &str,
) -> Result<u64, SupervisorReadinessError> {
    environment
        .get(name)
        .and_then(|value| value.parse::<u64>().ok())
        .filter(|value| *value > 0)
        .ok_or(SupervisorReadinessError::InvalidEnvironment)
}

fn lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

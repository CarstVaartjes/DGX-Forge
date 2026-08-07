use std::fs;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::Path;

use serde::{Deserialize, Serialize};
use thiserror::Error;
use vonk_agent_protocol::canonical_json;

use crate::slots::Slot;

const MAX_READINESS_BYTES: u64 = 4096;

#[derive(Debug, Error)]
pub enum ReadinessError {
    #[error("readiness evidence is invalid")]
    Invalid,
    #[error("readiness evidence is unsafe")]
    Unsafe,
    #[error("readiness evidence could not be read")]
    Io(#[from] std::io::Error),
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ReadinessEvidence {
    pub schema_version: u8,
    pub generation: u64,
    pub slot: Slot,
    pub artifact_sha256: String,
    pub state_schema: u32,
    pub challenge: String,
    pub pid: u32,
}

impl ReadinessEvidence {
    pub fn validate(&self) -> Result<(), ReadinessError> {
        if self.schema_version != 1
            || self.generation == 0
            || self.state_schema == 0
            || self.pid == 0
            || !lower_hex(&self.artifact_sha256, 64)
            || !lower_hex(&self.challenge, 64)
        {
            return Err(ReadinessError::Invalid);
        }
        Ok(())
    }

    pub fn load(path: &Path, required_uid: Option<u32>) -> Result<Self, ReadinessError> {
        let metadata = fs::symlink_metadata(path)?;
        if metadata.file_type().is_symlink()
            || !metadata.is_file()
            || metadata.nlink() != 1
            || metadata.len() == 0
            || metadata.len() > MAX_READINESS_BYTES
            || metadata.permissions().mode() & 0o777 != 0o600
            || required_uid.is_some_and(|uid| metadata.uid() != uid)
        {
            return Err(ReadinessError::Unsafe);
        }
        let raw = fs::read(path)?;
        let value: Self = serde_json::from_slice(&raw).map_err(|_| ReadinessError::Invalid)?;
        value.validate()?;
        if canonical_json(&value).map_err(|_| ReadinessError::Invalid)? != raw {
            return Err(ReadinessError::Invalid);
        }
        Ok(value)
    }
}

fn lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

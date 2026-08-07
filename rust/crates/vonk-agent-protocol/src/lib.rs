#![forbid(unsafe_code)]

use std::collections::BTreeMap;

use chrono::{DateTime, FixedOffset};
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use serde_json::Value;
use sha2::{Digest, Sha256};
use thiserror::Error;
use uuid::Uuid;

#[derive(Debug, Error)]
pub enum ProtocolError {
    #[error("protocol JSON is invalid")]
    Json(#[from] serde_json::Error),
    #[error("protocol identity is invalid: {0}")]
    Identity(&'static str),
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct AgentClaim {
    pub attempt: u32,
    pub base_commit: String,
    pub deadline: DateTime<FixedOffset>,
    pub fence: Uuid,
    pub job_id: Uuid,
    pub node_id: String,
    pub operation: String,
    pub operation_id: Uuid,
    pub payload: Value,
    pub payload_digest: String,
    pub schema_version: u8,
}

impl AgentClaim {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        if self.schema_version != 1 || self.attempt == 0 {
            return Err(ProtocolError::Identity("claim version or attempt"));
        }
        if !valid_node_id(&self.node_id) || !lower_hex(&self.base_commit, 40) {
            return Err(ProtocolError::Identity("claim node or authority"));
        }
        if !matches!(
            self.operation.as_str(),
            "recipe.install" | "recipe.start" | "recipe.stop" | "recipe.uninstall"
        ) {
            return Err(ProtocolError::Identity("claim operation"));
        }
        let payload = canonical_json(&self.payload)?;
        if hex_sha256(&payload) != self.payload_digest {
            return Err(ProtocolError::Identity("claim payload digest"));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct AgentResult {
    pub attempt: u32,
    pub deadline: DateTime<FixedOffset>,
    pub fence: Uuid,
    pub job_id: Uuid,
    pub node_id: String,
    pub operation_id: Uuid,
    pub result: Value,
    pub schema_version: u8,
    pub state: String,
}

impl AgentResult {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        if self.schema_version != 1
            || self.attempt == 0
            || !valid_node_id(&self.node_id)
            || !matches!(
                self.state.as_str(),
                "succeeded" | "failed" | "waiting-for-operator"
            )
        {
            return Err(ProtocolError::Identity("result identity"));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct EnrollmentRequest {
    pub csr: String,
    pub evidence: EnrollmentEvidence,
    pub grant_token: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct EnrollmentEvidence {
    pub agent_digest: String,
    pub boot_id: String,
    pub csr_public_key_fingerprint: String,
    pub hardware_fingerprint: String,
    pub host_key_fingerprint: String,
    pub node_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct PackageOperationRequest {
    pub deployment_digest: String,
    pub deployment_id: String,
    pub release_digest: String,
    pub schema_version: u8,
}

pub fn parse_strict<T: DeserializeOwned>(input: &[u8]) -> Result<T, ProtocolError> {
    Ok(serde_json::from_slice(input)?)
}

pub fn canonical_json<T: Serialize>(value: &T) -> Result<Vec<u8>, ProtocolError> {
    let value = serde_json::to_value(value)?;
    Ok(serde_json::to_vec(&sort_value(value))?)
}

pub fn hex_sha256(value: &[u8]) -> String {
    format!("{:x}", Sha256::digest(value))
}

fn sort_value(value: Value) -> Value {
    match value {
        Value::Object(values) => Value::Object(
            values
                .into_iter()
                .map(|(key, value)| (key, sort_value(value)))
                .collect::<BTreeMap<_, _>>()
                .into_iter()
                .collect(),
        ),
        Value::Array(values) => Value::Array(values.into_iter().map(sort_value).collect()),
        other => other,
    }
}

fn valid_node_id(value: &str) -> bool {
    value.len() == 36
        && value.starts_with("spk_")
        && value[4..]
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

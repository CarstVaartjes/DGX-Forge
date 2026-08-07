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

#[derive(Debug, Clone, PartialEq)]
pub enum RecipeOperationRequest {
    Install(RecipeInstallRequest),
    Start(RecipeStartRequest),
    Stop(RecipeStopRequest),
    Uninstall(RecipeUninstallRequest),
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct RecipeInstallRequest {
    pub expected_bytes: u64,
    pub installation_id: Uuid,
    pub plan_digest: String,
    pub recipe_content_sha256: String,
    pub recipe_revision_id: Uuid,
    pub schema_version: u8,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct RecipeStartRequest {
    pub alias: String,
    pub endpoint_address: std::net::IpAddr,
    pub installation_id: Uuid,
    pub local_address: Option<std::net::IpAddr>,
    pub master_address: Option<std::net::IpAddr>,
    pub master_port: Option<u16>,
    pub plan_digest: String,
    pub port: u16,
    pub rank: u16,
    pub recipe_content_sha256: String,
    pub recipe_revision_id: Uuid,
    pub reserved_memory_bytes: u64,
    pub role: String,
    pub run_id: Uuid,
    pub schema_version: u8,
    pub world_size: u16,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct RecipeStopRequest {
    pub plan_digest: String,
    pub run_id: Uuid,
    pub schema_version: u8,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct RecipeUninstallRequest {
    pub installation_id: Uuid,
    pub plan_digest: String,
    pub recipe_content_sha256: String,
    pub schema_version: u8,
}

impl RecipeOperationRequest {
    pub fn parse(claim: &AgentClaim) -> Result<Self, ProtocolError> {
        claim.validate()?;
        let request = match claim.operation.as_str() {
            "recipe.install" => Self::Install(serde_json::from_value(claim.payload.clone())?),
            "recipe.start" => Self::Start(serde_json::from_value(claim.payload.clone())?),
            "recipe.stop" => Self::Stop(serde_json::from_value(claim.payload.clone())?),
            "recipe.uninstall" => Self::Uninstall(serde_json::from_value(claim.payload.clone())?),
            _ => return Err(ProtocolError::Identity("recipe operation")),
        };
        request.validate()?;
        Ok(request)
    }

    fn validate(&self) -> Result<(), ProtocolError> {
        let valid_common = |version: u8, plan: &str| version == 1 && lower_hex(plan, 64);
        let valid = match self {
            Self::Install(value) => {
                valid_common(value.schema_version, &value.plan_digest)
                    && value.expected_bytes <= 16 * 1024_u64.pow(4)
                    && lower_hex(&value.recipe_content_sha256, 64)
            }
            Self::Start(value) => {
                valid_common(value.schema_version, &value.plan_digest)
                    && lower_hex(&value.recipe_content_sha256, 64)
                    && value.rank <= 1023
                    && (1..=16).contains(&value.world_size)
                    && value.rank < value.world_size
                    && matches!(value.role.as_str(), "entrypoint" | "worker")
                    && value.port >= 1024
                    && value.reserved_memory_bytes > 0
                    && value.reserved_memory_bytes <= 16 * 1024_u64.pow(4)
                    && !value.endpoint_address.is_loopback()
                    && !value.endpoint_address.is_unspecified()
                    && !value.endpoint_address.is_multicast()
                    && !link_local(value.endpoint_address)
                    && if value.world_size == 1 {
                        value.rank == 0
                            && value.role == "entrypoint"
                            && value.local_address.is_none()
                            && value.master_address.is_none()
                            && value.master_port.is_none()
                    } else {
                        value.local_address.is_some_and(valid_fabric_address)
                            && value.master_address.is_some_and(valid_fabric_address)
                            && value.master_port.is_some_and(|port| port >= 1024)
                    }
                    && valid_alias(&value.alias)
            }
            Self::Stop(value) => valid_common(value.schema_version, &value.plan_digest),
            Self::Uninstall(value) => {
                valid_common(value.schema_version, &value.plan_digest)
                    && lower_hex(&value.recipe_content_sha256, 64)
            }
        };
        if valid {
            Ok(())
        } else {
            Err(ProtocolError::Identity("recipe payload"))
        }
    }
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

fn valid_alias(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 63
        && value.bytes().enumerate().all(|(index, byte)| {
            let edge = index == 0 || index + 1 == value.len();
            byte.is_ascii_lowercase()
                || byte.is_ascii_digit()
                || !edge && matches!(byte, b'.' | b'_' | b'-')
        })
}

fn link_local(value: std::net::IpAddr) -> bool {
    match value {
        std::net::IpAddr::V4(address) => address.is_link_local() || address.is_broadcast(),
        std::net::IpAddr::V6(address) => address.is_unicast_link_local(),
    }
}

fn valid_fabric_address(value: std::net::IpAddr) -> bool {
    !value.is_loopback() && !value.is_unspecified() && !value.is_multicast() && !link_local(value)
}

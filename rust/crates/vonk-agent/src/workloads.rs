use std::{net::IpAddr, path::Path};

use serde::{Deserialize, Serialize};
use thiserror::Error;

#[derive(Debug, Error)]
pub enum WorkloadError {
    #[error("workload specification is invalid: {0}")]
    Invalid(&'static str),
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct WorkloadSpec {
    pub runtime: RuntimeSpec,
    pub artifacts: Vec<ArtifactSpec>,
    pub endpoint: EndpointSpec,
    pub security: SecuritySpec,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RuntimeSpec {
    pub interface: String,
    pub family: String,
    pub image: String,
    pub architecture: String,
    pub arguments: Vec<RuntimeArgument>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RuntimeArgument {
    pub name: String,
    pub value: ArgumentValue,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(untagged)]
pub enum ArgumentValue {
    Boolean(bool),
    Integer(i64),
    String(String),
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ArtifactSpec {
    pub kind: String,
    pub repository: String,
    pub revision: String,
    pub expected_bytes: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct EndpointSpec {
    pub protocol: String,
    pub port: u16,
    pub model_aliases: Vec<String>,
    pub health_path: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct SecuritySpec {
    pub devices: Vec<String>,
    pub capabilities: Vec<String>,
    pub host_network: bool,
    pub privileged: bool,
    pub mounts: Vec<MountSpec>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct MountSpec {
    pub source: String,
    pub target: String,
    pub read_only: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Placement {
    pub rank: u16,
    pub role: String,
    pub world_size: u16,
    pub local_address: Option<IpAddr>,
    pub master_address: Option<IpAddr>,
    pub master_port: Option<u16>,
    pub port: u16,
    pub reserved_memory_bytes: u64,
}

impl WorkloadSpec {
    pub fn validate(&self) -> Result<(), WorkloadError> {
        self.runtime.validate()?;
        if self.artifacts.is_empty() || self.artifacts.len() > 16 {
            return Err(WorkloadError::Invalid("artifacts"));
        }
        for artifact in &self.artifacts {
            artifact.validate()?;
        }
        if self.endpoint.protocol != "openai"
            || self.endpoint.port < 1024
            || self.endpoint.model_aliases.is_empty()
            || self.endpoint.health_path.len() > 256
            || !self.endpoint.health_path.starts_with('/')
            || self.endpoint.health_path.contains("..")
        {
            return Err(WorkloadError::Invalid("endpoint"));
        }
        if self.security.host_network
            || self.security.privileged
            || !self.security.capabilities.is_empty()
            || self.security.mounts
                != [
                    MountSpec {
                        source: "model".to_owned(),
                        target: "/models".to_owned(),
                        read_only: true,
                    },
                    MountSpec {
                        source: "state".to_owned(),
                        target: "/state".to_owned(),
                        read_only: false,
                    },
                ]
            || self
                .security
                .devices
                .iter()
                .any(|value| value != "nvidia.com/gpu=all")
        {
            return Err(WorkloadError::Invalid("security"));
        }
        Ok(())
    }
}

impl RuntimeSpec {
    fn validate(&self) -> Result<(), WorkloadError> {
        if self.interface != "vonk.runtime.v1"
            || !matches!(
                self.family.as_str(),
                "vllm" | "sglang" | "llama.cpp" | "ds4"
            )
            || self.architecture != "linux/arm64"
            || image_digest(&self.image).is_none()
            || self.arguments.len() > 128
        {
            return Err(WorkloadError::Invalid("runtime"));
        }
        for argument in &self.arguments {
            if argument.name.is_empty()
                || argument.name.len() > 64
                || !argument.name.bytes().enumerate().all(|(index, byte)| {
                    if index == 0 {
                        byte.is_ascii_lowercase()
                    } else {
                        byte.is_ascii_lowercase()
                            || byte.is_ascii_digit()
                            || matches!(byte, b'_' | b'-')
                    }
                })
                || matches!(&argument.value, ArgumentValue::String(value) if value.len() > 1024 || value.contains('\0'))
            {
                return Err(WorkloadError::Invalid("runtime argument"));
            }
        }
        Ok(())
    }
}

impl ArtifactSpec {
    fn validate(&self) -> Result<(), WorkloadError> {
        if self.repository.is_empty()
            || self.repository.len() > 512
            || self.repository.contains('\0')
            || self.expected_bytes == 0
        {
            return Err(WorkloadError::Invalid("artifact"));
        }
        let immutable = match self.kind.as_str() {
            "huggingface.snapshot" => {
                lower_hex(&self.revision, 40) || lower_hex(&self.revision, 64)
            }
            "http.file" | "oci.artifact" => self
                .revision
                .strip_prefix("sha256:")
                .is_some_and(|value| lower_hex(value, 64)),
            _ => false,
        };
        if !immutable {
            return Err(WorkloadError::Invalid("artifact revision"));
        }
        Ok(())
    }
}

impl Placement {
    pub fn validate(&self) -> Result<(), WorkloadError> {
        if self.rank >= self.world_size
            || self.world_size == 0
            || !matches!(self.role.as_str(), "entrypoint" | "worker")
            || self.port < 1024
            || self.reserved_memory_bytes == 0
            || if self.world_size == 1 {
                self.local_address.is_some()
                    || self.master_address.is_some()
                    || self.master_port.is_some()
                    || self.rank != 0
                    || self.role != "entrypoint"
            } else {
                self.local_address.is_none()
                    || self.master_address.is_none()
                    || self.master_port.is_none_or(|port| port < 1024)
            }
        {
            return Err(WorkloadError::Invalid("placement"));
        }
        Ok(())
    }
}

pub fn image_digest(image: &str) -> Option<&str> {
    let (name, digest) = image.rsplit_once("@sha256:")?;
    if name.is_empty()
        || name.len() > 512
        || name
            .bytes()
            .any(|byte| byte.is_ascii_whitespace() || byte == 0)
        || !lower_hex(digest, 64)
    {
        return None;
    }
    Some(digest)
}

pub fn managed_path(
    root: &Path,
    category: &str,
    identifier: &str,
) -> Result<std::path::PathBuf, WorkloadError> {
    if !matches!(category, "installations" | "models" | "runs")
        || identifier.is_empty()
        || identifier.len() > 128
        || !identifier
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
    {
        return Err(WorkloadError::Invalid("managed path"));
    }
    Ok(root.join(category).join(identifier))
}

fn lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

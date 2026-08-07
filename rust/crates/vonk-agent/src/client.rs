use std::{fs, time::Duration};

use reqwest::{Certificate, Client, Identity, StatusCode};
use serde::Serialize;
use thiserror::Error;
use url::Url;
use vonk_agent_protocol::{AgentClaim, AgentResult, canonical_json, parse_strict};

use crate::{
    config::AgentConfig,
    identity::{IdentityPaths, active_identity_paths},
    inventory::Inventory,
    pair::{IssuedResponse, verify_ca_pin},
    workloads::WorkloadSpec,
};

const MAX_BODY_BYTES: usize = 64 * 1024;

#[derive(Debug, Error)]
pub enum ClientError {
    #[error("agent credential could not be read")]
    CredentialRead(#[from] std::io::Error),
    #[error("agent TLS identity is invalid")]
    Identity,
    #[error("controller transport failed")]
    Transport(#[from] reqwest::Error),
    #[error("controller temporarily rejected the request")]
    Retryable,
    #[error("agent identity is not authorized")]
    Authentication,
    #[error("controller protocol response is invalid")]
    Protocol,
    #[error("controller CA pin is invalid")]
    Pin,
}

impl ClientError {
    pub fn retryable(&self) -> bool {
        matches!(self, Self::Transport(_) | Self::Retryable)
    }
}

#[derive(Serialize)]
#[serde(deny_unknown_fields)]
struct ClaimRequest<'a> {
    capabilities: &'a [&'a str],
    lease_seconds: u64,
    node_id: &'a str,
    protocol_version: u32,
    wait_seconds: u64,
}

#[derive(Serialize)]
#[serde(deny_unknown_fields)]
struct InventoryRequest<'a> {
    schema_version: u8,
    observed_at: chrono::DateTime<chrono::Utc>,
    #[serde(flatten)]
    inventory: &'a Inventory,
}

#[derive(Serialize)]
#[serde(deny_unknown_fields)]
struct RenewRequest<'a> {
    csr: &'a str,
    node_id: &'a str,
}

#[derive(Serialize)]
#[serde(deny_unknown_fields)]
struct ActivateRequest<'a> {
    generation: u64,
    node_id: &'a str,
}

pub struct AgentHttpClient {
    client: Client,
    controller: Url,
    node_id: String,
}

impl AgentHttpClient {
    pub fn from_config(config: &AgentConfig) -> Result<Self, ClientError> {
        let paths = active_identity_paths(&config.data_dir.join("credentials"))
            .map_err(|_| ClientError::Identity)?;
        Self::from_identity_paths(config, &paths)
    }

    pub fn from_identity_paths(
        config: &AgentConfig,
        paths: &IdentityPaths,
    ) -> Result<Self, ClientError> {
        let ca_pem = fs::read(&config.ca_path)?;
        verify_ca_pin(&ca_pem, &config.ca_sha256).map_err(|_| ClientError::Pin)?;
        let mut identity_pem = fs::read(&paths.certificate)?;
        identity_pem.extend_from_slice(&fs::read(&paths.chain)?);
        identity_pem.extend_from_slice(&fs::read(&paths.private_key)?);
        let identity = Identity::from_pem(&identity_pem).map_err(|_| ClientError::Identity)?;
        let ca = Certificate::from_pem(&ca_pem).map_err(|_| ClientError::Identity)?;
        let client = Client::builder()
            .https_only(true)
            .tls_built_in_root_certs(false)
            .add_root_certificate(ca)
            .identity(identity)
            .connect_timeout(Duration::from_secs(10))
            .timeout(Duration::from_secs(75))
            .build()?;
        Ok(Self {
            client,
            controller: config.controller_url.clone(),
            node_id: config.node_id.clone(),
        })
    }

    pub async fn claim(
        &self,
        capabilities: &[&str],
        wait_seconds: u64,
    ) -> Result<Option<AgentClaim>, ClientError> {
        let response = self
            .client
            .post(self.endpoint("/agent/v1/claim")?)
            .json(&ClaimRequest {
                capabilities,
                lease_seconds: 60,
                node_id: &self.node_id,
                protocol_version: 2,
                wait_seconds: wait_seconds.min(60),
            })
            .send()
            .await?;
        let status = response.status();
        if status == StatusCode::NO_CONTENT {
            return Ok(None);
        }
        classify_status(status)?;
        let body = bounded_body(response).await?;
        parse_claim_response(status.as_u16(), &body)
    }

    pub async fn submit_result(&self, result: &AgentResult) -> Result<(), ClientError> {
        result.validate().map_err(|_| ClientError::Protocol)?;
        let body = canonical_json(result).map_err(|_| ClientError::Protocol)?;
        let response = self
            .client
            .post(self.endpoint("/agent/v1/result")?)
            .header("content-type", "application/json")
            .body(body)
            .send()
            .await?;
        if matches!(
            response.status(),
            StatusCode::NO_CONTENT | StatusCode::CONFLICT
        ) {
            Ok(())
        } else {
            classify_status(response.status())?;
            Err(ClientError::Protocol)
        }
    }

    pub async fn recipe_spec(&self, content_sha256: &str) -> Result<WorkloadSpec, ClientError> {
        if content_sha256.len() != 64
            || !content_sha256
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        {
            return Err(ClientError::Protocol);
        }
        let response = self
            .client
            .get(self.endpoint(&format!("/agent/v1/recipe-specs/{content_sha256}"))?)
            .send()
            .await?;
        classify_status(response.status())?;
        let body = bounded_body(response).await?;
        let spec: WorkloadSpec =
            serde_json::from_slice(&body).map_err(|_| ClientError::Protocol)?;
        spec.validate().map_err(|_| ClientError::Protocol)?;
        Ok(spec)
    }

    pub async fn report_inventory(&self, inventory: &Inventory) -> Result<(), ClientError> {
        let response = self
            .client
            .post(self.endpoint("/agent/v1/inventory")?)
            .json(&InventoryRequest {
                schema_version: 1,
                observed_at: chrono::Utc::now(),
                inventory,
            })
            .send()
            .await?;
        if response.status() == StatusCode::NO_CONTENT {
            Ok(())
        } else {
            classify_status(response.status())?;
            Err(ClientError::Protocol)
        }
    }

    pub async fn renew(&self, csr: &[u8]) -> Result<IssuedResponse, ClientError> {
        let csr = std::str::from_utf8(csr).map_err(|_| ClientError::Protocol)?;
        if csr.is_empty() || csr.len() > 16 * 1024 {
            return Err(ClientError::Protocol);
        }
        let response = self
            .client
            .post(self.endpoint("/agent/v1/renew")?)
            .json(&RenewRequest {
                csr,
                node_id: &self.node_id,
            })
            .send()
            .await?;
        classify_status(response.status())?;
        let body = bounded_body(response).await?;
        let issued: IssuedResponse =
            serde_json::from_slice(&body).map_err(|_| ClientError::Protocol)?;
        if issued.node_id != self.node_id || issued.generation == 0 {
            return Err(ClientError::Protocol);
        }
        Ok(issued)
    }

    pub async fn activate(&self, generation: u64) -> Result<(), ClientError> {
        if generation == 0 {
            return Err(ClientError::Protocol);
        }
        let response = self
            .client
            .post(self.endpoint("/agent/v1/renew/activate")?)
            .json(&ActivateRequest {
                generation,
                node_id: &self.node_id,
            })
            .send()
            .await?;
        if response.status() != StatusCode::NO_CONTENT {
            classify_status(response.status())?;
            return Err(ClientError::Protocol);
        }
        Ok(())
    }

    fn endpoint(&self, path: &str) -> Result<Url, ClientError> {
        self.controller
            .join(path)
            .map_err(|_| ClientError::Protocol)
    }
}

pub fn parse_claim_response(status: u16, body: &[u8]) -> Result<Option<AgentClaim>, ClientError> {
    match status {
        204 if body.is_empty() => Ok(None),
        200 if body.len() <= MAX_BODY_BYTES => {
            let claim: AgentClaim = parse_strict(body).map_err(|_| ClientError::Protocol)?;
            claim.validate().map_err(|_| ClientError::Protocol)?;
            Ok(Some(claim))
        }
        401 | 403 => Err(ClientError::Authentication),
        408 | 429 | 500..=599 => Err(ClientError::Retryable),
        _ => Err(ClientError::Protocol),
    }
}

fn classify_status(status: StatusCode) -> Result<(), ClientError> {
    match status.as_u16() {
        200..=299 => Ok(()),
        401 | 403 => Err(ClientError::Authentication),
        408 | 429 | 500..=599 => Err(ClientError::Retryable),
        _ => Err(ClientError::Protocol),
    }
}

async fn bounded_body(response: reqwest::Response) -> Result<Vec<u8>, ClientError> {
    if response
        .content_length()
        .is_some_and(|length| length > MAX_BODY_BYTES as u64)
    {
        return Err(ClientError::Protocol);
    }
    let body = response.bytes().await?;
    if body.len() > MAX_BODY_BYTES {
        return Err(ClientError::Protocol);
    }
    Ok(body.to_vec())
}

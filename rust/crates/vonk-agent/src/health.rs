use std::time::Duration;

use chrono::{DateTime, Utc};
use serde::Serialize;
use thiserror::Error;

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct HealthEvidence {
    pub recipe_revision_id: String,
    pub recipe_content_sha256: String,
    pub image_digest: String,
    pub artifact_set_digest: String,
    pub model_identity: String,
    pub rank: u16,
    pub world_size: u16,
    pub endpoint: String,
    pub memory_reservation_bytes: u64,
    pub ready: bool,
}

#[derive(Debug, Error)]
pub enum HealthError {
    #[error("workload readiness deadline elapsed")]
    Deadline,
    #[error("workload health path is invalid")]
    Path,
    #[error("workload readiness transport failed")]
    Transport(#[from] reqwest::Error),
}

pub async fn wait_ready(port: u16, path: &str, deadline: DateTime<Utc>) -> Result<(), HealthError> {
    if port < 1024
        || !path.starts_with('/')
        || path.contains("..")
        || path.contains(['?', '#', '\0'])
    {
        return Err(HealthError::Path);
    }
    let client = reqwest::Client::builder()
        .no_proxy()
        .connect_timeout(Duration::from_secs(2))
        .timeout(Duration::from_secs(3))
        .build()?;
    let endpoint = format!("http://127.0.0.1:{port}{path}");
    loop {
        if Utc::now() >= deadline {
            return Err(HealthError::Deadline);
        }
        if let Ok(response) = client.get(&endpoint).send().await
            && response.status().is_success()
            && response
                .content_length()
                .is_none_or(|length| length <= 64 * 1024)
        {
            return Ok(());
        }
        tokio::time::sleep(Duration::from_secs(1)).await;
    }
}

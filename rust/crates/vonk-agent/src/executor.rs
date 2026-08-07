use chrono::Utc;
use serde_json::{Value, json};

use crate::{
    client::{AgentHttpClient, ClientError},
    state::{BeginDecision, StateError, StateStore},
};
use vonk_agent_protocol::AgentClaim;

pub struct ExecutionResult {
    pub state: &'static str,
    pub body: Value,
}

pub trait Executor {
    fn execute(&self, claim: &AgentClaim) -> ExecutionResult;
}

pub struct RejectingExecutor;

impl Executor for RejectingExecutor {
    fn execute(&self, claim: &AgentClaim) -> ExecutionResult {
        ExecutionResult {
            state: "waiting-for-operator",
            body: json!({"operation": claim.operation, "reason": "operation is not enabled by this agent build"}),
        }
    }
}

#[derive(Debug, thiserror::Error)]
pub enum LoopError {
    #[error(transparent)]
    Client(#[from] ClientError),
    #[error(transparent)]
    State(#[from] StateError),
}

pub async fn run_once<E: Executor>(
    client: &AgentHttpClient,
    state: &mut StateStore,
    executor: &E,
    capabilities: &[&str],
    wait_seconds: u64,
) -> Result<(), LoopError> {
    for result in state.pending_results()? {
        client.submit_result(&result).await?;
        state.acknowledge(&result)?;
    }
    let Some(claim) = client.claim(capabilities, wait_seconds).await? else {
        return Ok(());
    };
    let result = match state.begin(&claim, Utc::now()) {
        Ok(BeginDecision::Execute) => {
            let executed = executor.execute(&claim);
            state.finish(&claim, executed.state, executed.body)?
        }
        Ok(BeginDecision::Replay(result)) => result,
        Err(StateError::Busy) => return Ok(()),
        Err(error) => return Err(error.into()),
    };
    client.submit_result(&result).await?;
    state.acknowledge(&result)?;
    Ok(())
}

#![forbid(unsafe_code)]

use chrono::{DateTime, FixedOffset, Utc};
use serde_json::json;
use tempfile::tempdir;
use uuid::Uuid;
use vonk_agent::state::{BeginDecision, StateStore};
use vonk_agent_protocol::{AgentClaim, AgentResult, canonical_json, hex_sha256};

const NODE_ID: &str = "spk_0123456789abcdef0123456789abcdef";

fn claim() -> AgentClaim {
    let payload = json!({"run_id": "run-1"});
    AgentClaim {
        attempt: 1,
        base_commit: "b".repeat(40),
        deadline: DateTime::<FixedOffset>::parse_from_rfc3339("2099-01-01T00:00:00+00:00").unwrap(),
        fence: Uuid::parse_str("44d4e914-34df-4962-a802-d1f7dcd928aa").unwrap(),
        job_id: Uuid::parse_str("84ddf214-f067-4bbf-917e-95df32a07fd8").unwrap(),
        node_id: NODE_ID.to_owned(),
        operation: "recipe.start".to_owned(),
        operation_id: Uuid::parse_str("f450b5ac-5a78-4af5-9670-e874f735e3ee").unwrap(),
        payload_digest: hex_sha256(&canonical_json(&payload).unwrap()),
        payload,
        schema_version: 1,
    }
}

#[test]
fn completed_result_is_redelivered_until_acknowledged() {
    let directory = tempdir().unwrap();
    let path = directory.path().join("state.sqlite");
    let result = {
        let mut state = StateStore::open(&path, NODE_ID).unwrap();
        let claim = claim();
        assert_eq!(
            state.begin(&claim, Utc::now()).unwrap(),
            BeginDecision::Execute
        );
        state
            .finish(&claim, "succeeded", json!({"installed": true}))
            .unwrap()
    };

    let mut restarted = StateStore::open(&path, NODE_ID).unwrap();
    assert_eq!(restarted.pending_results().unwrap(), vec![result.clone()]);
    restarted.acknowledge(&result).unwrap();
    assert!(restarted.pending_results().unwrap().is_empty());
}

#[test]
fn interrupted_mutation_is_not_executed_twice_after_restart() {
    let directory = tempdir().unwrap();
    let path = directory.path().join("state.sqlite");
    let claim = claim();
    {
        let mut state = StateStore::open(&path, NODE_ID).unwrap();
        assert_eq!(
            state.begin(&claim, Utc::now()).unwrap(),
            BeginDecision::Execute
        );
    }

    let mut restarted = StateStore::open(&path, NODE_ID).unwrap();
    restarted.recover_interrupted().unwrap();
    let decision = restarted.begin(&claim, Utc::now()).unwrap();
    assert!(
        matches!(decision, BeginDecision::Replay(ref result) if result.state == "waiting-for-operator")
    );
}

fn create_python_state(path: &std::path::Path, claim: &AgentClaim, active: bool) -> AgentResult {
    let connection = rusqlite::Connection::open(path).unwrap();
    connection
        .execute_batch(
            "CREATE TABLE attempts (
          node_id TEXT NOT NULL, job_id TEXT NOT NULL, operation_id TEXT NOT NULL,
          attempt INTEGER NOT NULL, fence TEXT NOT NULL UNIQUE,
          state TEXT NOT NULL, claim_json BLOB NOT NULL,
          progress_sequence INTEGER NOT NULL, progress_json BLOB, result_json BLOB,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT,
          acknowledged_at TEXT, PRIMARY KEY (node_id,job_id,operation_id,attempt)
        );",
        )
        .unwrap();
    let result = AgentResult {
        attempt: claim.attempt,
        deadline: claim.deadline,
        fence: claim.fence,
        job_id: claim.job_id,
        node_id: claim.node_id.clone(),
        operation_id: claim.operation_id,
        result: json!({"installed": true}),
        schema_version: 1,
        state: "succeeded".to_owned(),
    };
    connection
        .execute(
            "INSERT INTO attempts VALUES (?1,?2,?3,?4,?5,?6,?7,0,NULL,?8,?9,?9,?10,NULL)",
            rusqlite::params![
                claim.node_id,
                claim.job_id.to_string(),
                claim.operation_id.to_string(),
                claim.attempt,
                claim.fence.to_string(),
                if active { "active" } else { "succeeded" },
                canonical_json(claim).unwrap(),
                if active {
                    None
                } else {
                    Some(canonical_json(&result).unwrap())
                },
                "2026-08-07T00:00:00+00:00",
                if active {
                    None
                } else {
                    Some("2026-08-07T00:00:01+00:00")
                },
            ],
        )
        .unwrap();
    result
}

#[test]
fn terminal_python_receipts_are_imported_without_credentials() {
    let directory = tempdir().unwrap();
    let legacy = directory.path().join("agent-state.sqlite3");
    let expected = create_python_state(&legacy, &claim(), false);
    let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();

    assert_eq!(state.import_python_receipts(&legacy).unwrap(), 1);
    assert_eq!(state.pending_results().unwrap(), vec![expected]);
    assert_eq!(state.import_python_receipts(&legacy).unwrap(), 1);
}

#[test]
fn active_python_work_blocks_cutover() {
    let directory = tempdir().unwrap();
    let legacy = directory.path().join("agent-state.sqlite3");
    create_python_state(&legacy, &claim(), true);
    let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();

    assert!(state.import_python_receipts(&legacy).is_err());
    assert!(state.pending_results().unwrap().is_empty());
}

use std::collections::BTreeMap;
use std::fs;
use std::os::unix::fs::PermissionsExt;

use vonk_agent::supervisor_readiness::SupervisorReadiness;

fn environment(credentials: &std::path::Path) -> BTreeMap<String, String> {
    BTreeMap::from([
        (
            "CREDENTIALS_DIRECTORY".to_owned(),
            credentials.display().to_string(),
        ),
        ("VONK_SUPERVISOR_GENERATION".to_owned(), "7".to_owned()),
        ("VONK_SUPERVISOR_SLOT".to_owned(), "b".to_owned()),
        ("VONK_SUPERVISOR_SHA256".to_owned(), "a".repeat(64)),
        ("VONK_SUPERVISOR_STATE_SCHEMA".to_owned(), "2".to_owned()),
    ])
}

#[test]
fn authenticated_generation_readiness_is_published_once_and_canonically() {
    let temp = tempfile::tempdir().unwrap();
    let credentials = temp.path().join("credentials");
    let runtime = temp.path().join("run");
    fs::create_dir(&credentials).unwrap();
    fs::create_dir(&runtime).unwrap();
    fs::set_permissions(&runtime, fs::Permissions::from_mode(0o700)).unwrap();
    fs::write(
        credentials.join("activation-challenge"),
        format!("{}\n", "b".repeat(64)),
    )
    .unwrap();
    let mut reporter =
        SupervisorReadiness::from_environment(&environment(&credentials), &runtime).unwrap();

    let identity = reporter.runtime_identity().unwrap();
    assert_eq!(
        identity.architecture,
        if cfg!(target_arch = "aarch64") {
            "linux-arm64"
        } else {
            "linux-x86_64"
        }
    );
    assert_eq!(identity.active_slot, "B");
    assert_eq!(identity.agent_sha256, "a".repeat(64));
    assert_eq!(identity.supervisor_generation, 7);
    assert_eq!(identity.supervisor_ready_generation, Some(7));
    assert!(identity.self_test_passed);

    assert!(reporter.report().unwrap());
    assert!(!reporter.report().unwrap());
    let raw = fs::read(runtime.join("readiness.json")).unwrap();
    let value: serde_json::Value = serde_json::from_slice(&raw).unwrap();
    assert_eq!(vonk_agent_protocol::canonical_json(&value).unwrap(), raw);
    assert_eq!(value["generation"], 7);
    assert_eq!(value["challenge"], "b".repeat(64));
}

#[test]
fn partial_environment_and_symlinked_challenge_fail_closed() {
    let temp = tempfile::tempdir().unwrap();
    let runtime = temp.path().join("run");
    fs::create_dir(&runtime).unwrap();
    fs::set_permissions(&runtime, fs::Permissions::from_mode(0o700)).unwrap();
    let mut partial = BTreeMap::new();
    partial.insert("VONK_SUPERVISOR_GENERATION".to_owned(), "1".to_owned());
    assert!(SupervisorReadiness::from_environment(&partial, &runtime).is_err());

    let credentials = temp.path().join("credentials");
    fs::create_dir(&credentials).unwrap();
    std::os::unix::fs::symlink(
        temp.path().join("elsewhere"),
        credentials.join("activation-challenge"),
    )
    .unwrap();
    assert!(SupervisorReadiness::from_environment(&environment(&credentials), &runtime).is_err());
}

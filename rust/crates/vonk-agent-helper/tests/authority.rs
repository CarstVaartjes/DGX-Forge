use std::fs;
use std::os::unix::fs::symlink;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use ring::signature::{Ed25519KeyPair, KeyPair};
use tempfile::TempDir;
use uuid::Uuid;
use vonk_agent_helper::operations::{
    CommandOutput, CommandRunner, ManagedRoots, OperationExecutor,
};
use vonk_agent_helper::protocol::{
    AgentSlot, GrantClaims, GrantSignature, GrantVerifier, HostOperation, ManagedArea,
    PeerIdentity, RestartUnit, SignedGrant, canonical_signing_bytes, parse_request,
};

const NOW: i64 = 2_100_000_000;
const NODE_ID: &str = "spk_11111111111111111111111111111111";

fn fixtures() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../agent_protocol/fixtures")
}

#[test]
fn python_fixture_is_the_same_strict_canonical_grant() {
    let raw = fs::read(fixtures().join("host-helper-grant.json")).unwrap();
    let raw = raw.strip_suffix(b"\n").unwrap_or(&raw);
    let request = parse_request(raw).unwrap();
    assert_eq!(request.claims.node_id, NODE_ID);
    assert_eq!(vonk_agent_protocol::canonical_json(&request).unwrap(), raw);
    let public_key =
        hex::decode("8b237d788e8eaaef550c6d125823fa45f1fd5fc29b2c88bdf871119471fc1312").unwrap();
    GrantVerifier::new(&public_key, 971)
        .unwrap()
        .authorize(
            &request,
            &PeerIdentity {
                uid: 1001,
                primary_gid: 971,
                supplementary_gids: Vec::new(),
            },
            2_100_000_000,
        )
        .unwrap();
}

fn signer(seed: u8) -> Ed25519KeyPair {
    Ed25519KeyPair::from_seed_unchecked(&[seed; 32]).unwrap()
}

fn signed(operation: HostOperation, signer: &Ed25519KeyPair) -> SignedGrant {
    let claims = GrantClaims {
        schema_version: 1,
        authority: "vonk.host-maintenance-helper".to_owned(),
        request_id: Uuid::parse_str("10000000-0000-4000-8000-000000000001").unwrap(),
        node_id: NODE_ID.to_owned(),
        issued_at: NOW - 1,
        expires_at: NOW + 60,
        operation,
    };
    let signature = signer.sign(&canonical_signing_bytes(&claims).unwrap());
    SignedGrant {
        schema_version: 1,
        claims,
        signature: GrantSignature {
            algorithm: "ed25519".to_owned(),
            key_id: vonk_agent_protocol::hex_sha256(signer.public_key().as_ref()),
            value: hex::encode(signature.as_ref()),
        },
    }
}

fn grant_verifier(signer: &Ed25519KeyPair) -> GrantVerifier {
    GrantVerifier::new(signer.public_key().as_ref(), 971).unwrap()
}

#[test]
fn every_permitted_operation_has_an_exact_typed_shape() {
    let signer = signer(7);
    let operations = [
        HostOperation::CreateManagedDirectory {
            area: ManagedArea::Models,
            relative_path: "sha256/aa".to_owned(),
        },
        HostOperation::ActivateAgentSlot {
            slot: AgentSlot::B,
            artifact_sha256: "a".repeat(64),
            artifact_signature: "b".repeat(128),
        },
        HostOperation::InstallVonkDeb {
            package_sha256: "c".repeat(64),
            package_signature: "d".repeat(128),
        },
        HostOperation::RestartVonkUnit {
            unit: RestartUnit::Agent,
        },
        HostOperation::ScheduleReboot { delay_seconds: 120 },
    ];

    for operation in operations {
        let request = signed(operation, &signer);
        let raw = vonk_agent_protocol::canonical_json(&request).unwrap();
        assert_eq!(parse_request(&raw).unwrap(), request);
    }
}

#[test]
fn rejects_unknown_fields_and_untyped_process_control() {
    let signer = signer(7);
    let request = signed(
        HostOperation::RestartVonkUnit {
            unit: RestartUnit::Agent,
        },
        &signer,
    );
    let raw = vonk_agent_protocol::canonical_json(&request).unwrap();
    let mut document: serde_json::Value = serde_json::from_slice(&raw).unwrap();
    for (field, value) in [
        ("executable", serde_json::json!("/bin/sh")),
        ("environment", serde_json::json!({"LD_PRELOAD": "/tmp/x"})),
        (
            "arguments",
            serde_json::json!(["--force", "../../etc/shadow"]),
        ),
    ] {
        document["claims"]["operation"]
            .as_object_mut()
            .unwrap()
            .insert(field.to_owned(), value);
        let invalid = vonk_agent_protocol::canonical_json(&document).unwrap();
        assert!(parse_request(&invalid).is_err(), "accepted {field}");
        document["claims"]["operation"]
            .as_object_mut()
            .unwrap()
            .remove(field);
    }
}

#[test]
fn authority_rejects_expiry_bad_signature_and_users_outside_agent_group() {
    let signer = signer(7);
    let verifier = grant_verifier(&signer);
    let request = signed(
        HostOperation::RestartVonkUnit {
            unit: RestartUnit::Supervisor,
        },
        &signer,
    );

    assert!(
        verifier
            .authorize(
                &request,
                &PeerIdentity {
                    uid: 1001,
                    primary_gid: 1001,
                    supplementary_gids: vec![971],
                },
                NOW,
            )
            .is_ok()
    );
    assert!(
        verifier
            .authorize(
                &request,
                &PeerIdentity {
                    uid: 1001,
                    primary_gid: 1001,
                    supplementary_gids: vec![999],
                },
                NOW,
            )
            .is_err()
    );
    assert!(
        verifier
            .authorize(
                &request,
                &PeerIdentity {
                    uid: 1001,
                    primary_gid: 971,
                    supplementary_gids: vec![],
                },
                NOW + 61,
            )
            .is_err()
    );

    let mut forged = request;
    forged.signature.value = "0".repeat(128);
    assert!(
        verifier
            .authorize(
                &forged,
                &PeerIdentity {
                    uid: 1001,
                    primary_gid: 971,
                    supplementary_gids: vec![],
                },
                NOW,
            )
            .is_err()
    );
}

#[derive(Clone, Default)]
struct RecordingRunner {
    calls: SharedCalls,
}

type SharedCalls = Arc<Mutex<Vec<(PathBuf, Vec<String>)>>>;

impl CommandRunner for RecordingRunner {
    fn run(
        &self,
        executable: &std::path::Path,
        arguments: &[String],
    ) -> Result<CommandOutput, String> {
        self.calls
            .lock()
            .unwrap()
            .push((executable.to_path_buf(), arguments.to_vec()));
        let stdout = if arguments.get(2).is_some_and(|value| value == "Package") {
            b"vonk-forge-agent\n".to_vec()
        } else if arguments
            .get(2)
            .is_some_and(|value| value == "Architecture")
        {
            b"arm64\n".to_vec()
        } else {
            Vec::new()
        };
        Ok(CommandOutput {
            success: true,
            stdout,
        })
    }
}

fn fixture() -> (TempDir, ManagedRoots, RecordingRunner, Ed25519KeyPair) {
    let temp = tempfile::tempdir().unwrap();
    let data = temp.path().join("data");
    let roots = ManagedRoots::under(&data);
    fs::create_dir_all(&roots.models).unwrap();
    fs::create_dir_all(&roots.state).unwrap();
    fs::create_dir_all(&roots.slots).unwrap();
    fs::create_dir_all(&roots.incoming).unwrap();
    (temp, roots, RecordingRunner::default(), signer(9))
}

#[test]
fn managed_directory_creation_rejects_traversal_and_symlink_escape() {
    let (temp, roots, runner, release) = fixture();
    let executor =
        OperationExecutor::new(roots.clone(), release.public_key().as_ref(), runner, None).unwrap();

    assert!(
        executor
            .execute(&HostOperation::CreateManagedDirectory {
                area: ManagedArea::Models,
                relative_path: "../escape".to_owned(),
            })
            .is_err()
    );
    symlink(temp.path(), roots.models.join("link")).unwrap();
    assert!(
        executor
            .execute(&HostOperation::CreateManagedDirectory {
                area: ManagedArea::Models,
                relative_path: "link/escape".to_owned(),
            })
            .is_err()
    );
    assert!(!temp.path().join("escape").exists());
}

#[test]
fn artifacts_are_verified_before_slot_or_package_mutation() {
    let (_temp, roots, runner, release) = fixture();
    let executor = OperationExecutor::new(
        roots.clone(),
        release.public_key().as_ref(),
        runner.clone(),
        None,
    )
    .unwrap();

    let slot = roots.slots.join("a");
    fs::create_dir_all(&slot).unwrap();
    let agent = slot.join("vonk-agent");
    fs::write(&agent, b"trusted agent").unwrap();
    let digest = vonk_agent_protocol::hex_sha256(b"trusted agent");
    let signature = release.sign(
        vonk_agent_helper::protocol::artifact_signing_bytes("agent", &digest)
            .unwrap()
            .as_slice(),
    );
    executor
        .execute(&HostOperation::ActivateAgentSlot {
            slot: AgentSlot::A,
            artifact_sha256: digest.clone(),
            artifact_signature: hex::encode(signature.as_ref()),
        })
        .unwrap();
    assert_eq!(
        runner.calls.lock().unwrap()[0],
        (
            PathBuf::from("/usr/lib/vonk-forge/vonk-agent-supervisor"),
            vec![
                "activate".to_owned(),
                "--slot".to_owned(),
                "a".to_owned(),
                "--sha256".to_owned(),
                digest,
            ],
        )
    );

    let bad_package = "e".repeat(64);
    fs::write(
        roots.incoming.join(format!("{bad_package}.deb")),
        b"not that digest",
    )
    .unwrap();
    assert!(
        executor
            .execute(&HostOperation::InstallVonkDeb {
                package_sha256: bad_package,
                package_signature: "0".repeat(128),
            })
            .is_err()
    );
    assert_eq!(runner.calls.lock().unwrap().len(), 1);
}

#[test]
fn package_restart_and_reboot_commands_are_compiled_not_caller_supplied() {
    let (_temp, roots, runner, release) = fixture();
    let executor = OperationExecutor::new(
        roots.clone(),
        release.public_key().as_ref(),
        runner.clone(),
        None,
    )
    .unwrap();
    let package = b"signed deb";
    let digest = vonk_agent_protocol::hex_sha256(package);
    fs::write(roots.incoming.join(format!("{digest}.deb")), package).unwrap();
    let signature = release.sign(
        vonk_agent_helper::protocol::artifact_signing_bytes("deb", &digest)
            .unwrap()
            .as_slice(),
    );

    executor
        .execute(&HostOperation::InstallVonkDeb {
            package_sha256: digest.clone(),
            package_signature: hex::encode(signature.as_ref()),
        })
        .unwrap();
    executor
        .execute(&HostOperation::RestartVonkUnit {
            unit: RestartUnit::Agent,
        })
        .unwrap();
    executor
        .execute(&HostOperation::ScheduleReboot { delay_seconds: 300 })
        .unwrap();
    assert!(
        executor
            .execute(&HostOperation::ScheduleReboot { delay_seconds: 5 })
            .is_err()
    );

    let calls = runner.calls.lock().unwrap();
    assert_eq!(calls[0].0, PathBuf::from("/usr/bin/dpkg-deb"));
    assert_eq!(calls[0].1[0], "--field");
    assert_eq!(calls[1].1[2], "Architecture");
    assert_eq!(calls[2].0, PathBuf::from("/usr/bin/dpkg"));
    assert_eq!(
        calls[3],
        (
            PathBuf::from("/usr/bin/systemctl"),
            vec!["restart".to_owned(), "vonk-agent.service".to_owned()],
        )
    );
    assert_eq!(calls[4].0, PathBuf::from("/usr/bin/systemd-run"));
    assert!(calls[4].1.contains(&"--on-active=300s".to_owned()));
}

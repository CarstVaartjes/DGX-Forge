#![forbid(unsafe_code)]

use std::{cell::RefCell, collections::VecDeque, fs, net::IpAddr, path::Path, time::Duration};

use tempfile::tempdir;
use vonk_agent::{
    oci::OciRuntime,
    process::{ProcessError, ProcessOutput, ProcessRunner, Program},
    workloads::{
        ArgumentValue, ArtifactSpec, EndpointSpec, MountSpec, Placement, RuntimeArgument,
        RuntimeSpec, SecuritySpec, WorkloadSpec,
    },
};

const DIGEST: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

struct FakeRunner {
    calls: RefCell<Vec<(Program, Vec<String>)>>,
    outputs: RefCell<VecDeque<ProcessOutput>>,
}

impl ProcessRunner for FakeRunner {
    fn run(
        &self,
        program: Program,
        arguments: &[String],
        _timeout: Duration,
    ) -> Result<ProcessOutput, ProcessError> {
        self.calls.borrow_mut().push((program, arguments.to_vec()));
        if program == Program::Curl {
            let destination = arguments
                .windows(2)
                .find(|values| values[0] == "--output")
                .map(|values| &values[1])
                .unwrap();
            if destination.ends_with(".huggingface-model.json") {
                fs::write(
                    destination,
                    br#"{"siblings":[{"rfilename":"weights.bin","lfs":{"sha256":"9a129038d9a00aed0cf6a7ea059ca50a813449061ab87848cf1a13eafdf33b2c"}}]}"#,
                )?;
            } else {
                fs::write(destination, b"weights")?;
            }
        }
        Ok(self.outputs.borrow_mut().pop_front().unwrap())
    }
}

struct BudgetRunner {
    inner: FakeRunner,
    budgets: RefCell<Vec<u64>>,
}

impl ProcessRunner for BudgetRunner {
    fn run(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
    ) -> Result<ProcessOutput, ProcessError> {
        self.inner.run(program, arguments, timeout)
    }

    fn run_bounded_directory(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
        directory: &Path,
        maximum_bytes: u64,
    ) -> Result<ProcessOutput, ProcessError> {
        self.budgets.borrow_mut().push(maximum_bytes);
        fs::write(directory.join("artifact"), b"weights")?;
        self.inner.run(program, arguments, timeout)
    }
}

fn spec() -> WorkloadSpec {
    WorkloadSpec {
        runtime: RuntimeSpec {
            interface: "vonk.runtime.v1".to_owned(),
            family: "vllm".to_owned(),
            image: format!("registry.example/vonk/vllm@sha256:{DIGEST}"),
            architecture: "linux/arm64".to_owned(),
            arguments: vec![
                RuntimeArgument {
                    name: "max_model_len".to_owned(),
                    value: ArgumentValue::Integer(32768),
                },
                RuntimeArgument {
                    name: "enable_prefix_caching".to_owned(),
                    value: ArgumentValue::Boolean(true),
                },
            ],
        },
        artifacts: vec![ArtifactSpec {
            kind: "huggingface.snapshot".to_owned(),
            repository: "publisher/model".to_owned(),
            revision: "b".repeat(40),
            expected_bytes: 7,
        }],
        endpoint: EndpointSpec {
            protocol: "openai".to_owned(),
            port: 8000,
            model_aliases: vec!["model".to_owned()],
            health_path: "/v1/models".to_owned(),
        },
        security: SecuritySpec {
            devices: vec!["nvidia.com/gpu=all".to_owned()],
            capabilities: vec![],
            host_network: false,
            privileged: false,
            mounts: vec![
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
            ],
        },
    }
}

#[test]
fn workload_schema_rejects_shell_privilege_environment_and_host_paths() {
    let original = serde_json::to_value(spec()).unwrap();
    for (field, value) in [
        ("shell", serde_json::json!("curl evil")),
        ("environment", serde_json::json!({"TOKEN": "secret"})),
        ("host_path", serde_json::json!("/etc")),
    ] {
        let mut mutated = original.clone();
        mutated
            .as_object_mut()
            .unwrap()
            .insert(field.to_owned(), value);
        assert!(serde_json::from_value::<WorkloadSpec>(mutated).is_err());
    }
    let mut privileged = spec();
    privileged.security.privileged = true;
    assert!(privileged.validate().is_err());

    let mut private_interface = spec();
    private_interface.runtime.interface = "publisher-specific.v1".to_owned();
    assert!(private_interface.validate().is_err());

    let mut incomplete_mounts = spec();
    incomplete_mounts.security.mounts.pop();
    assert!(incomplete_mounts.validate().is_err());
}

#[test]
fn image_is_pulled_and_verified_by_digest() {
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        outputs: RefCell::new(VecDeque::from([
            ProcessOutput {
                success: true,
                stdout: vec![],
                stderr: vec![],
            },
            ProcessOutput {
                success: true,
                stdout: format!("sha256:{DIGEST}\tlinux\tarm64\tv1\t\n").into_bytes(),
                stderr: vec![],
            },
        ])),
    };
    let directory = tempdir().unwrap();
    OciRuntime {
        runner: &runner,
        data_root: directory.path(),
        huggingface_curl_config: None,
    }
    .verify_image(&spec())
    .unwrap();
    let calls = runner.calls.borrow();
    assert_eq!(calls[0].0, Program::Podman);
    assert_eq!(calls[0].1[0], "pull");
    assert_eq!(calls[1].1[..3], ["image", "inspect", "--format"]);
    assert!(calls[1].1[3].contains(".Config.User"));
    drop(calls);

    let non_root_runner = FakeRunner {
        calls: RefCell::new(vec![]),
        outputs: RefCell::new(VecDeque::from([
            ProcessOutput {
                success: true,
                stdout: vec![],
                stderr: vec![],
            },
            ProcessOutput {
                success: true,
                stdout: format!("sha256:{DIGEST}\tlinux\tarm64\tv1\t10001:10001\n").into_bytes(),
                stderr: vec![],
            },
        ])),
    };
    assert!(
        OciRuntime {
            runner: &non_root_runner,
            data_root: directory.path(),
            huggingface_curl_config: None,
        }
        .verify_image(&spec())
        .is_err()
    );
}

#[test]
fn container_arguments_are_typed_and_hardened() {
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        outputs: RefCell::new(VecDeque::new()),
    };
    let directory = tempdir().unwrap();
    let arguments = OciRuntime {
        runner: &runner,
        data_root: directory.path(),
        huggingface_curl_config: None,
    }
    .start_arguments(
        &spec(),
        "cb555393-764b-4eb6-8f15-b416d289428f",
        "45ea6921-50c9-4971-be2a-4cd04ce05069",
        &Placement {
            rank: 1,
            role: "worker".to_owned(),
            world_size: 2,
            local_address: Some("192.168.100.11".parse::<IpAddr>().unwrap()),
            master_address: Some("192.168.100.10".parse::<IpAddr>().unwrap()),
            master_port: Some(29500),
            port: 8101,
            reserved_memory_bytes: 64 * 1024 * 1024 * 1024,
        },
    )
    .unwrap();

    for required in [
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "slirp4netns:allow_host_loopback=false",
        "--device",
        "nvidia.com/gpu=all",
        "VONK_RANK=1",
        "VONK_WORLD_SIZE=2",
        "VONK_MASTER_ADDR=192.168.100.10",
        "VONK_LOCAL_ADDR=192.168.100.11",
        "VONK_MASTER_PORT=29500",
        "VONK_RUNTIME_SPEC=/run/vonk/runtime.json",
        "VONK_MODEL_ROOT=/models",
        "VONK_STATE_ROOT=/state",
        "VONK_LISTEN_HOST=0.0.0.0",
        "VONK_LISTEN_PORT=8000",
    ] {
        assert!(
            arguments.iter().any(|value| value == required),
            "{required}"
        );
    }
    assert!(
        !arguments
            .iter()
            .any(|value| value == "--privileged" || value == "--network=host" || value == "--gpus")
    );
    assert!(
        arguments
            .iter()
            .any(|value| value.ends_with("dst=/models,readonly"))
    );
    assert!(
        arguments
            .iter()
            .any(|value| value.ends_with("dst=/run/vonk/runtime.json,readonly"))
    );
    assert!(
        arguments
            .windows(2)
            .any(|values| values == ["--max-model-len", "32768"])
    );
}

#[test]
fn mutable_artifact_revisions_are_rejected_at_the_agent_boundary() {
    let mut workload = spec();
    workload.artifacts[0].revision = "main".to_owned();
    assert!(workload.validate().is_err());
}

#[test]
fn installation_records_and_rechecks_a_content_manifest() {
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        outputs: RefCell::new(VecDeque::from([
            ProcessOutput {
                success: true,
                stdout: vec![],
                stderr: vec![],
            },
            ProcessOutput {
                success: true,
                stdout: format!("sha256:{DIGEST}\tlinux\tarm64\tv1\t\n").into_bytes(),
                stderr: vec![],
            },
            ProcessOutput {
                success: true,
                stdout: b"200\t\n".to_vec(),
                stderr: vec![],
            },
            ProcessOutput {
                success: true,
                stdout: b"200\t\n".to_vec(),
                stderr: vec![],
            },
        ])),
    };
    let directory = tempdir().unwrap();
    let runtime = OciRuntime {
        runner: &runner,
        data_root: directory.path(),
        huggingface_curl_config: None,
    };
    let installation_id = "cb555393-764b-4eb6-8f15-b416d289428f";

    runtime.install(&spec(), installation_id, DIGEST).unwrap();
    runtime.verify_installation(installation_id).unwrap();
    let weights = fs::read_dir(directory.path().join("models").join("sha256"))
        .unwrap()
        .next()
        .unwrap()
        .unwrap()
        .path()
        .join("weights.bin");
    fs::write(weights, b"tampered").unwrap();

    assert!(runtime.verify_installation(installation_id).is_err());
}

#[test]
fn http_artifacts_reject_private_hosts_before_curl_runs() {
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        outputs: RefCell::new(VecDeque::from([
            ProcessOutput {
                success: true,
                stdout: vec![],
                stderr: vec![],
            },
            ProcessOutput {
                success: true,
                stdout: format!("sha256:{DIGEST}\tlinux\tarm64\tv1\t\n").into_bytes(),
                stderr: vec![],
            },
            ProcessOutput {
                success: true,
                stdout: b"200\t\n".to_vec(),
                stderr: vec![],
            },
        ])),
    };
    let directory = tempdir().unwrap();
    let mut workload = spec();
    workload.artifacts[0] = ArtifactSpec {
        kind: "http.file".to_owned(),
        repository: "https://127.0.0.1/private".to_owned(),
        revision: "sha256:9a129038d9a00aed0cf6a7ea059ca50a813449061ab87848cf1a13eafdf33b2c"
            .to_owned(),
        expected_bytes: 7,
    };

    assert!(
        OciRuntime {
            runner: &runner,
            data_root: directory.path(),
            huggingface_curl_config: None,
        }
        .install(&workload, "cb555393-764b-4eb6-8f15-b416d289428f", DIGEST)
        .is_err()
    );
    assert!(
        !runner
            .calls
            .borrow()
            .iter()
            .any(|call| call.0 == Program::Curl)
    );
}

#[test]
fn http_artifacts_are_https_only_and_byte_limited_without_implicit_redirects() {
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        outputs: RefCell::new(VecDeque::from([
            ProcessOutput {
                success: true,
                stdout: vec![],
                stderr: vec![],
            },
            ProcessOutput {
                success: true,
                stdout: format!("sha256:{DIGEST}\tlinux\tarm64\tv1\t\n").into_bytes(),
                stderr: vec![],
            },
            ProcessOutput {
                success: true,
                stdout: b"200\t\n".to_vec(),
                stderr: vec![],
            },
        ])),
    };
    let directory = tempdir().unwrap();
    let mut workload = spec();
    workload.artifacts[0] = ArtifactSpec {
        kind: "http.file".to_owned(),
        repository: "https://93.184.216.34/artifact".to_owned(),
        revision: "sha256:9a129038d9a00aed0cf6a7ea059ca50a813449061ab87848cf1a13eafdf33b2c"
            .to_owned(),
        expected_bytes: 7,
    };

    OciRuntime {
        runner: &runner,
        data_root: directory.path(),
        huggingface_curl_config: None,
    }
    .install(&workload, "cb555393-764b-4eb6-8f15-b416d289428f", DIGEST)
    .unwrap();

    let calls = runner.calls.borrow();
    let arguments = &calls.iter().find(|call| call.0 == Program::Curl).unwrap().1;
    assert!(
        arguments
            .windows(2)
            .any(|pair| pair == ["--max-filesize", "7"])
    );
    assert!(
        arguments
            .windows(2)
            .any(|pair| pair == ["--max-redirs", "0"])
    );
    assert!(!arguments.iter().any(|argument| argument == "--location"));
}

#[test]
fn oci_artifacts_reject_private_registry_hosts_before_oras_runs() {
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        outputs: RefCell::new(VecDeque::from([
            ProcessOutput {
                success: true,
                stdout: vec![],
                stderr: vec![],
            },
            ProcessOutput {
                success: true,
                stdout: format!("sha256:{DIGEST}\tlinux\tarm64\tv1\t\n").into_bytes(),
                stderr: vec![],
            },
            ProcessOutput {
                success: true,
                stdout: vec![],
                stderr: vec![],
            },
        ])),
    };
    let directory = tempdir().unwrap();
    let mut workload = spec();
    workload.artifacts[0] = ArtifactSpec {
        kind: "oci.artifact".to_owned(),
        repository: "127.0.0.1/private/artifact".to_owned(),
        revision: format!("sha256:{DIGEST}"),
        expected_bytes: 7,
    };

    assert!(
        OciRuntime {
            runner: &runner,
            data_root: directory.path(),
            huggingface_curl_config: None,
        }
        .install(&workload, "cb555393-764b-4eb6-8f15-b416d289428f", DIGEST)
        .is_err()
    );
    assert!(
        !runner
            .calls
            .borrow()
            .iter()
            .any(|call| call.0 == Program::Oras)
    );
}

#[test]
fn oci_artifacts_run_under_the_declared_staging_budget() {
    let runner = BudgetRunner {
        inner: FakeRunner {
            calls: RefCell::new(vec![]),
            outputs: RefCell::new(VecDeque::from([
                ProcessOutput {
                    success: true,
                    stdout: vec![],
                    stderr: vec![],
                },
                ProcessOutput {
                    success: true,
                    stdout: format!("sha256:{DIGEST}\tlinux\tarm64\tv1\t\n").into_bytes(),
                    stderr: vec![],
                },
                ProcessOutput {
                    success: true,
                    stdout: vec![],
                    stderr: vec![],
                },
            ])),
        },
        budgets: RefCell::new(vec![]),
    };
    let directory = tempdir().unwrap();
    let mut workload = spec();
    workload.artifacts[0] = ArtifactSpec {
        kind: "oci.artifact".to_owned(),
        repository: "ghcr.io/vonkforge/public-artifact".to_owned(),
        revision: "sha256:9a129038d9a00aed0cf6a7ea059ca50a813449061ab87848cf1a13eafdf33b2c"
            .to_owned(),
        expected_bytes: 7,
    };

    OciRuntime {
        runner: &runner,
        data_root: directory.path(),
        huggingface_curl_config: None,
    }
    .install(&workload, "cb555393-764b-4eb6-8f15-b416d289428f", DIGEST)
    .unwrap();

    assert_eq!(*runner.budgets.borrow(), [7]);
    let calls = runner.inner.calls.borrow();
    let oras = calls.iter().find(|call| call.0 == Program::Oras).unwrap();
    let resolve = oras
        .1
        .iter()
        .position(|value| value == "--resolve")
        .unwrap();
    assert!(oras.1[resolve + 1].starts_with("ghcr.io:443:"));
}

#[test]
fn start_writes_a_bounded_standard_runtime_contract() {
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        outputs: RefCell::new(VecDeque::from([ProcessOutput {
            success: true,
            stdout: b"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc\n".to_vec(),
            stderr: vec![],
        }])),
    };
    let directory = tempdir().unwrap();
    let run_id = "45ea6921-50c9-4971-be2a-4cd04ce05069";
    OciRuntime {
        runner: &runner,
        data_root: directory.path(),
        huggingface_curl_config: None,
    }
    .start(
        &spec(),
        "cb555393-764b-4eb6-8f15-b416d289428f",
        run_id,
        &Placement {
            rank: 0,
            role: "entrypoint".to_owned(),
            world_size: 1,
            local_address: None,
            master_address: None,
            master_port: None,
            port: 8101,
            reserved_memory_bytes: 64 * 1024 * 1024 * 1024,
        },
    )
    .unwrap();

    let contract: serde_json::Value = serde_json::from_slice(
        &fs::read(
            directory
                .path()
                .join("runs")
                .join(run_id)
                .join("runtime.json"),
        )
        .unwrap(),
    )
    .unwrap();
    assert_eq!(contract["interface"], "vonk.runtime.v1");
    assert_eq!(
        contract["artifacts"][0]["path"]
            .as_str()
            .unwrap()
            .split('/')
            .count(),
        4
    );
    assert_eq!(contract["endpoint"]["listen_port"], 8000);
    assert_eq!(contract["placement"]["rank"], 0);
}

#[test]
fn stop_is_idempotent_when_a_gang_rank_never_started() {
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
        outputs: RefCell::new(VecDeque::from([ProcessOutput {
            success: true,
            stdout: vec![],
            stderr: vec![],
        }])),
    };
    let directory = tempdir().unwrap();
    let run_id = "45ea6921-50c9-4971-be2a-4cd04ce05069";

    OciRuntime {
        runner: &runner,
        data_root: directory.path(),
        huggingface_curl_config: None,
    }
    .stop(run_id)
    .unwrap();

    let calls = runner.calls.borrow();
    assert_eq!(calls[0].0, Program::Podman);
    assert_eq!(
        calls[0].1,
        vec![
            "rm".to_owned(),
            "--force".to_owned(),
            "--ignore".to_owned(),
            "--time".to_owned(),
            "30".to_owned(),
            format!("vonk-{run_id}"),
        ]
    );
}

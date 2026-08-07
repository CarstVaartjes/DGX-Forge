use std::fs::{self, File, OpenOptions};
use std::io::Read;
use std::os::unix::fs::{MetadataExt, PermissionsExt, symlink};
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::thread;
use std::time::Duration;

use ring::signature;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use thiserror::Error;
use vonk_agent_protocol::hex_sha256;
use wait_timeout::ChildExt;

use crate::protocol::{AgentSlot, HostOperation, ManagedArea, RestartUnit, artifact_signing_bytes};

const MAX_ARTIFACT_BYTES: u64 = 1024 * 1024 * 1024;
const MAX_COMMAND_OUTPUT_BYTES: u64 = 4096;
static ACTIVATION_SEQUENCE: AtomicU64 = AtomicU64::new(1);

#[derive(Debug, Error)]
pub enum OperationError {
    #[error("managed operation is invalid")]
    InvalidOperation,
    #[error("managed path is unsafe")]
    UnsafePath,
    #[error("artifact verification failed")]
    InvalidArtifact,
    #[error("compiled command failed")]
    CommandFailed,
    #[error("host mutation failed")]
    Io(#[from] std::io::Error),
}

#[derive(Debug, Clone)]
pub struct ManagedRoots {
    pub data: PathBuf,
    pub models: PathBuf,
    pub state: PathBuf,
    pub workloads: PathBuf,
    pub slots: PathBuf,
    pub incoming: PathBuf,
    pub active_slot: PathBuf,
}

impl ManagedRoots {
    pub fn under(data: &Path) -> Self {
        Self {
            data: data.to_path_buf(),
            models: data.join("models"),
            state: data.join("state"),
            workloads: data.join("workloads"),
            slots: data.join("slots"),
            incoming: data.join("incoming"),
            active_slot: data.join("current"),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CommandOutput {
    pub success: bool,
    pub stdout: Vec<u8>,
}

pub trait CommandRunner: Send + Sync {
    fn run(&self, executable: &Path, arguments: &[String]) -> Result<CommandOutput, String>;
}

#[derive(Debug, Clone, Copy)]
pub struct ProcessCommandRunner;

impl CommandRunner for ProcessCommandRunner {
    fn run(&self, executable: &Path, arguments: &[String]) -> Result<CommandOutput, String> {
        if !matches!(
            executable.to_str(),
            Some(
                "/usr/bin/dpkg-deb"
                    | "/usr/bin/dpkg"
                    | "/usr/bin/systemctl"
                    | "/usr/bin/systemd-run"
            )
        ) {
            return Err("executable is not compiled into the helper".to_owned());
        }
        let capture_output = executable == Path::new("/usr/bin/dpkg-deb");
        let mut command = Command::new(executable);
        command
            .args(arguments)
            .env_clear()
            .env("LANG", "C.UTF-8")
            .env("LC_ALL", "C.UTF-8")
            .env("PATH", "/usr/bin:/bin")
            .current_dir("/")
            .stdin(Stdio::null())
            .stderr(Stdio::null())
            .stdout(if capture_output {
                Stdio::piped()
            } else {
                Stdio::null()
            });
        let mut child = command
            .spawn()
            .map_err(|_| "compiled command could not start".to_owned())?;
        let reader = child.stdout.take().map(|mut stdout| {
            thread::spawn(move || {
                let mut value = Vec::new();
                stdout
                    .by_ref()
                    .take(MAX_COMMAND_OUTPUT_BYTES + 1)
                    .read_to_end(&mut value)
                    .map(|_| value)
            })
        });
        let timeout = if executable == Path::new("/usr/bin/dpkg") {
            Duration::from_secs(120)
        } else {
            Duration::from_secs(30)
        };
        let status = match child.wait_timeout(timeout) {
            Ok(Some(status)) => status,
            Ok(None) | Err(_) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err("compiled command exceeded its deadline".to_owned());
            }
        };
        let stdout = match reader {
            Some(reader) => reader
                .join()
                .map_err(|_| "compiled command output failed".to_owned())?
                .map_err(|_| "compiled command output failed".to_owned())?,
            None => Vec::new(),
        };
        if stdout.len() as u64 > MAX_COMMAND_OUTPUT_BYTES {
            return Err("compiled command output exceeded its bound".to_owned());
        }
        Ok(CommandOutput {
            success: status.success(),
            stdout,
        })
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct OperationOutcome {
    pub schema_version: u8,
    pub status: String,
    pub evidence_sha256: String,
}

pub struct OperationExecutor<R> {
    roots: ManagedRoots,
    release_public_key: [u8; 32],
    runner: R,
    required_owner_uid: Option<u32>,
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub fn new(
        roots: ManagedRoots,
        release_public_key: &[u8],
        runner: R,
        required_owner_uid: Option<u32>,
    ) -> Result<Self, OperationError> {
        let release_public_key = release_public_key
            .try_into()
            .map_err(|_| OperationError::InvalidArtifact)?;
        if !roots.data.is_absolute()
            || ![
                &roots.models,
                &roots.state,
                &roots.workloads,
                &roots.slots,
                &roots.incoming,
                &roots.active_slot,
            ]
            .iter()
            .all(|path| path.starts_with(&roots.data))
        {
            return Err(OperationError::UnsafePath);
        }
        Ok(Self {
            roots,
            release_public_key,
            runner,
            required_owner_uid,
        })
    }

    pub fn execute(&self, operation: &HostOperation) -> Result<OperationOutcome, OperationError> {
        operation
            .validate()
            .map_err(|_| OperationError::InvalidOperation)?;
        self.require_directory(&self.roots.data)?;
        let (status, evidence) = match operation {
            HostOperation::CreateManagedDirectory {
                area,
                relative_path,
            } => {
                let path = self.create_managed_directory(area, relative_path)?;
                ("directory-created", path.to_string_lossy().into_owned())
            }
            HostOperation::ActivateAgentSlot {
                slot,
                artifact_sha256,
                artifact_signature,
            } => {
                self.activate_slot(slot, artifact_sha256, artifact_signature)?;
                ("slot-activated", artifact_sha256.clone())
            }
            HostOperation::InstallVonkDeb {
                package_sha256,
                package_signature,
            } => {
                self.install_package(package_sha256, package_signature)?;
                ("package-installed", package_sha256.clone())
            }
            HostOperation::RestartVonkUnit { unit } => {
                let unit_name = self.restart_unit(unit)?;
                ("unit-restarted", unit_name.to_owned())
            }
            HostOperation::ScheduleReboot { delay_seconds } => {
                self.schedule_reboot(*delay_seconds)?;
                ("reboot-scheduled", delay_seconds.to_string())
            }
        };
        Ok(OperationOutcome {
            schema_version: 1,
            status: status.to_owned(),
            evidence_sha256: hex_sha256(evidence.as_bytes()),
        })
    }

    fn create_managed_directory(
        &self,
        area: &ManagedArea,
        relative_path: &str,
    ) -> Result<PathBuf, OperationError> {
        let root = match area {
            ManagedArea::Models => &self.roots.models,
            ManagedArea::State => &self.roots.state,
            ManagedArea::Workloads => &self.roots.workloads,
        };
        let canonical_root = fs::canonicalize(root).map_err(|_| OperationError::UnsafePath)?;
        self.require_directory(root)?;
        let relative = Path::new(relative_path);
        if relative.is_absolute()
            || relative
                .components()
                .any(|component| !matches!(component, Component::Normal(_)))
        {
            return Err(OperationError::UnsafePath);
        }
        let mut current = root.clone();
        for component in relative.components() {
            let Component::Normal(component) = component else {
                return Err(OperationError::UnsafePath);
            };
            current.push(component);
            match fs::symlink_metadata(&current) {
                Ok(metadata) => {
                    if metadata.file_type().is_symlink() || !metadata.is_dir() {
                        return Err(OperationError::UnsafePath);
                    }
                }
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                    fs::create_dir(&current)?;
                    fs::set_permissions(&current, fs::Permissions::from_mode(0o750))?;
                }
                Err(error) => return Err(OperationError::Io(error)),
            }
            let canonical = fs::canonicalize(&current).map_err(|_| OperationError::UnsafePath)?;
            if !canonical.starts_with(&canonical_root) {
                return Err(OperationError::UnsafePath);
            }
            self.require_directory(&current)?;
        }
        sync_directory(root)?;
        Ok(current)
    }

    fn activate_slot(
        &self,
        slot: &AgentSlot,
        digest: &str,
        detached_signature: &str,
    ) -> Result<(), OperationError> {
        self.require_directory(&self.roots.slots)?;
        let slot_name = match slot {
            AgentSlot::A => "a",
            AgentSlot::B => "b",
        };
        let artifact = self.roots.slots.join(slot_name).join("vonk-agent");
        self.verify_artifact(&artifact, "agent", digest, detached_signature)?;

        if let Ok(metadata) = fs::symlink_metadata(&self.roots.active_slot)
            && !metadata.file_type().is_symlink()
        {
            return Err(OperationError::UnsafePath);
        }
        let sequence = ACTIVATION_SEQUENCE.fetch_add(1, Ordering::Relaxed);
        let temporary = self
            .roots
            .data
            .join(format!(".current-{}-{sequence}", std::process::id()));
        symlink(format!("slots/{slot_name}"), &temporary)?;
        if let Err(error) = fs::rename(&temporary, &self.roots.active_slot) {
            let _ = fs::remove_file(&temporary);
            return Err(OperationError::Io(error));
        }
        sync_directory(&self.roots.data)
    }

    fn install_package(
        &self,
        digest: &str,
        detached_signature: &str,
    ) -> Result<(), OperationError> {
        self.require_directory(&self.roots.incoming)?;
        let package = self.roots.incoming.join(format!("{digest}.deb"));
        self.verify_artifact(&package, "deb", digest, detached_signature)?;
        let package_name = package.to_string_lossy().into_owned();
        self.require_field(&package_name, "Package", "vonk-forge-agent")?;
        self.require_field(&package_name, "Architecture", "arm64")?;
        let result = self
            .runner
            .run(
                Path::new("/usr/bin/dpkg"),
                &[
                    "--install".to_owned(),
                    "--force-confold".to_owned(),
                    package_name,
                ],
            )
            .map_err(|_| OperationError::CommandFailed)?;
        if !result.success {
            return Err(OperationError::CommandFailed);
        }
        Ok(())
    }

    fn require_field(
        &self,
        package: &str,
        field: &str,
        expected: &str,
    ) -> Result<(), OperationError> {
        let result = self
            .runner
            .run(
                Path::new("/usr/bin/dpkg-deb"),
                &["--field".to_owned(), package.to_owned(), field.to_owned()],
            )
            .map_err(|_| OperationError::CommandFailed)?;
        if !result.success || result.stdout != format!("{expected}\n").as_bytes() {
            return Err(OperationError::InvalidArtifact);
        }
        Ok(())
    }

    fn restart_unit(&self, unit: &RestartUnit) -> Result<&'static str, OperationError> {
        let unit = match unit {
            RestartUnit::Agent => "vonk-agent.service",
            RestartUnit::Supervisor => "vonk-agent-supervisor.service",
            RestartUnit::Helper => "vonk-agent-helper.service",
        };
        if matches!(unit, "vonk-agent-helper.service") {
            let result = self
                .runner
                .run(
                    Path::new("/usr/bin/systemd-run"),
                    &[
                        "--quiet".to_owned(),
                        "--collect".to_owned(),
                        "--unit=vonk-forge-helper-restart.service".to_owned(),
                        "--on-active=1s".to_owned(),
                        "/usr/bin/systemctl".to_owned(),
                        "restart".to_owned(),
                        unit.to_owned(),
                    ],
                )
                .map_err(|_| OperationError::CommandFailed)?;
            if !result.success {
                return Err(OperationError::CommandFailed);
            }
            return Ok(unit);
        }
        let result = self
            .runner
            .run(
                Path::new("/usr/bin/systemctl"),
                &["restart".to_owned(), unit.to_owned()],
            )
            .map_err(|_| OperationError::CommandFailed)?;
        if !result.success {
            return Err(OperationError::CommandFailed);
        }
        Ok(unit)
    }

    fn schedule_reboot(&self, delay_seconds: u16) -> Result<(), OperationError> {
        if !(60..=3600).contains(&delay_seconds) {
            return Err(OperationError::InvalidOperation);
        }
        let result = self
            .runner
            .run(
                Path::new("/usr/bin/systemd-run"),
                &[
                    "--quiet".to_owned(),
                    "--collect".to_owned(),
                    "--unit=vonk-forge-reboot.service".to_owned(),
                    format!("--on-active={delay_seconds}s"),
                    "/usr/bin/systemctl".to_owned(),
                    "reboot".to_owned(),
                ],
            )
            .map_err(|_| OperationError::CommandFailed)?;
        if !result.success {
            return Err(OperationError::CommandFailed);
        }
        Ok(())
    }

    fn verify_artifact(
        &self,
        path: &Path,
        kind: &str,
        expected_digest: &str,
        detached_signature: &str,
    ) -> Result<(), OperationError> {
        let metadata = fs::symlink_metadata(path).map_err(|_| OperationError::InvalidArtifact)?;
        if metadata.file_type().is_symlink()
            || !metadata.is_file()
            || metadata.nlink() != 1
            || metadata.len() == 0
            || metadata.len() > MAX_ARTIFACT_BYTES
            || metadata.mode() & 0o022 != 0
            || self
                .required_owner_uid
                .is_some_and(|uid| metadata.uid() != uid)
        {
            return Err(OperationError::InvalidArtifact);
        }
        let mut file = File::open(path).map_err(|_| OperationError::InvalidArtifact)?;
        let before = file
            .metadata()
            .map_err(|_| OperationError::InvalidArtifact)?;
        let mut digest = Sha256::new();
        let mut consumed = 0_u64;
        let mut buffer = [0_u8; 64 * 1024];
        loop {
            let count = file
                .read(&mut buffer)
                .map_err(|_| OperationError::InvalidArtifact)?;
            if count == 0 {
                break;
            }
            consumed += count as u64;
            if consumed > MAX_ARTIFACT_BYTES {
                return Err(OperationError::InvalidArtifact);
            }
            digest.update(&buffer[..count]);
        }
        let after = file
            .metadata()
            .map_err(|_| OperationError::InvalidArtifact)?;
        if stable_identity(&before) != stable_identity(&after)
            || format!("{:x}", digest.finalize()) != expected_digest
        {
            return Err(OperationError::InvalidArtifact);
        }
        let signature_bytes =
            hex::decode(detached_signature).map_err(|_| OperationError::InvalidArtifact)?;
        signature::UnparsedPublicKey::new(&signature::ED25519, self.release_public_key)
            .verify(
                &artifact_signing_bytes(kind, expected_digest)
                    .map_err(|_| OperationError::InvalidArtifact)?,
                &signature_bytes,
            )
            .map_err(|_| OperationError::InvalidArtifact)
    }

    fn require_directory(&self, path: &Path) -> Result<(), OperationError> {
        let metadata = fs::symlink_metadata(path).map_err(|_| OperationError::UnsafePath)?;
        if metadata.file_type().is_symlink()
            || !metadata.is_dir()
            || metadata.mode() & 0o022 != 0
            || self
                .required_owner_uid
                .is_some_and(|uid| metadata.uid() != uid)
        {
            return Err(OperationError::UnsafePath);
        }
        Ok(())
    }
}

fn stable_identity(metadata: &fs::Metadata) -> (u64, u64, u64, i64, i64) {
    (
        metadata.dev(),
        metadata.ino(),
        metadata.len(),
        metadata.mtime(),
        metadata.ctime(),
    )
}

fn sync_directory(path: &Path) -> Result<(), OperationError> {
    OpenOptions::new().read(true).open(path)?.sync_all()?;
    Ok(())
}

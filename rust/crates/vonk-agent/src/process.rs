use std::{
    fs::File,
    io::{Read, Seek, SeekFrom},
    process::{Command, Stdio},
    time::Duration,
};

use tempfile::tempfile;
use thiserror::Error;
use wait_timeout::ChildExt;

const OUTPUT_LIMIT: u64 = 64 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Program {
    Curl,
    NvidiaSmi,
    Oras,
    Podman,
}

impl Program {
    fn path(self) -> &'static str {
        match self {
            Self::Curl => "/usr/bin/curl",
            Self::NvidiaSmi => "/usr/bin/nvidia-smi",
            Self::Oras => "/usr/bin/oras",
            Self::Podman => "/usr/bin/podman",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProcessOutput {
    pub success: bool,
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
}

#[derive(Debug, Error)]
pub enum ProcessError {
    #[error("approved subprocess failed to start or complete")]
    Io(#[from] std::io::Error),
    #[error("approved subprocess exceeded its deadline")]
    Timeout,
    #[error("approved subprocess output exceeded its limit")]
    OutputLimit,
}

pub trait ProcessRunner {
    fn run(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
    ) -> Result<ProcessOutput, ProcessError>;
}

pub struct SystemProcessRunner;

impl ProcessRunner for SystemProcessRunner {
    fn run(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
    ) -> Result<ProcessOutput, ProcessError> {
        let mut stdout = tempfile()?;
        let mut stderr = tempfile()?;
        let mut child = Command::new(program.path())
            .args(arguments)
            .stdin(Stdio::null())
            .stdout(Stdio::from(stdout.try_clone()?))
            .stderr(Stdio::from(stderr.try_clone()?))
            .env_clear()
            .env("LANG", "C.UTF-8")
            .env("LC_ALL", "C.UTF-8")
            .env("PATH", "/usr/bin:/bin")
            .env("HOME", "/var/lib/vonk-forge/agent")
            .env("XDG_DATA_HOME", "/var/lib/vonk-forge/agent")
            .env("XDG_RUNTIME_DIR", "/run/vonk-forge-agent")
            .env(
                "CONTAINERS_STORAGE_CONF",
                "/etc/vonk-forge/containers-storage.conf",
            )
            .spawn()?;
        let status = match child.wait_timeout(timeout)? {
            Some(status) => status,
            None => {
                child.kill()?;
                child.wait()?;
                return Err(ProcessError::Timeout);
            }
        };
        Ok(ProcessOutput {
            success: status.success(),
            stdout: bounded_read(&mut stdout)?,
            stderr: bounded_read(&mut stderr)?,
        })
    }
}

fn bounded_read(file: &mut File) -> Result<Vec<u8>, ProcessError> {
    if file.metadata()?.len() > OUTPUT_LIMIT {
        return Err(ProcessError::OutputLimit);
    }
    file.seek(SeekFrom::Start(0))?;
    let mut value = Vec::new();
    file.take(OUTPUT_LIMIT + 1).read_to_end(&mut value)?;
    if value.len() as u64 > OUTPUT_LIMIT {
        return Err(ProcessError::OutputLimit);
    }
    Ok(value)
}

use std::{
    collections::BTreeMap,
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
    path::{Component, Path, PathBuf},
    time::Duration,
};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use thiserror::Error;
use url::Url;

use crate::{
    inventory::{available_disk_bytes, available_memory_bytes},
    process::{ProcessError, ProcessRunner, Program},
    workloads::{
        ArgumentValue, Placement, WorkloadError, WorkloadSpec, image_digest, managed_path,
    },
};

#[derive(Debug, Error)]
pub enum OciError {
    #[error("OCI subprocess failed")]
    Process(#[from] ProcessError),
    #[error("workload policy rejected the request")]
    Workload(#[from] WorkloadError),
    #[error("container runtime rejected the request")]
    Runtime,
    #[error("container image digest did not match")]
    ImageDigest,
    #[error("managed artifact content is corrupt")]
    Artifact,
    #[error("managed workload storage failed")]
    Io(#[from] std::io::Error),
    #[error("managed workload metadata is invalid")]
    Json(#[from] serde_json::Error),
    #[error("local disk or memory capacity changed after admission")]
    Capacity,
}

pub struct OciRuntime<'a, R> {
    pub runner: &'a R,
    pub data_root: &'a Path,
    pub huggingface_curl_config: Option<&'a Path>,
}

#[derive(Debug, Serialize)]
struct RuntimeContract<'a> {
    schema_version: u8,
    interface: &'static str,
    installation_id: &'a str,
    run_id: &'a str,
    artifacts: Vec<RuntimeArtifact>,
    endpoint: RuntimeEndpoint<'a>,
    placement: RuntimePlacement<'a>,
}

#[derive(Debug, Serialize)]
struct RuntimeArtifact {
    kind: String,
    repository: String,
    revision: String,
    path: String,
}

#[derive(Debug, Serialize)]
struct RuntimeEndpoint<'a> {
    listen_host: &'static str,
    listen_port: u16,
    protocol: &'a str,
    model_aliases: &'a [String],
    health_path: &'a str,
}

#[derive(Debug, Serialize)]
struct RuntimePlacement<'a> {
    rank: u16,
    role: &'a str,
    world_size: u16,
    local_address: Option<std::net::IpAddr>,
    master_address: Option<std::net::IpAddr>,
    master_port: Option<u16>,
}

impl<R: ProcessRunner> OciRuntime<'_, R> {
    pub fn ensure_disk_available(&self, required_bytes: u64) -> Result<(), OciError> {
        let required = required_bytes
            .checked_add(10_000_000_000)
            .ok_or(OciError::Capacity)?;
        if available_disk_bytes(self.data_root).map_err(|_| OciError::Capacity)? < required {
            return Err(OciError::Capacity);
        }
        Ok(())
    }

    pub fn ensure_memory_available(
        &self,
        required_bytes: u64,
        meminfo_path: &Path,
    ) -> Result<(), OciError> {
        let required = required_bytes
            .checked_add(4_000_000_000)
            .ok_or(OciError::Capacity)?;
        if available_memory_bytes(self.runner, meminfo_path).map_err(|_| OciError::Capacity)?
            < required
        {
            return Err(OciError::Capacity);
        }
        Ok(())
    }

    pub fn install(
        &self,
        spec: &WorkloadSpec,
        installation_id: &str,
        recipe_content_sha256: &str,
    ) -> Result<(), OciError> {
        if recipe_content_sha256.len() != 64
            || !recipe_content_sha256
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        {
            return Err(OciError::Artifact);
        }
        self.verify_image(spec)?;
        let installation = managed_path(self.data_root, "installations", installation_id)?;
        let models = self.data_root.join("models").join("sha256");
        fs::create_dir_all(&models)?;
        fs::create_dir_all(&installation)?;
        fs::set_permissions(&installation, fs::Permissions::from_mode(0o700))?;
        for artifact in &spec.artifacts {
            self.materialize_artifact(&models, artifact)?;
        }
        atomic_write(&installation, "spec.json", &serde_json::to_vec(spec)?)?;
        atomic_write(
            &installation,
            "recipe-content.sha256",
            recipe_content_sha256.as_bytes(),
        )?;
        File::open(&installation)?.sync_all()?;
        Ok(())
    }

    pub fn verify_image(&self, spec: &WorkloadSpec) -> Result<(), OciError> {
        spec.validate()?;
        let pull = self.runner.run(
            Program::Podman,
            &["pull".to_owned(), spec.runtime.image.clone()],
            Duration::from_secs(3600),
        )?;
        if !pull.success {
            return Err(OciError::Runtime);
        }
        let inspect = self.runner.run(
            Program::Podman,
            &[
                "image".to_owned(),
                "inspect".to_owned(),
                "--format".to_owned(),
                "{{.Id}}\\t{{.Os}}\\t{{.Architecture}}\\t{{index .Config.Labels \"ai.vonkforge.runtime-interface\"}}\\t{{.Config.User}}".to_owned(),
                spec.runtime.image.clone(),
            ],
            Duration::from_secs(30),
        )?;
        let expected = format!(
            "sha256:{}",
            image_digest(&spec.runtime.image).ok_or(OciError::ImageDigest)?
        );
        let fields = std::str::from_utf8(&inspect.stdout)
            .ok()
            .map(|value| value.trim_end_matches(['\r', '\n']))
            .map(|value| value.split('\t').collect::<Vec<_>>())
            .unwrap_or_default();
        let root_user = fields
            .get(4)
            .is_some_and(|value| matches!(*value, "" | "0" | "root" | "0:0" | "root:root"));
        if !inspect.success
            || fields.get(..4) != Some(&[expected.as_str(), "linux", "arm64", "v1"])
            || fields.len() != 5
            || !root_user
        {
            return Err(OciError::ImageDigest);
        }
        Ok(())
    }

    pub fn start_arguments(
        &self,
        spec: &WorkloadSpec,
        installation_id: &str,
        run_id: &str,
        placement: &Placement,
    ) -> Result<Vec<String>, OciError> {
        spec.validate()?;
        placement.validate()?;
        managed_path(self.data_root, "installations", installation_id)?;
        let models = self.data_root.join("models");
        let state = managed_path(self.data_root, "runs", run_id)?;
        let mut arguments = vec![
            "run".to_owned(),
            "--detach".to_owned(),
            "--name".to_owned(),
            format!("vonk-{run_id}"),
            "--restart".to_owned(),
            "no".to_owned(),
            "--read-only".to_owned(),
            "--cap-drop=ALL".to_owned(),
            "--security-opt=no-new-privileges".to_owned(),
            "--network".to_owned(),
            "slirp4netns:allow_host_loopback=false".to_owned(),
            "--pids-limit".to_owned(),
            "4096".to_owned(),
            "--memory".to_owned(),
            placement.reserved_memory_bytes.to_string(),
            "--publish".to_owned(),
            format!("{}:{}", placement.port, spec.endpoint.port),
            "--env".to_owned(),
            format!("VONK_RANK={}", placement.rank),
            "--env".to_owned(),
            format!("VONK_WORLD_SIZE={}", placement.world_size),
            "--env".to_owned(),
            "VONK_RUNTIME_SPEC=/run/vonk/runtime.json".to_owned(),
            "--env".to_owned(),
            "VONK_MODEL_ROOT=/models".to_owned(),
            "--env".to_owned(),
            "VONK_STATE_ROOT=/state".to_owned(),
            "--env".to_owned(),
            "VONK_LISTEN_HOST=0.0.0.0".to_owned(),
            "--env".to_owned(),
            format!("VONK_LISTEN_PORT={}", spec.endpoint.port),
        ];
        if let Some(master) = placement.master_address {
            arguments.extend(["--env".to_owned(), format!("VONK_MASTER_ADDR={master}")]);
        }
        if let Some(local) = placement.local_address {
            arguments.extend(["--env".to_owned(), format!("VONK_LOCAL_ADDR={local}")]);
        }
        if let Some(master_port) = placement.master_port {
            arguments.extend([
                "--env".to_owned(),
                format!("VONK_MASTER_PORT={master_port}"),
            ]);
            if placement.rank == 0 {
                arguments.extend([
                    "--publish".to_owned(),
                    format!("{master_port}:{master_port}"),
                ]);
            }
        }
        if !spec.security.devices.is_empty() {
            for device in &spec.security.devices {
                arguments.extend(["--device".to_owned(), device.clone()]);
            }
        }
        for mount in &spec.security.mounts {
            let source = if mount.source == "model" {
                &models
            } else {
                &state
            };
            let mut value = format!("type=bind,src={},dst={}", source.display(), mount.target);
            if mount.read_only {
                value.push_str(",readonly");
            }
            arguments.extend(["--mount".to_owned(), value]);
        }
        arguments.extend([
            "--mount".to_owned(),
            format!(
                "type=bind,src={},dst=/run/vonk/runtime.json,readonly",
                state.join("runtime.json").display()
            ),
        ]);
        arguments.push(spec.runtime.image.clone());
        for argument in &spec.runtime.arguments {
            arguments.push(format!("--{}", argument.name.replace('_', "-")));
            match &argument.value {
                ArgumentValue::Boolean(true) => {}
                ArgumentValue::Boolean(false) => arguments.push("false".to_owned()),
                ArgumentValue::Integer(value) => arguments.push(value.to_string()),
                ArgumentValue::String(value) => arguments.push(value.clone()),
            }
        }
        Ok(arguments)
    }

    pub fn start(
        &self,
        spec: &WorkloadSpec,
        installation_id: &str,
        run_id: &str,
        placement: &Placement,
    ) -> Result<String, OciError> {
        let state = managed_path(self.data_root, "runs", run_id)?;
        fs::create_dir_all(&state)?;
        fs::set_permissions(&state, fs::Permissions::from_mode(0o700))?;
        self.write_runtime_contract(spec, installation_id, run_id, placement)?;
        let arguments = self.start_arguments(spec, installation_id, run_id, placement)?;
        let output = self
            .runner
            .run(Program::Podman, &arguments, Duration::from_secs(300))?;
        let identifier = std::str::from_utf8(&output.stdout)
            .ok()
            .map(str::trim)
            .unwrap_or("");
        if !output.success
            || identifier.len() != 64
            || !identifier.bytes().all(|byte| byte.is_ascii_hexdigit())
        {
            return Err(OciError::Runtime);
        }
        Ok(identifier.to_owned())
    }

    pub fn stop(&self, run_id: &str) -> Result<(), OciError> {
        managed_path(self.data_root, "runs", run_id)?;
        let output = self.runner.run(
            Program::Podman,
            &[
                "stop".to_owned(),
                "--time".to_owned(),
                "30".to_owned(),
                format!("vonk-{run_id}"),
            ],
            Duration::from_secs(45),
        )?;
        if !output.success {
            return Err(OciError::Runtime);
        }
        Ok(())
    }

    pub fn uninstall(&self, installation_id: &str) -> Result<(), OciError> {
        let installation = managed_path(self.data_root, "installations", installation_id)?;
        let metadata = fs::symlink_metadata(&installation)?;
        if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
            return Err(OciError::Artifact);
        }
        self.verify_installation(installation_id)?;
        fs::remove_dir_all(installation)?;
        File::open(self.data_root.join("installations"))?.sync_all()?;
        Ok(())
    }

    pub fn load_spec(&self, installation_id: &str) -> Result<WorkloadSpec, OciError> {
        let path =
            managed_path(self.data_root, "installations", installation_id)?.join("spec.json");
        let metadata = fs::symlink_metadata(&path)?;
        if !metadata.file_type().is_file()
            || metadata.file_type().is_symlink()
            || metadata.len() > 64 * 1024
        {
            return Err(OciError::Artifact);
        }
        let spec: WorkloadSpec = serde_json::from_slice(&fs::read(path)?)?;
        spec.validate()?;
        Ok(spec)
    }

    pub fn verify_installation(&self, installation_id: &str) -> Result<(), OciError> {
        let spec = self.load_spec(installation_id)?;
        let models = self.data_root.join("models").join("sha256");
        for artifact in &spec.artifacts {
            let destination = models.join(artifact_key(artifact)?);
            verify_manifest(&destination)?;
        }
        Ok(())
    }

    pub fn recipe_digest(&self, installation_id: &str) -> Result<String, OciError> {
        let path = managed_path(self.data_root, "installations", installation_id)?
            .join("recipe-content.sha256");
        let value = fs::read_to_string(path)?;
        if value.len() != 64
            || !value
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        {
            return Err(OciError::Artifact);
        }
        Ok(value)
    }

    pub fn installed_bytes(&self, installation_id: &str) -> Result<u64, OciError> {
        let installation = managed_path(self.data_root, "installations", installation_id)?;
        let mut files = BTreeMap::new();
        let mut total = 0;
        visit_files(&installation, &installation, &mut files, &mut total)?;
        let spec = self.load_spec(installation_id)?;
        for artifact in &spec.artifacts {
            let manifest = read_manifest(
                &self
                    .data_root
                    .join("models")
                    .join("sha256")
                    .join(artifact_key(artifact)?),
            )?;
            total = total
                .checked_add(manifest.total_bytes)
                .ok_or(OciError::Artifact)?;
        }
        Ok(total)
    }

    pub fn artifact_set_digest(&self, installation_id: &str) -> Result<String, OciError> {
        let spec = self.load_spec(installation_id)?;
        let mut identities = Vec::with_capacity(spec.artifacts.len());
        for artifact in &spec.artifacts {
            let key = artifact_key(artifact)?;
            let manifest = read_manifest(&self.data_root.join("models").join("sha256").join(&key))?;
            identities.push(serde_json::json!({
                "artifact": artifact,
                "key": key,
                "manifest": manifest,
            }));
        }
        Ok(hex::encode(Sha256::digest(serde_json::to_vec(
            &identities,
        )?)))
    }

    fn write_runtime_contract(
        &self,
        spec: &WorkloadSpec,
        installation_id: &str,
        run_id: &str,
        placement: &Placement,
    ) -> Result<(), OciError> {
        let state = managed_path(self.data_root, "runs", run_id)?;
        let artifacts = spec
            .artifacts
            .iter()
            .map(|artifact| {
                Ok(RuntimeArtifact {
                    kind: artifact.kind.clone(),
                    repository: artifact.repository.clone(),
                    revision: artifact.revision.clone(),
                    path: format!("/models/sha256/{}", artifact_key(artifact)?),
                })
            })
            .collect::<Result<Vec<_>, OciError>>()?;
        let contract = RuntimeContract {
            schema_version: 1,
            interface: "vonk.runtime.v1",
            installation_id,
            run_id,
            artifacts,
            endpoint: RuntimeEndpoint {
                listen_host: "0.0.0.0",
                listen_port: spec.endpoint.port,
                protocol: &spec.endpoint.protocol,
                model_aliases: &spec.endpoint.model_aliases,
                health_path: &spec.endpoint.health_path,
            },
            placement: RuntimePlacement {
                rank: placement.rank,
                role: &placement.role,
                world_size: placement.world_size,
                local_address: placement.local_address,
                master_address: placement.master_address,
                master_port: placement.master_port,
            },
        };
        atomic_write(&state, "runtime.json", &serde_json::to_vec(&contract)?)?;
        Ok(())
    }

    fn materialize_artifact(
        &self,
        models: &Path,
        artifact: &crate::workloads::ArtifactSpec,
    ) -> Result<(), OciError> {
        let key = artifact_key(artifact)?;
        let destination = models.join(&key);
        if destination.exists() {
            return verify_manifest(&destination);
        }
        let staging = models.join(format!(".{key}.{}.staging", std::process::id()));
        fs::create_dir(&staging)?;
        let download = match artifact.kind.as_str() {
            "huggingface.snapshot" => self.download_huggingface(&staging, artifact),
            "http.file" => self.run_download(
                Program::Curl,
                vec![
                    "--fail".to_owned(),
                    "--location".to_owned(),
                    "--proto-redir".to_owned(),
                    "=https".to_owned(),
                    "--proto".to_owned(),
                    "=https".to_owned(),
                    "--tlsv1.3".to_owned(),
                    "--output".to_owned(),
                    staging.join("artifact").display().to_string(),
                    artifact.repository.clone(),
                ],
            ),
            "oci.artifact" => self.run_download(
                Program::Oras,
                vec![
                    "pull".to_owned(),
                    format!("{}@{}", artifact.repository, artifact.revision),
                    "--output".to_owned(),
                    staging.display().to_string(),
                ],
            ),
            _ => Err(OciError::Artifact),
        };
        if download.is_err() {
            let _ = fs::remove_dir_all(&staging);
            return Err(OciError::Artifact);
        }
        let manifest = create_manifest(&staging)?;
        if manifest.total_bytes != artifact.expected_bytes
            || artifact.kind == "http.file"
                && manifest.files.get("artifact").map(String::as_str)
                    != artifact.revision.strip_prefix("sha256:")
        {
            let _ = fs::remove_dir_all(&staging);
            return Err(OciError::Artifact);
        }
        atomic_write(
            &staging,
            ".vonk-manifest.json",
            &serde_json::to_vec(&manifest)?,
        )?;
        fs::rename(&staging, &destination)?;
        File::open(models)?.sync_all()?;
        Ok(())
    }

    fn run_download(&self, program: Program, arguments: Vec<String>) -> Result<(), OciError> {
        let output = self
            .runner
            .run(program, &arguments, Duration::from_secs(3600))?;
        if output.success {
            Ok(())
        } else {
            Err(OciError::Artifact)
        }
    }

    fn download_huggingface(
        &self,
        staging: &Path,
        artifact: &crate::workloads::ArtifactSpec,
    ) -> Result<(), OciError> {
        let repository = huggingface_repository(&artifact.repository)?;
        let mut metadata_url =
            Url::parse("https://huggingface.co/api/models/").map_err(|_| OciError::Artifact)?;
        metadata_url
            .path_segments_mut()
            .map_err(|_| OciError::Artifact)?
            .extend(repository)
            .push("revision")
            .push(&artifact.revision);
        let metadata_path = staging.join(".huggingface-model.json");
        self.run_download(
            Program::Curl,
            self.huggingface_curl_arguments(&metadata_url, &metadata_path)?,
        )?;
        let metadata = fs::read(&metadata_path)?;
        fs::remove_file(&metadata_path)?;
        if metadata.len() > 8 * 1024 * 1024 {
            return Err(OciError::Artifact);
        }
        let model: HuggingFaceModel = serde_json::from_slice(&metadata)?;
        if model.siblings.is_empty() || model.siblings.len() > 20_000 {
            return Err(OciError::Artifact);
        }
        for file in model.siblings {
            let relative = safe_relative_path(&file.rfilename)?;
            let destination = staging.join(&relative);
            if let Some(parent) = destination.parent() {
                fs::create_dir_all(parent)?;
            }
            let mut url = Url::parse("https://huggingface.co/").map_err(|_| OciError::Artifact)?;
            url.path_segments_mut()
                .map_err(|_| OciError::Artifact)?
                .extend(repository.iter().copied())
                .push("resolve")
                .push(&artifact.revision)
                .extend(
                    relative
                        .components()
                        .filter_map(|component| match component {
                            Component::Normal(value) => value.to_str(),
                            _ => None,
                        }),
                );
            url.query_pairs_mut().append_pair("download", "true");
            self.run_download(
                Program::Curl,
                self.huggingface_curl_arguments(&url, &destination)?,
            )?;
            if let Some(lfs) = file.lfs
                && (!lower_hex(&lfs.sha256, 64) || sha256_file(&destination)? != lfs.sha256)
            {
                return Err(OciError::Artifact);
            }
        }
        Ok(())
    }

    fn huggingface_curl_arguments(
        &self,
        url: &Url,
        destination: &Path,
    ) -> Result<Vec<String>, OciError> {
        let mut arguments = curl_arguments(url, destination);
        if let Some(path) = self.huggingface_curl_config {
            let metadata = fs::symlink_metadata(path)?;
            let effective_uid = rustix::process::geteuid().as_raw();
            if !metadata.file_type().is_file()
                || metadata.file_type().is_symlink()
                || metadata.len() > 4096
                || !matches!(metadata.uid(), 0) && metadata.uid() != effective_uid
                || metadata.permissions().mode() & 0o077 != 0
            {
                return Err(OciError::Artifact);
            }
            arguments.splice(0..0, ["--config".to_owned(), path.display().to_string()]);
        }
        Ok(arguments)
    }
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct ArtifactManifest {
    schema_version: u8,
    files: BTreeMap<String, String>,
    total_bytes: u64,
}

#[derive(Debug, Deserialize)]
struct HuggingFaceModel {
    siblings: Vec<HuggingFaceFile>,
}

#[derive(Debug, Deserialize)]
struct HuggingFaceFile {
    rfilename: String,
    lfs: Option<HuggingFaceLfs>,
}

#[derive(Debug, Deserialize)]
struct HuggingFaceLfs {
    sha256: String,
}

fn huggingface_repository(value: &str) -> Result<[&str; 2], OciError> {
    let parts = value.split('/').collect::<Vec<_>>();
    if parts.len() != 2
        || parts.iter().any(|part| {
            part.is_empty()
                || part.len() > 96
                || !part
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
        })
    {
        return Err(OciError::Artifact);
    }
    Ok([parts[0], parts[1]])
}

fn safe_relative_path(value: &str) -> Result<PathBuf, OciError> {
    if value.is_empty()
        || value.len() > 512
        || value.contains('\0')
        || value.contains('\\')
        || value.split('/').count() > 32
    {
        return Err(OciError::Artifact);
    }
    let path = PathBuf::from(value);
    if path
        .components()
        .any(|component| !matches!(component, Component::Normal(_)))
    {
        return Err(OciError::Artifact);
    }
    Ok(path)
}

fn curl_arguments(url: &Url, destination: &Path) -> Vec<String> {
    vec![
        "--fail".to_owned(),
        "--location".to_owned(),
        "--proto".to_owned(),
        "=https".to_owned(),
        "--proto-redir".to_owned(),
        "=https".to_owned(),
        "--tlsv1.3".to_owned(),
        "--connect-timeout".to_owned(),
        "15".to_owned(),
        "--retry".to_owned(),
        "3".to_owned(),
        "--retry-all-errors".to_owned(),
        "--output".to_owned(),
        destination.display().to_string(),
        url.as_str().to_owned(),
    ]
}

fn sha256_file(path: &Path) -> Result<String, OciError> {
    let mut file = File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(hex::encode(hasher.finalize()))
}

fn lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn artifact_key(artifact: &crate::workloads::ArtifactSpec) -> Result<String, OciError> {
    Ok(hex::encode(Sha256::digest(serde_json::to_vec(artifact)?)))
}

fn create_manifest(root: &Path) -> Result<ArtifactManifest, OciError> {
    let mut files = BTreeMap::new();
    let mut total = 0_u64;
    visit_files(root, root, &mut files, &mut total)?;
    if files.is_empty() {
        return Err(OciError::Artifact);
    }
    Ok(ArtifactManifest {
        schema_version: 1,
        files,
        total_bytes: total,
    })
}

fn visit_files(
    root: &Path,
    directory: &Path,
    files: &mut BTreeMap<String, String>,
    total: &mut u64,
) -> Result<(), OciError> {
    let mut entries = fs::read_dir(directory)?.collect::<Result<Vec<_>, _>>()?;
    entries.sort_by_key(fs::DirEntry::file_name);
    for entry in entries {
        let metadata = entry.file_type()?;
        let path = entry.path();
        if metadata.is_symlink() {
            return Err(OciError::Artifact);
        }
        if metadata.is_dir() {
            visit_files(root, &path, files, total)?;
        } else if metadata.is_file() {
            let relative = path.strip_prefix(root).map_err(|_| OciError::Artifact)?;
            let name = relative
                .to_str()
                .ok_or(OciError::Artifact)?
                .replace('\\', "/");
            if name == ".vonk-manifest.json" {
                continue;
            }
            if name.contains("..") {
                return Err(OciError::Artifact);
            }
            let mut file = File::open(&path)?;
            let mut hasher = Sha256::new();
            let mut buffer = [0_u8; 64 * 1024];
            loop {
                let read = file.read(&mut buffer)?;
                if read == 0 {
                    break;
                }
                hasher.update(&buffer[..read]);
                *total = total.checked_add(read as u64).ok_or(OciError::Artifact)?;
            }
            files.insert(name, hex::encode(hasher.finalize()));
        } else {
            return Err(OciError::Artifact);
        }
    }
    Ok(())
}

fn verify_manifest(root: &Path) -> Result<(), OciError> {
    let expected = read_manifest(root)?;
    let observed = create_manifest(root)?;
    if expected.files != observed.files || expected.total_bytes != observed.total_bytes {
        return Err(OciError::Artifact);
    }
    Ok(())
}

fn read_manifest(root: &Path) -> Result<ArtifactManifest, OciError> {
    let raw = fs::read(root.join(".vonk-manifest.json"))?;
    if raw.len() > 64 * 1024 {
        return Err(OciError::Artifact);
    }
    let manifest: ArtifactManifest = serde_json::from_slice(&raw)?;
    if manifest.schema_version != 1 {
        return Err(OciError::Artifact);
    }
    Ok(manifest)
}

fn atomic_write(root: &Path, name: &str, value: &[u8]) -> Result<(), OciError> {
    let temporary: PathBuf = root.join(format!(".{name}.{}.tmp", std::process::id()));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&temporary)?;
    file.write_all(value)?;
    file.sync_all()?;
    fs::rename(temporary, root.join(name))?;
    Ok(())
}

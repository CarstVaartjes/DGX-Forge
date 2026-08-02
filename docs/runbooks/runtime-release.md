# Runtime release deployment

Use `scripts/deploy-runtime-release` from the developer machine to install the
checked Mia adapter release on both DGX Sparks. This operation deploys only the
small, immutable runtime adapter. It does not download model artifacts, pull
container images, start containers, or change the active Cluster Profile.

## Preconditions

- The workload has a complete `[runtime_release]` block.
- Its manifest is a repository-relative regular file.
- Every manifest entry is a regular file below the manifest's parent directory
  and matches its recorded SHA-256.
- Payloads below `bin/` have mode `0755`; all other payloads have mode `0644`.
- `inventory/cluster.toml` contains the hardened SSH aliases for `spark1` and
  `spark2`.
- Host keys have already been accepted through the SSH bootstrap runbook.
- `/opt/spark/model-adapters` exists on both nodes and is writable by the
  controller SSH user. Bootstrap it once with:

  ```bash
  sudo install -d -o root -g root -m 0755 /opt/spark
  sudo install -d -o carst -g carst -m 0755 /opt/spark/model-adapters
  ```

The current release manifest is
`adapters/deepseek/mia-vllm/runtime-manifest.json`. Repository paths such as
`adapters/deepseek/mia-vllm/bin/mia-deepseek-dual` are installed with their
common `adapters/deepseek/mia-vllm/` prefix removed.

## Review the dry run

Dry run is the default and executes no SSH or transfer command:

```bash
scripts/deploy-runtime-release deepseek-agent-dual
```

The JSON plan identifies the exact manifest digest, both SSH aliases, stripped
release paths, and the immutable destination:

```text
/opt/spark/model-adapters/deepseek-agent-dual/releases/<manifest-sha256>/
```

Resolve every local manifest or payload error before applying. Do not bypass a
digest mismatch by editing only the workload pin; regenerate and review the
release manifest after all release files are final.

## Apply to both Sparks

Writing requires the explicit flag:

```bash
scripts/deploy-runtime-release --apply deepseek-agent-dual
```

For each node, the deployment performs these gates in order:

1. Probe the digest-qualified final directory. An exact existing tree is an
   idempotent success; any differing file, directory, symlink, or hash is
   refused.
2. Create a unique temporary directory whose name contains the full manifest
   digest below the final `releases/` directory.
3. Create only manifest-implied subdirectories and transfer only manifest-listed
   payload files.
4. On the node, require the exact file and directory counts, reject symlinks or
   special files, and recompute every payload SHA-256 and expected mode.
5. Atomically rename the verified temporary tree to the immutable final path.

SSH uses batch mode, disables forwarding, requires the configured identity and
strict host-key checking, and never evaluates repository-provided shell text.
The remote scripts receive only locally validated paths and digests as quoted
arguments.

## Failure handling

A differing final directory is never replaced. Preserve it for diagnosis and
compare it with the checked manifest. A failure before the final rename leaves
the final path untouched; the digest-qualified temporary directory may remain
for inspection. No automatic recursive cleanup runs.

Deployment is atomic per node, not across both nodes. If Spark 1 installs and
Spark 2 fails, correct the failure and rerun the same command. Spark 1 will be
recognized as identical and skipped, while Spark 2 resumes through a new safe
temporary directory. Do not create a mutable `current` symlink.

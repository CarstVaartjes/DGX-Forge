#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
validator="$repo_root/nodes/bin/validate-platform-update-root"
test_dir="$(mktemp -d)"
trap 'rm -rf "$test_dir"' EXIT

mkdir -p "$test_dir/bin"

cat > "$test_dir/bin/docker" <<'FAKE'
#!/usr/bin/env bash
printf '%s\n' "$@" > "$DOCKER_ARGS_FILE"
printf 'GPU visible\n'
FAKE

cat > "$test_dir/bin/journalctl" <<'FAKE'
#!/usr/bin/env bash
printf '%s\n' "$JOURNAL_CONTENT"
FAKE

chmod +x "$test_dir/bin/docker" "$test_dir/bin/journalctl"

export PATH="$test_dir/bin:$PATH"
export DOCKER_ARGS_FILE="$test_dir/docker-args"

JOURNAL_CONTENT='normal kernel message' "$validator" > "$test_dir/safe-output"

cat > "$test_dir/expected-docker-args" <<'EXPECTED'
run
--rm
--gpus=all
nvcr.io/nvidia/cuda:13.0.1-devel-ubuntu24.04
nvidia-smi
EXPECTED

cmp "$test_dir/expected-docker-args" "$DOCKER_ARGS_FILE"
grep -Fxq 'PASS: GPU container and current-boot storage checks passed' \
  "$test_dir/safe-output"

if JOURNAL_CONTENT='nvme0: I/O error' "$validator" \
  > "$test_dir/unsafe-output" 2>&1
then
  printf 'validator accepted a kernel storage error\n' >&2
  exit 1
fi

grep -Fq 'nvme0: I/O error' "$test_dir/unsafe-output"
grep -Fq 'FAIL: storage or filesystem error in current boot' \
  "$test_dir/unsafe-output"

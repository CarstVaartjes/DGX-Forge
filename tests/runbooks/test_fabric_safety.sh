#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
runbook="${FABRIC_RUNBOOK:-$repo_root/docs/runbooks/fabric.md}"

grep -Fq 'scp -o ForwardAgent=no nodes/bin/configure-direct-fabric' "$runbook"
grep -Fq 'sudo bash /tmp/configure-direct-fabric --node spark2 --local-postcheck' "$runbook"
grep -Fq 'sudo cat /etc/netplan/99-dgx-spark-direct-fabric.yaml' "$runbook"
grep -Fq 'set -euo pipefail' "$runbook"
grep -Fq "Do not use its generated Netplan, nor run \`discover-sparks\`" "$runbook"

worker_validation_line="$(grep -n 'spark2 --local-postcheck' "$runbook" | head -n1 | cut -d: -f1)"
head_stage_line="$(grep -n 'dgx-spark-1:/tmp/configure-direct-fabric' "$runbook" | head -n1 | cut -d: -f1)"
test "$worker_validation_line" -lt "$head_stage_line"

printf 'fabric runbook safety invariants: PASS\n'

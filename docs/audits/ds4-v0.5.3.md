# DS4 v0.5.3 single-GPU node audit

Audited: 2026-08-03. This is an immutable source and artifact audit, not a
deployment authorization or a command to change either live GPU node.

## Selected serving lane

The `deepseek-agent-single` candidate is DS4 v0.5.3 with the Q2-imatrix base
GGUF and the draft-model drafter. It is a one-GPU node, initially exclusive,
mapped/registered no-copy service. The checked pair is recorded in
[`deepseek-v4-flash-0731-ds4.json`](../../adapters/deepseek/ds4/manifests/deepseek-v4-flash-0731-ds4.json)
and totals `93,691,352,992` bytes.

| Role | Immutable repository and revision | File | Bytes | SHA-256 |
| --- | --- | --- | ---: | --- |
| Base | `antirez/deepseek-v4-gguf` @ `1cd7b564460821938add0475a60b942c409295e0` | `DeepSeek-V4-Flash-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-chat-v2-imatrix-0731.gguf` | 86,720,111,488 | `ca22ae2f838e14077c22bc1c1417b71b45b5e5a3687bd96c2ac6e17fdb6261c0` |
| Drafter | `bleysg/DeepSeek-V4-Flash-draft-model-drafter-GGUF` @ `81c6fdd38f9582da45ba27f0ed7b63bcd3ea3b62` | `draft-model-drafter-Q2K-Q8-0731.gguf` | 6,971,241,504 | `8fa269560dc76fd73e4233ad9b1938b5f65dd363381fd9b1a5c6183f7d12d686` |

The verifier is deliberately offline: `artifact_manifest.py verify --manifest
PATH --root DIRECTORY` rejects unsafe paths, symlinks, and non-regular files;
checks size before digest; and streams each digest in 8 MiB chunks. It reports
both artifacts even if the first does not verify.

## Source audit

| Source | Immutable selection | License | Audit use |
| --- | --- | --- | --- |
| DS4 | `https://github.com/Entrpi/ds4.git`, tag `v0.5.3`, peeled commit `4ad370b4a338efe9723a386673c0e04f6e214108` | MIT | Production source build candidate. |
| DS4-on-GPU node | `https://github.com/Entrpi/ds4-on-node.git@185487ba5749a3c24a71ca81d1bc514c45f10dca`, source archive SHA-256 `7db338d0a441fed36c5e4e7af44ff670e8bfe567e88d482f00ff6a3dc0e5dbe3` | MIT | Audited recipe only; never execute its installer as deployment automation. |

The recipe's installer has mutable-download hazards: it fetches build inputs
and model/runtime content through moving network locations instead of a fully
checked local artifact set. Treat its commands as review evidence only. The
supported build is `make cuda-node` for the pinned v0.5.3 source on the GPU node
CUDA environment (GB10 `sm_121` with the GPU node HBM weight cache); rebuild from
the pinned source and record the resulting binary identity before any future
admission test.

## Runtime and security contract

- Use mapped/registered no-copy startup. Never set `DS4_CUDA_COPY_MODEL` and
  never enable `DS4_MODEL_ANON_HUGE`; either can violate the one-GPU node memory
  contract. Set `DS4_NO_UPDATE_CHECK=1`.
- Keep model files read-only on local NVMe. Put DS4's writable files, logs,
  caches, and disk-backed KV state in explicitly owned writable paths, not in
  the immutable checkpoint directory.
- Disk-backed KV can leave plaintext prompt and generated-token material at
  rest. Its directory therefore needs local access control, lifecycle cleanup,
  and no unreviewed sharing or backup path.
- DS4's HTTP listener has no application authentication. Bind it privately and
  put authenticated access control in front of it before any non-local use.
- The service alias patch remains required so DS4 reports and accepts the
  platform's stable `deepseek` model alias rather than only its internal model
  identity. Patch and record it as a separate, reviewable source delta.

## Deferred MXFP4 lane

MXFP4 is not the selected lane. The available MXFP4 GGUF is
`155,976,458,848` bytes, which exceeds one GPU node's visible memory before KV
cache or runtime overhead. DS4 v0.5.3's loader rejects its GGUF type 39.
MXFP4 remains deferred until both loader support and measured one-GPU node
admission exist.

# Identity verification policy

Run `scripts/verify-vonk-identity --json` before declaring the repository
identity cleanup complete. The command reports deterministic, path-and-line
sorted matches. It exits with status 1 when a match is found in Vonk-owned
content.

The verifier examines names and UTF-8 text in ordinary source files. It skips
repository metadata, ignored sibling worktree checkouts, agent work records,
dependency directories, generalized cache/build directories, virtual
environments, and binary or encoded artifacts
identified by suffix, file signature, NUL bytes, or a high control-byte ratio.
Skipping an artifact also skips its filename, preventing an archive or encoded
blob from causing a false failure.

Only `manifests/`, `inventory/raw/`, and `tests/fixtures/external/` are
external-evidence roots. Matches there remain visible in `external_matches`
but do not fail the command. Do not add other source locations to this list
merely to make the gate pass.

Tests that exercise retired identities must construct the probe value at
runtime so the owned test source remains clean. If a literal is necessary to
preserve provenance, place it under one of the documented external-evidence
roots and make its origin clear in the fixture.

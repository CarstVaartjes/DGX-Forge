# Global catalog import and publication

Local PostgreSQL remains authoritative. The controller does not depend on
vonkforge.ai for startup, readiness, installation, running, or an already
imported recipe.

## Import an immutable public recipe

1. Copy the `vonk://catalog/PUBLISHER/SLUG@sha256:DIGEST` URI from
   vonkforge.ai.
2. In **Recipe catalog**, paste it under **Import from vonkforge.ai**.
3. Review the exact image, artifact revisions, disk, memory, and topology.
4. Choose **Import exact revision**. The controller fetches the immutable
   revision again, verifies its schema and canonical hash, then writes an
   independent resolved revision and provenance rows to local PostgreSQL.

The client uses a fixed HTTPS origin, no ambient proxy credentials, no
redirects, strict timeouts, and a 512 KiB response limit. Set
`DGX_GLOBAL_CATALOG_URL` only to an HTTPS origin; plain HTTP is accepted solely
for an explicit loopback development server.

## Publish without storing global credentials

1. Run the recipe locally on its declared node count and capture a v1 JSON test
   report. It must bind the exact local recipe hash and image digest and record
   successful `container.started`, `endpoint.healthy`, and
   `inference.completed` checks.
2. Open the resolved local recipe and attach that JSON under **Publish through
   vonkforge.ai**. Vonk validates the schema and bindings but labels it
   publisher-submitted evidence, not Vonk certification.
3. Enter the exact publisher namespace you will choose after OAuth. Download
   the publication JSON. Export normalizes only that publisher field and binds
   the exported report to the resulting canonical hash.
4. Open `https://vonkforge.ai/publish`, sign in using a supported OAuth
   provider, choose the same namespace, and upload the JSON.
5. The global service creates a private draft, inspects public ARM64 registry
   metadata, validates the submitted evidence, and requires an explicit final
   publication confirmation.

The export contains exactly `recipe` and `test_report`. It never contains
container layers, model weights, registry credentials, local hostnames, node
inventory, prompts/responses, tailnet details, or OAuth credentials.

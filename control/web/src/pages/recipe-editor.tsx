import {type FormEvent, useEffect, useState} from "react";
import type {CatalogApi, CatalogRecipeDocument, CatalogRecipeRevision} from "../api/types";

type SavedRecipe = Pick<CatalogRecipeRevision, "recipe_id" | "revision_number"> & Partial<CatalogRecipeRevision>;
type EditorApi = Partial<Omit<CatalogApi, "createCatalogRecipe">> & {
  createCatalogRecipe(input: {slug: string; document: CatalogRecipeDocument}): Promise<SavedRecipe>;
};
type Fields = Record<string, string>;
const initial: Fields = {
  publisher: "local", slug: "", title: "", description: "", tags: "", workloadFamily: "custom", capabilities: "openai.chat",
  artifactKind: "huggingface.snapshot", artifactRepository: "", artifactRevision: "", artifactBytes: "", runtimeFamily: "vllm", runtimeImage: "",
  downloadBytes: "1", installedBytes: "1", stagingBytes: "1", residentMemoryBytes: "1", activationMemoryBytes: "1",
  endpointPort: "8000", modelAliases: "model", healthPath: "/v1/models",
};

function nested(value: unknown, path: (string | number)[], fallback = ""): string {
  let current: unknown = value;
  for (const part of path) {
    if (typeof part === "number" && Array.isArray(current)) current = current[part];
    else if (typeof part === "string" && typeof current === "object" && current !== null && part in current) current = (current as Record<string, unknown>)[part];
    else return fallback;
  }
  return current === undefined || current === null ? fallback : String(current);
}
function array(value: unknown, path: (string | number)[]): string {
  const raw = nestedValue(value, path); return Array.isArray(raw) ? raw.map(String).join(", ") : "";
}
function nestedValue(value: unknown, path: (string | number)[]): unknown {
  let current = value;
  for (const part of path) {
    if (typeof part === "number" && Array.isArray(current)) current = current[part];
    else if (typeof part === "string" && typeof current === "object" && current !== null && part in current) current = (current as Record<string, unknown>)[part];
    else return undefined;
  }
  return current;
}
function fromRecipe(recipe: CatalogRecipeRevision): Fields {
  const document = recipe.document;
  return {
    publisher: nested(document, ["identity", "publisher"]), slug: recipe.slug, title: recipe.title, description: recipe.description,
    tags: array(document, ["metadata", "tags"]), workloadFamily: nested(document, ["workload", "family"]), capabilities: array(document, ["workload", "capabilities"]),
    artifactKind: nested(document, ["artifacts", 0, "kind"], "huggingface.snapshot"), artifactRepository: nested(document, ["artifacts", 0, "repository"]), artifactRevision: nested(document, ["artifacts", 0, "revision"]), artifactBytes: nested(document, ["artifacts", 0, "expected_bytes"]),
    runtimeFamily: nested(document, ["runtime", "family"], "vllm"), runtimeImage: nested(document, ["runtime", "image"]),
    downloadBytes: nested(document, ["resources", "per_node", "download_bytes"]), installedBytes: nested(document, ["resources", "per_node", "installed_bytes"]), stagingBytes: nested(document, ["resources", "per_node", "staging_bytes"]), residentMemoryBytes: nested(document, ["resources", "per_node", "resident_memory_bytes"]), activationMemoryBytes: nested(document, ["resources", "per_node", "activation_memory_bytes"]),
    endpointPort: nested(document, ["endpoint", "port"], "8000"), modelAliases: array(document, ["endpoint", "model_aliases"]), healthPath: nested(document, ["endpoint", "health_path"], "/v1/models"),
  };
}
function list(value: string): string[] { return value.split(",").map(item => item.trim()).filter(Boolean); }
function positive(value: string): number { const result = Number(value); if (!Number.isSafeInteger(result) || result < 1) throw new Error("Resource sizes must be positive whole bytes"); return result; }
function documentFrom(fields: Fields): CatalogRecipeDocument {
  return {
    schema_version: 1, identity: {publisher: fields.publisher, slug: fields.slug}, metadata: {title: fields.title, description: fields.description, tags: list(fields.tags)},
    workload: {family: fields.workloadFamily, capabilities: list(fields.capabilities)}, artifacts: [{kind: fields.artifactKind, repository: fields.artifactRepository, revision: fields.artifactRevision, expected_bytes: positive(fields.artifactBytes)}],
    runtime: {interface: "vonk.runtime.v1", family: fields.runtimeFamily, image: fields.runtimeImage, architecture: "linux/arm64", arguments: []},
    resources: {per_node: {download_bytes: positive(fields.downloadBytes), installed_bytes: positive(fields.installedBytes), staging_bytes: positive(fields.stagingBytes), resident_memory_bytes: positive(fields.residentMemoryBytes), activation_memory_bytes: positive(fields.activationMemoryBytes)}, measurement: "declared"},
    topology: {kind: "single", min_nodes: 1, max_nodes: 1, tested_node_counts: [1]}, endpoint: {protocol: "openai", port: positive(fields.endpointPort), model_aliases: list(fields.modelAliases), health_path: fields.healthPath},
    security: {devices: ["nvidia.com/gpu=all"], capabilities: [], host_network: false, privileged: false, mounts: [{source: "model", target: "/models", read_only: true}, {source: "state", target: "/state", read_only: false}]}, provenance: {source_kind: "local", source_reference: null, attribution: []},
  };
}

export function RecipeEditorPage({api, recipeId}: {api: EditorApi; recipeId?: string}) {
  const [fields, setFields] = useState(initial); const [recipe, setRecipe] = useState<CatalogRecipeRevision | null>(null);
  const [message, setMessage] = useState(""); const [error, setError] = useState(""); const [confirmResolve, setConfirmResolve] = useState(false);
  const [targetPublisher, setTargetPublisher] = useState("vonk");
  useEffect(() => {
    if (!recipeId || !api.catalogRecipe) return;
    let active = true; void api.catalogRecipe(recipeId).then(value => { if (active) { setRecipe(value); setFields(fromRecipe(value)); } }).catch(value => { if (active) setError(value instanceof Error ? value.message : "Unable to load recipe"); });
    return () => { active = false; };
  }, [api, recipeId]);
  const set = (name: string, value: string) => setFields(current => ({...current, [name]: value}));
  async function save(event: FormEvent) {
    event.preventDefault(); setError(""); setMessage("");
    try {
      const document = documentFrom(fields);
      const saved = recipe && recipeId && api.updateCatalogRecipe ? await api.updateCatalogRecipe(recipeId, recipe.revision_number, document) : await api.createCatalogRecipe({slug: fields.slug, document});
      if (saved.lifecycle !== undefined) setRecipe(saved as CatalogRecipeRevision);
      setMessage(`Draft saved as revision ${saved.revision_number}`);
    } catch (value) { setError(value instanceof Error ? value.message.slice(0, 256) : "Unable to save recipe"); }
  }
  async function resolve() {
    if (!recipeId || !recipe || !api.resolveCatalogRecipe) return;
    try { const value = await api.resolveCatalogRecipe(recipeId, recipe.revision_number); setRecipe(value); setConfirmResolve(false); setMessage(`Resolved as sha256:${value.content_sha256}`); }
    catch (value) { setError(value instanceof Error ? value.message.slice(0, 256) : "Unable to resolve recipe"); }
  }
  async function attachReport(file: File | undefined) {
    if (!file || !recipeId || !api.attachPublicationReport) return;
    setError("");
    try {
      const value = JSON.parse(await file.text()) as Record<string, unknown>;
      await api.attachPublicationReport(recipeId, value);
      setMessage("Passing local test report attached to this exact recipe revision.");
    } catch (value) { setError(value instanceof Error ? value.message.slice(0, 256) : "Unable to attach test report"); }
  }
  async function downloadExport() {
    if (!recipeId || !recipe || !api.publicationExport) return;
    setError("");
    try {
      const envelope = await api.publicationExport(recipeId, targetPublisher);
      const url = URL.createObjectURL(new Blob([JSON.stringify(envelope, null, 2)], {type: "application/json"}));
      const anchor = document.createElement("a"); anchor.href = url; anchor.download = `${targetPublisher}-${recipe.slug}.json`; anchor.click();
      URL.revokeObjectURL?.(url);
      setMessage("Publication JSON downloaded. Sign in on vonkforge.ai to upload, validate, and publish it.");
    } catch (value) { setError(value instanceof Error ? value.message.slice(0, 256) : "Unable to export recipe"); }
  }
  const capacityFields: [string, string][] = [["downloadBytes", "Download bytes"], ["installedBytes", "Installed bytes"], ["stagingBytes", "Staging bytes"], ["residentMemoryBytes", "Resident memory bytes"], ["activationMemoryBytes", "Activation memory bytes"]];
  return <>
    <div className="page-heading"><div><h2>{recipeId ? "Recipe details" : "Create local recipe"}</h2><p>Describe artifacts, runtime, resources, topology, endpoint, and security as typed data.</p></div></div>
    {error && <p role="alert" tabIndex={-1}>{error}</p>}{message && <p role="status">{message}</p>}
    <form className="recipe-editor" onSubmit={save}>
      <fieldset><legend>Identity and metadata</legend>
        <label>Publisher<input value={fields.publisher} onChange={event => set("publisher", event.target.value)} required pattern="[a-z0-9][a-z0-9-]{1,62}"/></label>
        <label>Recipe slug<input aria-label="Recipe slug" value={fields.slug} onChange={event => set("slug", event.target.value)} required pattern="[a-z0-9][a-z0-9-]{1,62}" disabled={Boolean(recipeId)}/></label>
        <label>Title<input value={fields.title} onChange={event => set("title", event.target.value)} required maxLength={120}/></label>
        <label className="wide">Description<textarea value={fields.description} onChange={event => set("description", event.target.value)} required maxLength={4000}/></label>
        <label className="wide">Tags, comma separated<input value={fields.tags} onChange={event => set("tags", event.target.value)}/></label>
      </fieldset>
      <fieldset><legend>Artifact and workload</legend>
        <label>Workload family<input value={fields.workloadFamily} onChange={event => set("workloadFamily", event.target.value)} required/></label><label>Capabilities<input value={fields.capabilities} onChange={event => set("capabilities", event.target.value)} required/></label>
        <label>Artifact kind<select value={fields.artifactKind} onChange={event => set("artifactKind", event.target.value)}><option value="huggingface.snapshot">Hugging Face snapshot</option><option value="http.file">HTTP file</option><option value="oci.artifact">OCI artifact</option></select></label>
        <label>Artifact repository<input aria-label="Artifact repository" value={fields.artifactRepository} onChange={event => set("artifactRepository", event.target.value)} required/></label><label>Artifact revision<input aria-label="Artifact revision" value={fields.artifactRevision} onChange={event => set("artifactRevision", event.target.value)} required/></label><label>Artifact bytes<input aria-label="Artifact bytes" inputMode="numeric" value={fields.artifactBytes} onChange={event => set("artifactBytes", event.target.value)} required/></label>
      </fieldset>
      <fieldset><legend>Runtime and capacity per node</legend>
        <label>Runtime family<select value={fields.runtimeFamily} onChange={event => set("runtimeFamily", event.target.value)}><option value="vllm">vLLM</option><option value="sglang">SGLang</option><option value="llama.cpp">llama.cpp</option><option value="ds4">DS4</option></select></label><label className="wide">Runtime image digest<input aria-label="Runtime image digest" value={fields.runtimeImage} onChange={event => set("runtimeImage", event.target.value)} required/></label>
        {capacityFields.map(([name, label]) => <label key={name}>{label}<input inputMode="numeric" value={fields[name]} onChange={event => set(name, event.target.value)} required/></label>)}
      </fieldset>
      <fieldset><legend>OpenAI endpoint and enforced security</legend><label>Port<input inputMode="numeric" value={fields.endpointPort} onChange={event => set("endpointPort", event.target.value)} required/></label><label>Model aliases<input value={fields.modelAliases} onChange={event => set("modelAliases", event.target.value)} required/></label><label>Health path<input value={fields.healthPath} onChange={event => set("healthPath", event.target.value)} required/></label><p className="wide">ARM64 container · NVIDIA GPU · unprivileged · no host network · read-only model mount</p></fieldset>
      <div className="actions"><button type="submit">Save draft</button>{recipeId && recipe?.lifecycle !== "resolved" && <button type="button" onClick={() => setConfirmResolve(true)}>Resolve recipe</button>}</div>
    </form>
    {confirmResolve && <section className="confirmation" aria-labelledby="resolve-heading"><h3 id="resolve-heading">Create immutable revision?</h3><p>The current typed document becomes content-addressed and cannot be edited. You can fork it later.</p><button onClick={() => void resolve()}>Confirm immutable revision</button><button onClick={() => setConfirmResolve(false)}>Cancel</button></section>}
    {recipe?.content_sha256 && <p className="digest">Canonical content: sha256:{recipe.content_sha256}</p>}
    {recipe?.lifecycle === "resolved" && <section className="confirmation" aria-labelledby="publish-heading"><h3 id="publish-heading">Publish through vonkforge.ai</h3><p>The NAS stores no global OAuth token. Attach publisher-submitted evidence from an actual local lifecycle and inference test, download the metadata-only envelope, then authenticate in your browser.</p><label>Local test report JSON<input type="file" accept="application/json,.json" onChange={event => void attachReport(event.target.files?.[0])}/></label><label>Target publisher namespace<input value={targetPublisher} pattern="[a-z0-9][a-z0-9-]{1,62}" onChange={event => setTargetPublisher(event.target.value)}/></label><div className="actions"><button type="button" onClick={() => void downloadExport()}>Download publication JSON</button><a className="button" href="https://vonkforge.ai/publish" target="_blank" rel="noreferrer">Open publisher workspace</a></div></section>}
  </>;
}

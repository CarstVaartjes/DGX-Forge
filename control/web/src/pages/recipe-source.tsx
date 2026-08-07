import {useEffect, useState} from "react";
import type {CatalogApi, CatalogRecipeRevision, ControlApi, RecipeBuildPlan, SourcePolicyReport} from "../api/types";

type SourceApi = Pick<CatalogApi, "catalogRecipe" | "checkRecipeSource" | "previewRecipeBuild" | "buildRecipe"> & Pick<ControlApi, "agents">;

export function RecipeSourcePage({api, recipeId}: {api: SourceApi; recipeId: string}) {
  const [recipe, setRecipe] = useState<CatalogRecipeRevision | null>(null);
  const [nodes, setNodes] = useState<Array<{node_id: string; capabilities: string[]; stale: boolean}>>([]);
  const [builder, setBuilder] = useState("");
  const [report, setReport] = useState<SourcePolicyReport | null>(null);
  const [plan, setPlan] = useState<RecipeBuildPlan | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    void Promise.all([api.catalogRecipe(recipeId), api.agents()]).then(([loaded, fleet]) => {
      if (!active) return;
      setRecipe(loaded);
      const builders = fleet.agents.filter(node => !node.stale && node.capabilities.includes("recipe.build.v1"));
      setNodes(builders); setBuilder(builders[0]?.node_id ?? "");
    }).catch(value => { if (active) setError(value instanceof Error ? value.message : "Unable to load build workflow"); });
    return () => { active = false; };
  }, [api, recipeId]);
  async function check() {
    if (!recipe) return;
    setError(""); setPlan(null); setMessage("");
    try { setReport(await api.checkRecipeSource(recipe.id)); }
    catch (value) { setError(value instanceof Error ? value.message.slice(0, 256) : "Unable to check source"); }
  }
  async function preview() {
    if (!recipe || !builder || !report?.passed) return;
    setError("");
    try { setPlan(await api.previewRecipeBuild(recipe.id, builder)); }
    catch (value) { setError(value instanceof Error ? value.message.slice(0, 256) : "Unable to preview build"); }
  }
  async function build() {
    if (!plan) return;
    setError("");
    try { const operation = await api.buildRecipe(plan); setMessage(`Build ${operation.owner_id} queued on ${operation.nodes.join(", ")}. The Spark repeats the source gate before Podman starts.`); setPlan(null); }
    catch (value) { setError(value instanceof Error ? value.message.slice(0, 256) : "Unable to schedule build"); }
  }
  return <>
    <div className="page-heading"><div><h2>Security check &amp; build</h2><p>{recipe?.title ?? "Recipe"}: inspect locally, then send the exact digest to one selected builder Spark.</p></div><div className="actions">{recipe && <a className="button" href={`/api/v1/catalog/source-bundles/${recipe.source_bundle_sha256}`} download>Download source tar</a>}<a className="button" href={`/catalog/${recipeId}`}>Back to recipe</a></div></div>
    {error && <p role="alert">{error}</p>}{message && <p role="status">{message}</p>}
    <section className="confirmation"><h3>Stage 1 — controller source gate</h3><p>Hard failures include unpinned base images, root final users, ADD, escaping COPY paths, secret/SSH mounts, privileged or host-network Compose services, added capabilities, devices, host binds, and container-runtime sockets.</p><button type="button" disabled={!recipe || recipe.lifecycle !== "resolved"} onClick={() => void check()}>Check exact source bundle</button></section>
    {report && <section className="confirmation" aria-live="polite"><h3>{report.passed ? "Source gate passed" : "Source gate blocked the build"}</h3><p><code>sha256:{report.source_bundle_sha256}</code> · <code>{report.dockerfile}</code></p>{report.findings.length === 0 ? <p>No structural policy findings. This is not a claim that the source is benign; the independent, bounded rootless build remains mandatory.</p> : <ul>{report.findings.map((finding, index) => <li key={`${finding.code}-${index}`}><strong>{finding.code}</strong> — {finding.detail}{finding.line ? ` (line ${finding.line})` : ""}</li>)}</ul>}</section>}
    {report?.passed && <section className="confirmation"><h3>Stage 2 — independent Spark gate</h3><label>Builder Spark<select aria-label="Builder Spark" value={builder} onChange={event => { setBuilder(event.target.value); setPlan(null); }}><option value="">Select a capable online Spark</option>{nodes.map(node => <option key={node.node_id} value={node.node_id}>{node.node_id}</option>)}</select></label><p>The agent verifies the canonical tar again and builds rootless with bounded CPU, memory, PIDs, storage, time, and no build network by default.</p><button type="button" disabled={!builder} onClick={() => void preview()}>Preview sealed build input</button></section>}
    {plan && <section className="confirmation"><h3>Confirm build</h3><dl className="evidence-grid compact"><div><dt>Builder</dt><dd><code>{plan.builder_node_id}</code></dd></div><div><dt>Source</dt><dd><code>{plan.source_bundle_sha256}</code></dd></div><div><dt>Build input</dt><dd><code>{plan.build_input_sha256}</code></dd></div></dl><button type="button" onClick={() => void build()}>Send exact source to builder</button></section>}
  </>;
}

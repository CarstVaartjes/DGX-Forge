import {useEffect, useState} from "react";
import type {CatalogApi, CatalogRecipeSummary, GlobalRecipeRevision} from "../api/types";
import {RecipeSummary} from "../components/recipe-summary";

type CatalogListApi = Pick<CatalogApi, "catalogRecipes"> & Partial<Pick<CatalogApi, "previewGlobalRecipe" | "importGlobalRecipe">>;

function object(value: unknown): Record<string, unknown> { return typeof value === "object" && value !== null ? value as Record<string, unknown> : {}; }
function gb(value: unknown): string { return `${(Number(value) / 1_000_000_000).toFixed(1)} GB`; }

export function CatalogPage({api}: {api: CatalogListApi}) {
  const [recipes, setRecipes] = useState<CatalogRecipeSummary[]>([]);
  const [error, setError] = useState("");
  const [uri, setUri] = useState("");
  const [preview, setPreview] = useState<GlobalRecipeRevision | null>(null);
  const [message, setMessage] = useState("");
  useEffect(() => {
    let active = true;
    void api.catalogRecipes().then(result => { if (active) setRecipes(result.recipes); }).catch(value => { if (active) setError(value instanceof Error ? value.message.slice(0, 256) : "Unable to load recipes"); });
    return () => { active = false; };
  }, [api]);
  async function review() {
    if (!api.previewGlobalRecipe) return;
    setError(""); setMessage(""); setPreview(null);
    try { setPreview(await api.previewGlobalRecipe(uri)); }
    catch (value) { setError(value instanceof Error ? value.message.slice(0, 256) : "Unable to review global recipe"); }
  }
  async function importExact() {
    if (!preview || !api.importGlobalRecipe) return;
    setError("");
    try {
      const imported = await api.importGlobalRecipe(uri, preview.content_sha256);
      setRecipes(current => [imported, ...current.filter(item => item.recipe_id !== imported.recipe_id)]);
      setMessage(`${imported.title} is now in local PostgreSQL and available offline.`);
      setPreview(null);
    } catch (value) { setError(value instanceof Error ? value.message.slice(0, 256) : "Unable to import global recipe"); }
  }
  const metadata = object(preview?.document.metadata);
  const perNode = object(object(preview?.document.resources).per_node);
  return <>
    <div className="page-heading"><div><h2>Recipe catalog</h2><p>Local PostgreSQL is authoritative. Recipes remain available when vonkforge.ai or Git is unavailable.</p></div><div className="actions"><a className="button" href="/catalog/import/sparkrun">Import SparkRun</a><a className="button" href="/catalog/new">Create local recipe</a></div></div>
    {error && <p role="alert">{error}</p>}{!error && recipes.length === 0 && <p role="status">No recipes yet.</p>}
    {message && <p role="status">{message}</p>}
    {api.previewGlobalRecipe && <section className="confirmation" aria-labelledby="global-import-heading"><h3 id="global-import-heading">Import from vonkforge.ai</h3><p>Paste the immutable URI from a public recipe. Review its exact container, weights, sizing, and topology before creating a durable local copy.</p><label>Immutable vonkforge.ai URI<input value={uri} onChange={event => setUri(event.target.value)} placeholder={`vonk://catalog/vonk/model@sha256:${"0".repeat(64)}`}/></label><button type="button" onClick={() => void review()}>Review global recipe</button></section>}
    {preview && <section className="confirmation" aria-labelledby="global-review-heading"><h3 id="global-review-heading">Review {String(metadata.title ?? preview.slug)}</h3><p><code>{preview.publisher}/{preview.slug}@sha256:{preview.content_sha256}</code></p><dl className="evidence-grid compact"><div><dt>Global revision</dt><dd>{preview.revision_number}</dd></div><div><dt>Disk</dt><dd>{gb(perNode.installed_bytes)} disk / node</dd></div><div><dt>Memory</dt><dd>{gb(perNode.resident_memory_bytes)} RAM / node</dd></div><div><dt>Topology</dt><dd>{String(object(preview.document.topology).kind ?? "unknown")}</dd></div></dl><button type="button" onClick={() => void importExact()}>Import exact revision</button></section>}
    <div className="recipe-grid">{recipes.map(recipe => <RecipeSummary key={recipe.recipe_id} recipe={recipe}/>)}</div>
  </>;
}

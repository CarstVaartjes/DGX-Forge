import type {CatalogRecipeSummary} from "../api/types";

const originLabels = {local: "Local", sparkrun: "Imported from SparkRun", global: "Downloaded from vonkforge.ai"} as const;

function bytes(value: number): string {
  if (value >= 1_000_000_000) return `${(value / 1_000_000_000).toFixed(1)} GB`;
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)} MB`;
  return `${value} B`;
}

export function RecipeSummary({recipe}: {recipe: CatalogRecipeSummary}) {
  const nodes = recipe.min_nodes === recipe.max_nodes ? `${recipe.min_nodes} node${recipe.min_nodes === 1 ? "" : "s"}` : `${recipe.min_nodes}–${recipe.max_nodes} nodes`;
  return <article className="recipe-card">
    <div className="recipe-card-heading"><div><span className={`origin origin-${recipe.origin}`}>{originLabels[recipe.origin]}</span><h3>{recipe.title}</h3></div><span className="status">{recipe.lifecycle}</span></div>
    <dl className="recipe-facts">
      <div><dt>Runtime</dt><dd>{recipe.runtime_family}</dd></div><div><dt>Topology</dt><dd>{nodes}</dd></div>
      <div><dt>Install</dt><dd>{bytes(recipe.installed_bytes_per_node)} disk / node</dd></div>
      <div><dt>Run</dt><dd>{bytes(recipe.resident_memory_bytes_per_node)} + {bytes(recipe.activation_memory_bytes_per_node)} activation / node</dd></div>
    </dl>
    <p className="digest">{recipe.content_sha256 ? `sha256:${recipe.content_sha256.slice(0, 12)}…` : `Draft revision ${recipe.revision_number}`}</p>
    <a href={`/catalog/${encodeURIComponent(recipe.recipe_id)}`}>Open recipe</a>
  </article>;
}

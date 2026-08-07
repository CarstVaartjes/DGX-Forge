import {useEffect, useMemo, useState} from "react";
import type {CatalogApi, CatalogRecipeRevision, ControlApi, RecipeMappingPlan} from "../api/types";

type MappingApi = Pick<CatalogApi, "catalogRecipe" | "previewRecipeMapping" | "createRecipeMapping"> & Pick<ControlApi, "agents">;
type Profile = {name: string; node_count: number; description?: string};

export function ClusterMappingPage({api, recipeId}: {api: MappingApi; recipeId: string}) {
  const [recipe, setRecipe] = useState<CatalogRecipeRevision | null>(null);
  const [nodes, setNodes] = useState<Array<{node_id: string; stale: boolean; state: string}>>([]);
  const [profileName, setProfileName] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [plan, setPlan] = useState<RecipeMappingPlan | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const profiles = useMemo(() => (Array.isArray(recipe?.document.deployment_profiles) ? recipe?.document.deployment_profiles : []) as Profile[], [recipe]);
  const profile = profiles.find(item => item.name === profileName);
  useEffect(() => {
    let active = true;
    void Promise.all([api.catalogRecipe(recipeId), api.agents()]).then(([loaded, fleet]) => {
      if (!active) return;
      setRecipe(loaded); setNodes(fleet.agents.filter(node => !node.stale && node.state === "active"));
      const available = (Array.isArray(loaded.document.deployment_profiles) ? loaded.document.deployment_profiles : []) as Profile[];
      setProfileName(available[0]?.name ?? "");
    }).catch(value => { if (active) setError(value instanceof Error ? value.message : "Unable to load mapping workflow"); });
    return () => { active = false; };
  }, [api, recipeId]);
  function toggle(nodeId: string) { setPlan(null); setSelected(current => current.includes(nodeId) ? current.filter(value => value !== nodeId) : [...current, nodeId]); }
  async function preview() {
    if (!recipe || !profile) return;
    setError(""); setMessage("");
    try { setPlan(await api.previewRecipeMapping(recipe.id, profile.name, selected)); }
    catch (value) { setError(value instanceof Error ? value.message.slice(0, 256) : "Unable to preview mapping"); }
  }
  async function create() {
    if (!plan) return;
    setError("");
    try { const mapping = await api.createRecipeMapping(plan); setMessage(`Cluster mapping ${mapping.mapping_id} generation ${mapping.generation} created.`); setPlan(null); }
    catch (value) { setError(value instanceof Error ? value.message.slice(0, 256) : "Unable to create mapping"); }
  }
  return <>
    <div className="page-heading"><div><h2>Map recipe to cluster</h2><p>The recipe says what a valid deployment looks like; this mapping chooses the actual GPU nodes and preserves their rank and role.</p></div><a className="button" href={`/catalog/${recipeId}`}>Back to recipe</a></div>
    {error && <p role="alert">{error}</p>}{message && <p role="status">{message}</p>}
    <section className="confirmation"><label>Deployment profile<select value={profileName} onChange={event => { setProfileName(event.target.value); setSelected([]); setPlan(null); }}>{profiles.map(item => <option key={item.name} value={item.name}>{item.name} ({item.node_count} node{item.node_count === 1 ? "" : "s"})</option>)}</select></label><p>{profile?.description}</p><fieldset><legend>Select exactly {profile?.node_count ?? 0} online GPU nodes</legend>{nodes.map(node => <label key={node.node_id}><input type="checkbox" checked={selected.includes(node.node_id)} onChange={() => toggle(node.node_id)}/><code>{node.node_id}</code></label>)}</fieldset><button type="button" disabled={!profile || selected.length !== profile.node_count} onClick={() => void preview()}>Preview ranks, roles, and capacity identity</button></section>
    {plan && <section className="confirmation"><h3>Immutable placement preview</h3><p><code>{plan.placement_digest}</code></p><table><thead><tr><th>Rank</th><th>Role</th><th>GPU node</th><th>Endpoint</th></tr></thead><tbody>{plan.nodes.map(node => <tr key={node.node_id}><td>{node.rank}</td><td>{node.role}</td><td><code>{node.node_id}</code></td><td>{node.endpoint_owner ? "yes" : "no"}</td></tr>)}</tbody></table><button type="button" onClick={() => void create()}>Create cluster mapping</button></section>}
  </>;
}

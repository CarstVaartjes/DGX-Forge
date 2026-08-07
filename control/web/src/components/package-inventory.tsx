import {useEffect, useMemo, useState} from "react";
import type {
  PackageGcProgress,
  PackageGcPreview,
  PackageInventoryEntry,
  PackageInventoryNode,
  PackagePreview,
  PackageApi,
} from "../pages/package-types";

function bytes(value: number | null | undefined): string {
  if (!Number.isFinite(value) || !value || value < 0) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let amount = value;
  let unit = 0;
  while (amount >= 1024 && unit < units.length - 1) { amount /= 1024; unit += 1; }
  return `${amount >= 10 || unit === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[unit]}`;
}

function bounded(value: string | null | undefined): string { return value || "—"; }

function stateLabel(state: string): string {
  return state.replace(/[-_]/g, " ").replace(/\b\w/g, value => value.toUpperCase());
}

function storageUsed(node: PackageInventoryNode): number {
  return node.storage.used_bytes ?? Math.max(0, node.storage.total_bytes - (node.storage.free_bytes ?? node.storage.available_bytes ?? 0));
}

function percent(value: number, total: number): number {
  if (!Number.isFinite(total) || total <= 0) return 0;
  return Math.min(100, Math.max(0, (value / total) * 100));
}

function entryProgress(entry: PackageInventoryEntry): number | undefined {
  if (entry.bytes_complete === undefined || entry.bytes_total === undefined || entry.bytes_total <= 0) return undefined;
  return percent(entry.bytes_complete, entry.bytes_total);
}

type InventoryApi = Pick<PackageApi, "packageInventory" | "previewPackageGc" | "applyPackageGc" | "previewPackageRemoval" | "removePackageInventory">;

/**
 * GPU node-local package inventory and storage safety controls.
 *
 * Acquisition and activation are intentionally represented as different
 * states.  A download can therefore be observed here while an active
 * generation continues serving traffic; the remove controls only accept a
 * server-issued, exact preview digest and never offer an active/leased entry.
 */
export function PackageInventory({api}: {api: InventoryApi}) {
  const [nodes, setNodes] = useState<PackageInventoryNode[]>([]);
  const [error, setError] = useState("");
  const [gcPreview, setGcPreview] = useState<PackageGcPreview>();
  const [gcConfirmation, setGcConfirmation] = useState("");
  const [gcProgress, setGcProgress] = useState<PackageGcProgress>();
  const [selectedRemoval, setSelectedRemoval] = useState<{node: PackageInventoryNode; entry: PackageInventoryEntry}>();
  const [removalPreview, setRemovalPreview] = useState<PackagePreview>();
  const [removalConfirmation, setRemovalConfirmation] = useState("");
  const [removalProgress, setRemovalProgress] = useState<PackageGcProgress>();
  const [busy, setBusy] = useState(false);

  async function refresh() {
    if (!api.packageInventory) return;
    try { const result = await api.packageInventory(); setNodes(Array.isArray(result) ? result : result.nodes); setError(""); }
    catch (value) { setError(value instanceof Error ? value.message : "Unable to load GPU node package inventory"); }
  }
  useEffect(() => { void refresh(); }, [api]);

  const totals = useMemo(() => nodes.reduce((acc, node) => ({
    total: acc.total + node.storage.total_bytes,
    available: acc.available + (node.storage.available_bytes ?? node.storage.free_bytes ?? 0),
    used: acc.used + storageUsed(node),
  }), {total: 0, available: 0, used: 0}), [nodes]);

  async function previewGc() {
    if (!api.previewPackageGc) return;
    setBusy(true);
    try { setGcPreview(await api.previewPackageGc()); setGcConfirmation(""); }
    catch (value) { setError(value instanceof Error ? value.message : "Unable to preview package cleanup"); }
    finally { setBusy(false); }
  }
  async function applyGc() {
    if (!api.applyPackageGc || !gcPreview || gcConfirmation !== gcPreview.digest) return;
    setBusy(true);
    try { setGcProgress(await api.applyPackageGc(gcPreview.digest)); setGcConfirmation(""); await refresh(); }
    catch (value) { setError(value instanceof Error ? value.message : "Unable to apply package cleanup"); }
    finally { setBusy(false); }
  }
  async function previewRemoval(node: PackageInventoryNode, entry: PackageInventoryEntry) {
    if (!api.previewPackageRemoval || entry.active || entry.leased || !["retained", "available", "staged", "failed"].includes(entry.state)) return;
    setBusy(true);
    try { setSelectedRemoval({node, entry}); setRemovalPreview(await api.previewPackageRemoval({deployment_id: entry.deployment_id, release_digest: entry.release_digest, node_ids: [node.node_id]})); setRemovalConfirmation(""); }
    catch (value) { setError(value instanceof Error ? value.message : "Unable to preview package removal"); }
    finally { setBusy(false); }
  }
  async function applyRemoval() {
    if (!api.removePackageInventory || !selectedRemoval || !removalPreview || removalConfirmation !== removalPreview.digest) return;
    setBusy(true);
    try { setRemovalProgress(await api.removePackageInventory(removalPreview.digest)); setRemovalConfirmation(""); await refresh(); }
    catch (value) { setError(value instanceof Error ? value.message : "Unable to remove package"); }
    finally { setBusy(false); }
  }

  if (!api.packageInventory) return <section className="inventory-shell review-card" aria-labelledby="inventory-unavailable-heading">
    <h3 id="inventory-unavailable-heading">GPU node package inventory</h3>
    <p>The control plane is still wiring GPU node inventory telemetry. Downloads and running services remain separate; once telemetry is available this view will show local generations, free space, and safe cleanup previews.</p>
  </section>;

  return <section className="inventory-shell" aria-labelledby="inventory-heading">
    <div className="page-heading inventory-heading"><div><h3 id="inventory-heading">GPU node package inventory</h3><p>Downloaded packages are local and resumable. Removing or collecting a generation never interrupts an active or leased service.</p></div><button type="button" onClick={() => void refresh()} disabled={busy}>Refresh</button></div>
    {error && <p role="alert">{bounded(error)}</p>}
    <div className="inventory-summary" aria-label="Cluster package storage summary">
      <div><span>Used</span><strong>{bytes(totals.used)}</strong></div><div><span>Available</span><strong>{bytes(totals.available)}</strong></div><div><span>GPU node capacity</span><strong>{bytes(totals.total)}</strong></div><div><span>Nodes reporting</span><strong>{nodes.length}</strong></div>
    </div>
    {nodes.length === 0 && <p>No GPU node package inventory is reporting yet.</p>}
    <div className="inventory-grid">{nodes.map(node => {
      const used = storageUsed(node);
      const free = node.storage.free_bytes ?? node.storage.available_bytes ?? Math.max(0, node.storage.total_bytes - used);
      return <article className="inventory-node" key={node.node_id} aria-labelledby={`inventory-node-${node.node_id}`}>
        <div className="inventory-node-heading"><div><h4 id={`inventory-node-${node.node_id}`}>{bounded(node.display_name || node.node_id)}</h4><small>{bounded(node.node_id)} {node.online === false ? "· offline" : "· online"}</small></div><span className={`status ${percent(used, node.storage.total_bytes) > 90 ? "warning" : "good"}`}>{bytes(free)} free</span></div>
        <label className="storage-meter">Disk usage <progress max={node.storage.total_bytes || 1} value={used} aria-label={`${bounded(node.display_name || node.node_id)} disk usage`}/><small>{bytes(used)} of {bytes(node.storage.total_bytes)} used{node.storage.reserved_bytes ? ` · ${bytes(node.storage.reserved_bytes)} reserved` : ""}{node.storage.reclaimable_bytes ? ` · ${bytes(node.storage.reclaimable_bytes)} reclaimable` : ""}</small></label>
        <div className="inventory-packages">{(node.packages ?? []).length === 0 && <p>No package generations installed.</p>}{(node.packages ?? []).map(entry => {
          const progress = entryProgress(entry);
          const protectedEntry = entry.active || entry.leased || !["retained", "available", "staged", "failed"].includes(entry.state);
          return <div className="inventory-package" key={`${node.node_id}:${entry.deployment_id}:${entry.release_digest}:${entry.content_group}`}>
            <div className="inventory-package-heading"><div><strong>{bounded(entry.family_id)}</strong><small>{bounded(entry.deployment_id)} · {bounded(entry.content_group)} · <code>{bounded(entry.release_digest)}</code></small></div><span className={`status ${entry.active ? "good" : ""}`}>{stateLabel(entry.state)}</span></div>
            {progress !== undefined && <label className="download-meter">Download progress <progress max={100} value={progress} aria-label={`${bounded(entry.family_id)} download progress`}/><small>{bytes(entry.bytes_complete)} of {bytes(entry.bytes_total)} · {progress.toFixed(0)}%{entry.bytes_remaining ? ` · ${bytes(entry.bytes_remaining)} remaining` : ""}</small></label>}
            <div className="package-resources"><span>Disk <strong>{bytes(entry.installed_bytes)}</strong></span><span>Run memory <strong>{bytes(entry.resources.host_memory_bytes)}</strong></span><span>Memory split <strong>{bytes(entry.resources.resident_memory_bytes)} resident · {bytes(entry.resources.auxiliary_memory_bytes)} auxiliary · {bytes(entry.resources.activation_memory_bytes)} activation · {bytes(entry.resources.workspace_memory_bytes)} workspace</strong></span><span>GPU memory <strong>{bytes(entry.resources.gpu_memory_bytes)}</strong> ({entry.resources.gpu_count} GPU)</span><span>KV base <strong>{bytes(entry.resources.kv_cache_base_bytes)}</strong></span><span>GPU nodes <strong>{entry.resources.required_nodes}</strong></span></div>
            <div className="inventory-actions">{protectedEntry ? <small>{entry.active ? "Active generation — protected while serving" : entry.leased ? "Leased generation — protected" : "Protected generation"}</small> : api.previewPackageRemoval && api.removePackageInventory ? <button type="button" className="danger" onClick={() => void previewRemoval(node, entry)} disabled={busy}>Preview removal</button> : <small>Removal controls unavailable</small>}</div>
          </div>;
        })}</div>
        {node.observed_at && <small className="inventory-observed">Observed {node.observed_at}</small>}
      </article>;
    })}</div>
    {api.previewPackageGc && <section className="gc-card review-card" aria-labelledby="gc-heading"><h4 id="gc-heading">Reclaim unused package storage</h4><p>Preview reclaimable, non-active generations across the cluster. Nothing is deleted until the exact preview digest is confirmed.</p><button type="button" onClick={() => void previewGc()} disabled={busy}>Preview cleanup</button>{gcPreview && <div className="gc-preview"><p><strong>{bytes(gcPreview.reclaim_bytes)} reclaimable</strong>{gcPreview.storage_bytes ? ` · ${bytes(gcPreview.storage_bytes)} retained storage` : ""}</p><code>{gcPreview.digest}</code><label>Type the exact cleanup preview digest<input value={gcConfirmation} onChange={event => setGcConfirmation(event.target.value)} maxLength={128}/></label><button type="button" className="danger" disabled={busy || !api.applyPackageGc || gcConfirmation !== gcPreview.digest} onClick={() => void applyGc()}>Apply safe cleanup</button></div>}</section>}
    {selectedRemoval && removalPreview && <section className="removal-card review-card" aria-labelledby="removal-heading"><h4 id="removal-heading">Remove {bounded(selectedRemoval.entry.family_id)} from {bounded(selectedRemoval.node.display_name)}</h4><p>This exact preview removes only the selected inactive generation and releases {bytes(selectedRemoval.entry.installed_bytes)}. Active and leased generations cannot be selected.</p><code>{removalPreview.digest}</code><label>Type the exact removal preview digest<input value={removalConfirmation} onChange={event => setRemovalConfirmation(event.target.value)} maxLength={128}/></label><button type="button" className="danger" disabled={busy || !api.removePackageInventory || removalConfirmation !== removalPreview.digest} onClick={() => void applyRemoval()}>Remove exact generation</button></section>}
    {(gcProgress || removalProgress) && <p role="status">Package storage operation: {stateLabel((gcProgress ?? removalProgress)?.state ?? "accepted")}.</p>}
  </section>;
}

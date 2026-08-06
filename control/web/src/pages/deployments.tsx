import {useEffect, useState} from "react";
import type {PackageApi, PackageDeployment, PackagePreview, PackageRollout, PackageRolloutPreview} from "./package-types";

function bounded(value: string | null | undefined): string { return value || "—"; }
function gibibytes(value: number): string { return `${(Math.max(0, value) / 1024 ** 3).toFixed(1)} GiB`; }

export function DeploymentsPage({api}: {api: Pick<PackageApi, "deployments" | "previewPackageRollout" | "startPackageRollout" | "packageRollout" | "previewPackageRollback" | "rollbackPackage">}) {
  const [deployments, setDeployments] = useState<PackageDeployment[]>([]);
  const [selected, setSelected] = useState<PackageDeployment>();
  const [rolloutPreview, setRolloutPreview] = useState<PackageRolloutPreview>();
  const [rollbackPreview, setRollbackPreview] = useState<PackagePreview>();
  const [rolloutConfirmation, setRolloutConfirmation] = useState("");
  const [rollbackConfirmation, setRollbackConfirmation] = useState("");
  const [rollout, setRollout] = useState<PackageRollout>();
  const [error, setError] = useState("");

  useEffect(() => { void api.deployments().then(setDeployments).catch(value => setError(value instanceof Error ? value.message : "Unable to load deployments")); }, [api]);
  async function previewRollout(deployment: PackageDeployment) { setSelected(deployment); setRolloutPreview(await api.previewPackageRollout(deployment.id)); setRolloutConfirmation(""); setRollbackPreview(undefined); }
  async function startRollout() { if (!selected || !rolloutPreview || rolloutConfirmation !== rolloutPreview.digest) return; const started = await api.startPackageRollout(selected.id, rolloutPreview.digest); setRollout(await api.packageRollout(selected.id, started.id)); }
  async function previewRollback(deployment: PackageDeployment) {
    if (!deployment.rollout_id) { setError("The control projection did not provide a retained rollout identity"); return; }
    setSelected(deployment); setRollbackPreview(await api.previewPackageRollback(deployment.id, deployment.rollout_id)); setRollbackConfirmation("");
  }
  async function rollback() { if (!selected || !selected.rollout_id || !rollbackPreview || rollbackConfirmation !== rollbackPreview.digest) return; await api.rollbackPackage(selected.id, selected.rollout_id, rollbackPreview.digest); }

  return <>
    <div className="page-heading"><div><h2>Workload deployments</h2><p>Plan package activation, monitor canaries, and select retained generations for rollback.</p></div></div>
    {error && <p role="alert">{error}</p>}
    <div className="table-scroll"><table aria-label="Workload deployments"><thead><tr><th scope="col">Deployment</th><th scope="col">Family</th><th scope="col">Active release</th><th scope="col">Retained rollback</th><th scope="col">State</th><th scope="col">Actions</th></tr></thead><tbody>
      {deployments.map(deployment => <tr key={deployment.id}><th scope="row">{bounded(deployment.id)}</th><td>{bounded(deployment.family_id)}</td><td><code>{bounded(deployment.release_digest)}</code></td><td><code>{bounded(deployment.previous_release_digest)}</code></td><td>{bounded(deployment.state)}</td><td><button type="button" onClick={() => void previewRollout(deployment)}>Preview rollout for {bounded(deployment.id)}</button>{deployment.previous_release_digest && deployment.rollout_id && <button type="button" onClick={() => void previewRollback(deployment)}>Preview rollback for {bounded(deployment.id)}</button>}</td></tr>)}
    </tbody></table></div>
    {rolloutPreview && <section className="review-card" aria-label="Package rollout preview"><h3>Exact rollout preview</h3><p><code>{rolloutPreview.digest}</code></p><p>{gibibytes(rolloutPreview.download_remaining_bytes)} remaining; {gibibytes(rolloutPreview.storage_required_bytes)} storage required.</p><p>Canary: {rolloutPreview.canary.map(bounded).join(", ") || "—"}</p><p>Offline pending: {rolloutPreview.offline_pending.map(bounded).join(", ") || "None"}</p><label>Type the exact rollout preview digest<input value={rolloutConfirmation} onChange={event => setRolloutConfirmation(event.target.value)} maxLength={512}/></label><button type="button" disabled={rolloutConfirmation !== rolloutPreview.digest} onClick={() => void startRollout()}>Start exact rollout</button></section>}
    {rollout && <section className="review-card" aria-label="Package rollout progress"><h3>Rollout {rollout.id}</h3><p>State: {bounded(rollout.state)}; phase: {bounded(rollout.phase)}</p>{rollout.failure_reason && <p role="alert">{bounded(rollout.failure_reason)}</p>}<ul>{rollout.nodes.map(node => <li key={node.name}>{bounded(node.name)} — {bounded(node.state)}</li>)}</ul></section>}
    {rollbackPreview && <section className="review-card" aria-label="Package rollback preview"><h3>Retained rollback generation</h3><p><code>{rollbackPreview.digest}</code></p><p>Release <code>{rollbackPreview.release_digest ?? "—"}</code></p><label>Type the exact rollback preview digest<input value={rollbackConfirmation} onChange={event => setRollbackConfirmation(event.target.value)} maxLength={512}/></label><button type="button" disabled={rollbackConfirmation !== rollbackPreview.digest} onClick={() => void rollback()}>Roll back exact retained generation</button></section>}
  </>;
}

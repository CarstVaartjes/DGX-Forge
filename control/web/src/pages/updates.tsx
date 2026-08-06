import {useEffect, useState} from "react";
import type {
  ControlApi,
  UpdatePlan,
  UpdateRollout,
  UpdateSkew,
} from "../api/types";

const MAX_TEXT = 256;
const CANONICAL_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function bounded(value: unknown, fallback = "Unavailable"): string {
  if (typeof value !== "string" || value.length === 0) return fallback;
  return value.length > MAX_TEXT ? `${value.slice(0, MAX_TEXT)}…` : value;
}

function errorText(value: unknown): string {
  return bounded(value instanceof Error ? value.message : String(value), "Update request failed");
}

function rolloutLookup(): string | null {
  const value = new URLSearchParams(location.search).get("rollout");
  return value !== null && CANONICAL_UUID.test(value) ? value : null;
}

function persistRollout(id: string): void {
  if (!CANONICAL_UUID.test(id)) return;
  const url = new URL(location.href);
  url.searchParams.set("rollout", id);
  history.replaceState(history.state, "", url);
}

function StringList({items, empty}: {items: string[]; empty: string}) {
  if (items.length === 0) return <p>{empty}</p>;
  return <ul>{items.slice(0, 1024).map(item => <li key={item}><code>{bounded(item)}</code></li>)}</ul>;
}

function PlanReview({plan}: {plan: UpdatePlan}) {
  return <section className="plan-review" aria-label="Update plan review">
    <h3>Exact signed plan</h3>
    <dl className="evidence-grid">
      <div><dt>Plan digest</dt><dd><code>{bounded(plan.plan_digest)}</code></dd></div>
      <div><dt>Target release</dt><dd>{bounded(plan.target.platform_version)}<small><code>{bounded(plan.target.build_digest)}</code></small></dd></div>
      <div><dt>Immutable TUF target</dt><dd><code>{bounded(plan.target.release)}</code><small><code>{bounded(plan.target.target_sha256)}</code></small></dd></div>
      <div><dt>TUF authorization</dt><dd>Targets metadata version {plan.target.tuf_targets_version}</dd></div>
      <div><dt>Canary</dt><dd><code>{bounded(plan.canary_node, "No update required")}</code></dd></div>
      <div><dt>Soak</dt><dd>{plan.soak_seconds} seconds</dd></div>
    </dl>

    <section><h4>Batch order</h4>
      {plan.batches.length === 0
        ? <p>No online Sparks require this release.</p>
        : <ol>{plan.batches.map((batch, index) => <li key={`${index}-${batch.join("-")}`}>
          {index === 0 ? "Canary: " : `Batch ${index + 1}: `}
          {batch.map(node => <code key={node}>{bounded(node)} </code>)}
        </li>)}</ol>}
    </section>

    <section><h4>Affected workloads</h4>
      {plan.workloads.length === 0 ? <p>No distributed workloads are affected.</p> : <ul>{plan.workloads.map(workload =>
        <li key={workload.workload_id}>{bounded(workload.workload_id)} — minimum {workload.minimum_available} available; members {workload.members.map(member => bounded(member)).join(", ")}</li>)}</ul>}
    </section>
    <section><h4>Affected routes</h4><StringList items={plan.affected_routes} empty="No published routes are affected."/></section>
    <section><h4>Offline pending Sparks</h4><StringList items={plan.offline_pending} empty="No Sparks are offline pending."/></section>
    <section><h4>Incompatible Sparks</h4><StringList items={plan.incompatible} empty="No incompatible Sparks."/></section>
    <section><h4>Rollback slots</h4>
      {Object.keys(plan.rollback_slots).length === 0 ? <p>No rollback slots are required.</p> : <ul>{Object.entries(plan.rollback_slots).slice(0, 1024).map(([node, slot]) =>
        <li key={node}><code>{bounded(node)}</code>: {slot ?? "unavailable"}</li>)}</ul>}
    </section>
    <section><h4>Acceptance gates</h4>
      {plan.gates.length === 0 ? <p>No additional gates were reported.</p> : <ul>{plan.gates.map((gate, index) =>
        <li key={`${gate.name}-${index}`}>{bounded(gate.name)} — {bounded(gate.status)}: {bounded(gate.detail)}</li>)}</ul>}
    </section>
  </section>;
}

function RolloutStatus({rollout}: {rollout: UpdateRollout}) {
  return <section className="review-card" aria-label="Update rollout status">
    <h3>Rollout {bounded(rollout.id)}</h3>
    <p>State: <strong>{bounded(rollout.state)}</strong>; current batch {rollout.current_batch + 1}.</p>
    <p>Job: <code>{bounded(rollout.job_id)}</code></p>
    {rollout.failure_reason && <p role="alert">{bounded(rollout.failure_reason)}</p>}
    <div className="table-scroll"><table aria-label="Spark update progress">
      <thead><tr><th scope="col">Spark</th><th scope="col">State</th></tr></thead>
      <tbody>{rollout.nodes.slice(0, 1024).map(node => <tr key={node.node_id}>
        <th scope="row"><code>{bounded(node.node_id)}</code></th><td>{bounded(node.state)}</td>
      </tr>)}</tbody>
    </table></div>
  </section>;
}

export function UpdatesPage({api}: {api: ControlApi}) {
  const [skew, setSkew] = useState<UpdateSkew>();
  const [release, setRelease] = useState("");
  const [plan, setPlan] = useState<UpdatePlan>();
  const [confirmation, setConfirmation] = useState("");
  const [rollbackConfirmation, setRollbackConfirmation] = useState("");
  const [rollout, setRollout] = useState<UpdateRollout>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");

  useEffect(() => {
    let current = true;
    setLoading(true);
    const skewRequest = api.updateSkew().then(result => {
      if (!current) return;
      setSkew(result);
      setRelease(result.target.release);
    }).catch(value => {
      if (current) setError(errorText(value));
    });
    const lookup = rolloutLookup();
    const rolloutRequest = lookup === null
      ? Promise.resolve()
      : api.updateStatus(lookup).then(result => {
        if (current) setRollout(result);
      }).catch(value => {
        if (current) setError(errorText(value));
      });
    void Promise.allSettled([skewRequest, rolloutRequest]).then(() => {
      if (current) setLoading(false);
    });
    return () => { current = false; };
  }, [api]);

  async function preview() {
    setError("");
    setStatus("");
    setPlan(undefined);
    setRollout(undefined);
    setConfirmation("");
    setLoading(true);
    try {
      const result = await api.planUpdate(release);
      setPlan(result);
    } catch (value) {
      setError(errorText(value));
    } finally {
      setLoading(false);
    }
  }

  async function apply() {
    if (!plan || confirmation !== plan.plan_digest || plan.incompatible.length > 0) return;
    setError("");
    setStatus("");
    setLoading(true);
    try {
      const result = await api.applyUpdate(plan.plan_digest);
      setRollout(result);
      setRollbackConfirmation("");
      persistRollout(result.id);
      setStatus(`Exact plan accepted as rollout ${bounded(result.id)}.`);
    } catch (value) {
      setError(errorText(value));
      setPlan(undefined);
      setConfirmation("");
    } finally {
      setLoading(false);
    }
  }

  async function refresh() {
    if (!rollout) return;
    setError("");
    try {
      setRollout(await api.updateStatus(rollout.id));
    } catch (value) {
      setError(errorText(value));
    }
  }

  async function approveRequiredAction() {
    if (!rollout?.required_action) return;
    const action = rollout.required_action;
    if (action === "authorize-rollback" && rollbackConfirmation !== `ROLLBACK ${rollout.id}`) return;
    setError("");
    try {
      setRollout(await api.approveUpdateResume(rollout.id));
      setRollbackConfirmation("");
      setStatus(action === "authorize-rollback"
        ? `Administrator authorized Spark rollback for rollout ${bounded(rollout.id)}.`
        : `Administrator approved rollout ${bounded(rollout.id)} to resume.`);
    } catch (value) {
      setError(errorText(value));
    }
  }

  const applyBlocked = !plan
    || confirmation !== plan.plan_digest
    || plan.incompatible.length > 0
    || loading;

  return <>
    <div className="page-heading"><div><h2>Platform updates</h2><p>Preview and administer signed DGX-Forge updates for Spark agents.</p></div></div>
    {loading && <p role="status">Loading authoritative update state…</p>}
    {error && <p role="alert">{error}</p>}
    {status && <p role="status">{status}</p>}
    {skew && <section className="review-card" aria-label="Platform version skew">
      <h3>NAS control release</h3>
      <dl className="evidence-grid compact">
        <div><dt>Version</dt><dd>{bounded(skew.target.platform_version)}</dd></div>
        <div><dt>Build digest</dt><dd><code>{bounded(skew.target.build_digest)}</code></dd></div>
        <div><dt>Release digest</dt><dd><code>{bounded(skew.target.release_digest)}</code></dd></div>
        <div><dt>TUF target SHA-256</dt><dd><code>{bounded(skew.target.target_sha256)}</code></dd></div>
        <div><dt>Authorization</dt><dd>Targets metadata version {skew.target.tuf_targets_version}</dd></div>
        <div><dt>Skew evidence</dt><dd><code>{bounded(skew.digest)}</code></dd></div>
      </dl>
      <label>Exact immutable TUF target<input value={release} readOnly maxLength={512}/></label>
      <button type="button" disabled={loading || release.length === 0} onClick={() => void preview()}>Preview signed update plan</button>
      <div className="table-scroll"><table aria-label="Spark platform skew">
        <thead><tr><th scope="col">Spark</th><th scope="col">Running identity</th><th scope="col">Slot</th><th scope="col">Compatibility</th><th scope="col">Affected services</th></tr></thead>
        <tbody>{skew.nodes.slice(0, 1024).map(node => <tr key={node.node_id}>
          <th scope="row">{bounded(node.display_name)}<small>{bounded(node.node_id)}</small></th>
          <td>{bounded(node.platform_version)}<small><code>{bounded(node.build_digest)}</code></small></td>
          <td>{node.active_slot ?? "—"}; rollback {node.rollback_slot ?? "unavailable"}</td>
          <td>{bounded(node.status)}<small>{node.reasons.map(reason => bounded(reason)).join(", ") || "compatible"}</small></td>
          <td>{[...node.active_workloads, ...node.active_routes].map(item => bounded(item)).join(", ") || "—"}</td>
        </tr>)}</tbody>
      </table></div>
    </section>}
    {plan && <>
      <PlanReview plan={plan}/>
      {plan.incompatible.length > 0 && <p role="alert">Incompatible Spark skew blocks this update mutation.</p>}
      <label>Type the exact plan digest to confirm<input value={confirmation} onChange={event => setConfirmation(event.target.value)} autoComplete="off" spellCheck={false}/></label>
      <button type="button" disabled={applyBlocked} onClick={() => void apply()}>Apply exact update plan</button>
    </>}
    {rollout && <>
      <RolloutStatus rollout={rollout}/>
      <button type="button" disabled={loading} onClick={() => void refresh()}>Refresh rollout status</button>{" "}
      {rollout.required_action === "authorize-rollback" && <>
        <label>Type <code>ROLLBACK {bounded(rollout.id)}</code> to confirm<input value={rollbackConfirmation} onChange={event => setRollbackConfirmation(event.target.value)} autoComplete="off" spellCheck={false}/></label>
        <button className="danger" type="button" disabled={loading || rollbackConfirmation !== `ROLLBACK ${rollout.id}`} onClick={() => void approveRequiredAction()}>Authorize Spark rollback</button>
      </>}
      {rollout.required_action === "approve-resume" && <button type="button" disabled={loading} onClick={() => void approveRequiredAction()}>Approve rollout resume</button>}
      {rollout.resume_required && rollout.required_action === null && <p>Administrator approval is required to continue this rollout.</p>}
    </>}
  </>;
}

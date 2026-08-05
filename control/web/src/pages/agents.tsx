import {useCallback, useEffect, useId, useState} from "react";
import type {
  AgentSummary,
  ControlApi,
  EnrollmentGrantResponse,
  EnrollmentSummary,
  FleetResponse,
} from "../api/types";
import {EnrollmentReview} from "../components/enrollment-review";

function sameOriginPath(configured: string | undefined, fallback: string): string {
  try {
    const url = new URL(configured || fallback, location.origin);
    if (url.origin !== location.origin) return fallback;
    return `${url.pathname}${url.search}${url.hash}`;
  } catch {
    return fallback;
  }
}

function valueOrDash(value: string | number | null | undefined): string {
  return value === null || value === undefined || value === "" ? "—" : String(value);
}

function CertificateControls({agent, onRevoke}: {agent: AgentSummary; onRevoke(nodeId: string): Promise<void>}) {
  const headingId = useId();
  const [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState(false);

  async function revoke() {
    setBusy(true);
    try {
      await onRevoke(agent.node_id);
    } finally {
      setBusy(false);
    }
  }

  return <section className="certificate-controls" role="region" aria-labelledby={headingId}>
    <h4 id={headingId}>Certificate controls for {agent.node_id}</h4>
    <p role="alert">Revocation immediately disconnects this node and cannot be undone. A new enrollment is required.</p>
    <label>Type {agent.node_id} to confirm certificate revocation
      <input autoComplete="off" value={confirmation} onChange={event => setConfirmation(event.target.value)}/>
    </label>
    <button
      className="danger"
      type="button"
      disabled={confirmation !== agent.node_id || busy}
      onClick={() => void revoke()}
    >Revoke node certificate</button>
  </section>;
}

export function AgentsPage({api}: {api: ControlApi}) {
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [enrollments, setEnrollments] = useState<EnrollmentSummary[]>([]);
  const [fleet, setFleet] = useState<FleetResponse>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [grant, setGrant] = useState<EnrollmentGrantResponse>();
  const [grantNodeId, setGrantNodeId] = useState("");
  const [grantTtl, setGrantTtl] = useState("300");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [agentResult, enrollmentResult, fleetResult] = await Promise.all([
        api.agents(),
        api.enrollments(),
        api.fleet(),
      ]);
      setAgents(agentResult.agents);
      setEnrollments(enrollmentResult.enrollments);
      setFleet(fleetResult);
    } catch (value) {
      setError(value instanceof Error ? value.message : "Unable to load agent data");
    } finally {
      setLoading(false);
    }
  }, [api]);

  useEffect(() => { void load(); }, [load]);

  async function mutate(action: () => Promise<void>) {
    setError("");
    try {
      await action();
    } catch (value) {
      setError(value instanceof Error ? value.message : "Agent operation failed");
    }
  }

  async function createGrant(event: React.FormEvent) {
    event.preventDefault();
    setGrant(undefined);
    await mutate(async () => {
      const created = await api.createEnrollmentGrant(grantNodeId, Number(grantTtl));
      setGrant(created);
      setStatus(`One-time grant created for ${created.node_id}`);
    });
  }

  async function approve(enrollmentId: string) {
    await mutate(async () => {
      const decision = await api.approveEnrollment(enrollmentId);
      setEnrollments(current => current.map(item => item.id === decision.id ? {...item, state: decision.state} : item));
      setStatus(`Enrollment for ${decision.node_id} approved`);
    });
  }

  async function reject(enrollmentId: string, reason: string) {
    await mutate(async () => {
      const decision = await api.rejectEnrollment(enrollmentId, reason);
      setEnrollments(current => current.map(item => item.id === decision.id ? {
        ...item,
        rejection_reason: reason,
        state: decision.state,
      } : item));
      setStatus(`Enrollment for ${decision.node_id} rejected`);
    });
  }

  async function revoke(nodeId: string) {
    await mutate(async () => {
      await api.revokeAgentNode(nodeId);
      setAgents(current => current.map(item => item.node_id === nodeId ? {
        ...item,
        certificate_expires_at: null,
        state: "revoked",
      } : item));
      setStatus(`Certificate for ${nodeId} revoked`);
    });
  }

  function refresh() {
    setGrant(undefined);
    setStatus("");
    void load();
  }

  const compatibility = new Map(fleet?.nodes.map(node => [node.id, node.compatibility]));
  const litellmPath = sameOriginPath(import.meta.env.VITE_LITELLM_ADMIN_PATH, "/ui/");
  const grafanaPath = sameOriginPath(import.meta.env.VITE_GRAFANA_PATH, "/grafana/");

  return <>
    <div className="page-heading">
      <div>
        <h2>Agent enrollment and fleet</h2>
        <p>Review identity evidence, manage node certificates, and inspect bounded agent state.</p>
      </div>
      <button type="button" onClick={refresh}>Refresh agent data</button>
    </div>
    {loading && <p role="status">Loading agent data…</p>}
    {error && <p role="alert">{error}</p>}
    {status && <p role="status">{status}</p>}

    <section aria-labelledby="native-admin-heading" className="native-links">
      <h3 id="native-admin-heading">Native administration surfaces</h3>
      <p><a href={litellmPath}>LiteLLM Admin UI — keys, teams, and spend</a></p>
      <p><a href={grafanaPath}>Grafana — fleet dashboards</a></p>
      <p>These same-origin links remain protected by Caddy. Model definitions remain repository-backed; LiteLLM records do not replace Git authority.</p>
    </section>

    <section aria-labelledby="grant-heading">
      <h3 id="grant-heading">Create enrollment grant</h3>
      <form onSubmit={event => void createGrant(event)}>
        <label>Grant node ID
          <input required value={grantNodeId} onChange={event => setGrantNodeId(event.target.value)}/>
        </label>
        <label>Grant lifetime in seconds
          <input required min="1" max="600" type="number" value={grantTtl} onChange={event => setGrantTtl(event.target.value)}/>
        </label>
        <button type="submit">Create one-time grant</button>
      </form>
      {grant && <div className="grant-secret" role="status" aria-label="One-time enrollment grant">
        <strong>Copy this token now. It will not be shown again.</strong>
        <code>{grant.token}</code>
        <span>Expires at {grant.expires_at}</span>
        <button type="button" onClick={() => setGrant(undefined)}>Dismiss token</button>
      </div>}
    </section>

    <section aria-labelledby="agents-heading">
      <h3 id="agents-heading">Enrolled agents</h3>
      <div className="table-scroll"><table aria-label="Enrolled agents">
        <caption>Current bounded agent and certificate status</caption>
        <thead><tr>
          <th scope="col">Immutable node ID</th>
          <th scope="col">State and version</th>
          <th scope="col">Last seen</th>
          <th scope="col">Certificate expiry</th>
          <th scope="col">Compatibility</th>
          <th scope="col">Capabilities</th>
        </tr></thead>
        <tbody>{agents.map(agent => <tr key={agent.node_id}>
          <th scope="row"><code>{agent.node_id}</code></th>
          <td><span className="status">{agent.state}</span><small>Protocol {valueOrDash(agent.protocol_version)}</small></td>
          <td>{valueOrDash(agent.last_seen_at)}<small>{agent.stale ? "Stale" : `${valueOrDash(agent.last_seen_age_seconds)} seconds ago`}</small></td>
          <td>{valueOrDash(agent.certificate_expires_at)}</td>
          <td>{compatibility.get(agent.node_id) ?? "unknown"}</td>
          <td>{agent.capabilities.length ? agent.capabilities.join(", ") : "—"}</td>
        </tr>)}</tbody>
      </table></div>
      {!loading && agents.length === 0 && <p>No enrolled agents.</p>}
      {agents.map(agent => <CertificateControls key={agent.node_id} agent={agent} onRevoke={revoke}/>)}
    </section>

    <section aria-labelledby="enrollment-heading">
      <h3 id="enrollment-heading">Enrollment evidence</h3>
      {enrollments.map(item => <EnrollmentReview key={item.id} enrollment={item} onApprove={approve} onReject={reject}/>)}
      {!loading && enrollments.length === 0 && <p>No enrollment records.</p>}
    </section>
  </>;
}

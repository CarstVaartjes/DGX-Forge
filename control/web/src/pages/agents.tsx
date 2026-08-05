import {useCallback, useEffect, useId, useRef, useState} from "react";
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

const AGENT_PAGE_SIZE = 20;
const CAPABILITY_PAGE_SIZE = 3;
const MAX_CAPABILITY_LENGTH = 80;

function boundedText(value: string): string {
  return value.length > MAX_CAPABILITY_LENGTH
    ? `${value.slice(0, MAX_CAPABILITY_LENGTH)}…`
    : value;
}

function certificateSnapshot(agent: AgentSummary): string {
  return [agent.node_id, agent.state, agent.certificate_expires_at, agent.protocol_version].join("\u0000");
}

function enrollmentEvidenceSnapshot(enrollment: EnrollmentSummary): string {
  return [
    enrollment.id,
    enrollment.node_id,
    enrollment.state,
    enrollment.host_key_fingerprint,
    enrollment.hardware_fingerprint,
    enrollment.agent_digest,
    enrollment.csr_public_key_fingerprint,
    enrollment.boot_id,
    enrollment.created_at,
    enrollment.certificate_fingerprint,
    enrollment.certificate_serial,
  ].join("\u0000");
}

function Capabilities({agent}: {agent: AgentSummary}) {
  const [page, setPage] = useState(0);
  const pageCount = Math.max(1, Math.ceil(agent.capabilities.length / CAPABILITY_PAGE_SIZE));
  const safePage = Math.min(page, pageCount - 1);
  const start = safePage * CAPABILITY_PAGE_SIZE;
  const visible = agent.capabilities.slice(start, start + CAPABILITY_PAGE_SIZE);
  const end = start + visible.length;

  if (agent.capabilities.length === 0) return <>—</>;

  return <div className="capability-list">
    <span role="status" aria-label={`Capability result count for ${agent.node_id}`}>
      Capabilities {start + 1}–{end} of {agent.capabilities.length}
    </span>
    <span>{visible.map(boundedText).join(", ")}</span>
    {pageCount > 1 && <div className="pagination">
      <button
        type="button"
        aria-label={`Previous capabilities for ${agent.node_id}`}
        disabled={safePage === 0}
        onClick={() => setPage(current => Math.max(0, current - 1))}
      >Previous</button>
      <button
        type="button"
        aria-label={`Next capabilities for ${agent.node_id}`}
        disabled={safePage === pageCount - 1}
        onClick={() => setPage(current => Math.min(pageCount - 1, current + 1))}
      >Next</button>
    </div>}
  </div>;
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
  const [grantPending, setGrantPending] = useState(false);
  const [grantNodeId, setGrantNodeId] = useState("");
  const [grantTtl, setGrantTtl] = useState("300");
  const [agentPage, setAgentPage] = useState(0);
  const [dataRevision, setDataRevision] = useState(0);
  const grantRequest = useRef<{controller: AbortController; id: number} | undefined>(undefined);
  const grantRequestId = useRef(0);

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
      setAgentPage(0);
      setEnrollments(enrollmentResult.enrollments);
      setFleet(fleetResult);
      setDataRevision(current => current + 1);
    } catch (value) {
      setError(value instanceof Error ? value.message : "Unable to load agent data");
    } finally {
      setLoading(false);
    }
  }, [api]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => () => {
    grantRequestId.current += 1;
    grantRequest.current?.controller.abort();
    grantRequest.current = undefined;
  }, []);

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
    if (grantPending || grant) return;
    const controller = new AbortController();
    const requestId = ++grantRequestId.current;
    grantRequest.current = {controller, id: requestId};
    setGrantPending(true);
    setError("");
    setStatus("");
    try {
      const created = await api.createEnrollmentGrant(
        grantNodeId,
        Number(grantTtl),
        controller.signal,
      );
      if (requestId !== grantRequestId.current) return;
      setGrant(created);
      setStatus(`One-time grant created for ${created.node_id}`);
    } catch (value) {
      if (requestId !== grantRequestId.current || controller.signal.aborted) return;
      setError(value instanceof Error ? value.message : "Enrollment grant creation failed");
    } finally {
      if (requestId === grantRequestId.current) {
        grantRequest.current = undefined;
        setGrantPending(false);
      }
    }
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
    grantRequestId.current += 1;
    grantRequest.current?.controller.abort();
    grantRequest.current = undefined;
    setGrantPending(false);
    setGrant(undefined);
    setStatus("");
    void load();
  }

  const compatibility = new Map(fleet?.nodes.map(node => [node.id, node.compatibility]));
  const agentPageCount = Math.max(1, Math.ceil(agents.length / AGENT_PAGE_SIZE));
  const safeAgentPage = Math.min(agentPage, agentPageCount - 1);
  const agentStart = safeAgentPage * AGENT_PAGE_SIZE;
  const visibleAgents = agents.slice(agentStart, agentStart + AGENT_PAGE_SIZE);
  const agentEnd = agentStart + visibleAgents.length;
  const litellmPath = sameOriginPath(import.meta.env.VITE_LITELLM_ADMIN_PATH, "/litellm/ui/");
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
        <button type="submit" disabled={grantPending || Boolean(grant)}>Create one-time grant</button>
      </form>
      {grantPending && <p role="status" aria-label="Enrollment grant request">Creating one-time enrollment grant…</p>}
      {grant && <div className="grant-secret" role="status" aria-label="One-time enrollment grant">
        <strong>Copy this token now. It will not be shown again.</strong>
        <code>{grant.token}</code>
        <span>Expires at {grant.expires_at}</span>
        <button type="button" onClick={() => setGrant(undefined)}>Dismiss token</button>
      </div>}
    </section>

    <section aria-labelledby="agents-heading">
      <h3 id="agents-heading">Enrolled agents</h3>
      <p role="status" aria-label="Agent result count">
        {agents.length === 0
          ? "Showing agents 0 of 0"
          : `Showing agents ${agentStart + 1}–${agentEnd} of ${agents.length}`}
      </p>
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
        <tbody>{visibleAgents.map(agent => <tr key={agent.node_id}>
          <th scope="row"><code>{agent.node_id}</code></th>
          <td><span className="status">{agent.state}</span><small>Protocol {valueOrDash(agent.protocol_version)}</small></td>
          <td>{valueOrDash(agent.last_seen_at)}<small>{agent.stale ? "Stale" : `${valueOrDash(agent.last_seen_age_seconds)} seconds ago`}</small></td>
          <td>{valueOrDash(agent.certificate_expires_at)}</td>
          <td>{compatibility.get(agent.node_id) ?? "unknown"}</td>
          <td><Capabilities agent={agent}/></td>
        </tr>)}</tbody>
      </table></div>
      {agentPageCount > 1 && <div className="pagination">
        <button
          type="button"
          aria-label="Previous agent page"
          disabled={safeAgentPage === 0}
          onClick={() => setAgentPage(current => Math.max(0, current - 1))}
        >Previous agents</button>
        <button
          type="button"
          aria-label="Next agent page"
          disabled={safeAgentPage === agentPageCount - 1}
          onClick={() => setAgentPage(current => Math.min(agentPageCount - 1, current + 1))}
        >Next agents</button>
      </div>}
      {!loading && agents.length === 0 && <p>No enrolled agents.</p>}
      {visibleAgents
        .filter(agent => agent.state !== "revoked" && Boolean(agent.certificate_expires_at))
        .map(agent => <CertificateControls
          key={`${dataRevision}:${certificateSnapshot(agent)}`}
          agent={agent}
          onRevoke={revoke}
        />)}
    </section>

    <section aria-labelledby="enrollment-heading">
      <h3 id="enrollment-heading">Enrollment evidence</h3>
      {enrollments.map(item => <EnrollmentReview
        key={`${dataRevision}:${enrollmentEvidenceSnapshot(item)}`}
        enrollment={item}
        onApprove={approve}
        onReject={reject}
      />)}
      {!loading && enrollments.length === 0 && <p>No enrollment records.</p>}
    </section>
  </>;
}

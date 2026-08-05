import {render, screen, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type {components} from "../api/generated";
import {ApiClient} from "../api/client";
import {App} from "../app";
import {AgentsPage} from "./agents";
import {FleetPage} from "./fleet";

type Agent = components["schemas"]["AgentSummary"];
type Enrollment = components["schemas"]["EnrollmentSummary"];
type Fleet = components["schemas"]["FleetStatusResponse"];

type Assert<T extends true> = T;
type SensitiveEnrollmentFields = Extract<
  keyof Enrollment,
  "address" | "private_key" | "certificate_body" | "certificate_chain" | "csr" | "grant_token"
>;
type EnrollmentContractIsSecretFree = Assert<SensitiveEnrollmentFields extends never ? true : false>;
const enrollmentContractIsSecretFree: EnrollmentContractIsSecretFree = true;

const nodeId = "spk_0123456789abcdef0123456789abcdef";
const enrollmentId = "enrollment-001";
const grantToken = "g".repeat(48);
const leakedListToken = "must-never-render-from-list";
const privateKey = "must-never-render-private-key";
const certificateBody = "must-never-render-certificate-body";

const agent: Agent = {
  capabilities: ["reconciliation", "telemetry"],
  certificate_expires_at: "2026-09-01T12:00:00Z",
  last_seen_age_seconds: 12,
  last_seen_at: "2026-08-05T09:59:48Z",
  node_id: nodeId,
  protocol_version: 4,
  stale: false,
  state: "active",
};

const enrollment: Enrollment = {
  agent_digest: "b".repeat(64),
  boot_id: "boot-001",
  certificate_fingerprint: null,
  certificate_serial: null,
  created_at: "2026-08-05T09:45:00Z",
  csr_public_key_fingerprint: "SHA256:csr-public-key",
  decided_at: null,
  decision_actor: null,
  hardware_fingerprint: "sha256:hardware-evidence",
  host_key_fingerprint: "SHA256:host-key-evidence",
  id: enrollmentId,
  node_id: nodeId,
  rejection_reason: null,
  state: "pending",
};

const fleet: Fleet = {
  commit: "a".repeat(40),
  nodes: [
    {
      agent_last_seen_at: "2026-08-05T09:59:48Z",
      agent_online: true,
      agent_state: "active",
      certificate_expires_at: "2026-09-01T12:00:00Z",
      certificate_expiry_seconds: 2_350_812,
      compatibility: "compatible",
      disk_available_bytes: 2_000_000,
      display_name: "Alpha Spark",
      healthy: true,
      hostname: "not-rendered.internal",
      id: nodeId,
      labels: {zone: "lab-a"},
      last_seen_age_seconds: 12,
      last_seen_at: "2026-08-05T09:59:48Z",
      lifecycle: "managed",
      memory_available_bytes: 1_000_000,
      probe_age_seconds: 4,
      profile: "production",
      stale: false,
    },
  ],
};

type CapturedRequest = {body: unknown; method: string; path: string};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    headers: {"Content-Type": "application/json"},
    status,
  });
}

function installApiFake() {
  const requests: CapturedRequest[] = [];
  const decisions = new Map<string, "approved" | "rejected">();

  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(input, init);
    const url = new URL(request.url);
    const body = request.body ? await request.clone().json() : undefined;
    requests.push({body, method: request.method, path: url.pathname});

    if (request.method === "GET" && url.pathname === "/api/v1/agents") {
      return jsonResponse({agents: [agent]});
    }
    if (request.method === "GET" && url.pathname === "/api/v1/fleet") {
      return jsonResponse(fleet);
    }
    if (request.method === "GET" && url.pathname === "/api/v1/agents/enrollments") {
      return jsonResponse({
        enrollments: [
          {
            ...enrollment,
            state: decisions.get(enrollmentId) ?? enrollment.state,
            address: "10.0.0.44",
            private_key: privateKey,
            certificate_body: certificateBody,
            grant_token: leakedListToken,
          },
        ],
        next_cursor: null,
      });
    }
    if (request.method === "POST" && url.pathname === "/api/v1/agents/enrollments/grants") {
      return jsonResponse(
        {expires_at: "2026-08-05T10:15:00Z", id: "grant-001", node_id: nodeId, token: grantToken},
        201,
      );
    }
    if (request.method === "POST" && url.pathname === `/api/v1/agents/enrollments/${enrollmentId}/approve`) {
      decisions.set(enrollmentId, "approved");
      return jsonResponse({id: enrollmentId, node_id: nodeId, state: "approved"});
    }
    if (request.method === "POST" && url.pathname === `/api/v1/agents/enrollments/${enrollmentId}/reject`) {
      decisions.set(enrollmentId, "rejected");
      return jsonResponse({id: enrollmentId, node_id: nodeId, state: "rejected"});
    }
    if (request.method === "POST" && url.pathname === `/api/v1/agents/nodes/${nodeId}/revoke`) {
      return new Response(null, {status: 204});
    }
    return jsonResponse({detail: "Unexpected test request"}, 404);
  });

  return requests;
}

afterEach(() => {
  history.replaceState(null, "", "/");
  vi.unstubAllGlobals();
});

it("keeps the agents workflow reachable from the keyboard-operable primary navigation", async () => {
  // Break caught: shipping an isolated page without registering its route would strand the workflow.
  installApiFake();
  render(<App api={new ApiClient()}/>);
  const user = userEvent.setup();

  const agentsLink = screen.getByRole("link", {name: "Agents"});
  agentsLink.focus();
  await user.keyboard("{Enter}");

  expect(await screen.findByRole("heading", {name: "Agent enrollment and fleet"})).toBeVisible();
  expect(agentsLink).toHaveAttribute("aria-current", "page");
  expect(location.pathname).toBe("/agents");
});

it("keeps the current fleet page on the generated bounded node status contract", async () => {
  // Break caught: reducing FleetPage to its legacy DTO would hide agent/certificate compatibility state.
  installApiFake();
  render(<FleetPage api={new ApiClient()}/>);

  const table = await screen.findByRole("table", {name: "DGX Spark nodes"});
  const row = within(table).getByRole("row", {name: /Alpha Spark/});
  expect(row).toHaveTextContent(nodeId);
  expect(row).toHaveTextContent("active");
  expect(row).toHaveTextContent("2026-08-05T09:59:48Z");
  expect(row).toHaveTextContent("2026-09-01T12:00:00Z");
  expect(row).toHaveTextContent("compatible");
  expect(row).not.toHaveTextContent("not-rendered.internal");
});

it("keeps fleet and enrollment evidence semantic, bounded, and secret-free", async () => {
  // Break caught: rendering raw API payloads would disclose secret/address fields or omit typed fleet evidence.
  expect(enrollmentContractIsSecretFree).toBe(true);
  installApiFake();
  render(<AgentsPage api={new ApiClient()}/>);

  expect(await screen.findByRole("heading", {name: "Agent enrollment and fleet"})).toBeVisible();
  const agentTable = screen.getByRole("table", {name: "Enrolled agents"});
  expect(within(agentTable).getByRole("columnheader", {name: "Immutable node ID"})).toBeVisible();
  const agentRow = within(agentTable).getByRole("row", {name: new RegExp(nodeId)});
  expect(agentRow).toHaveTextContent("Protocol 4");
  expect(agentRow).toHaveTextContent("compatible");
  expect(agentRow).toHaveTextContent("2026-08-05T09:59:48Z");
  expect(agentRow).toHaveTextContent("2026-09-01T12:00:00Z");

  const review = screen.getByRole("region", {name: `Enrollment evidence for ${nodeId}`});
  expect(review).toHaveTextContent("SHA256:host-key-evidence");
  expect(review).toHaveTextContent("sha256:hardware-evidence");
  expect(review).toHaveTextContent("b".repeat(64));
  expect(review).toHaveTextContent("SHA256:csr-public-key");
  expect(review).toHaveTextContent("2026-08-05T09:45:00Z");
  expect(screen.queryByText(privateKey)).not.toBeInTheDocument();
  expect(screen.queryByText(certificateBody)).not.toBeInTheDocument();
  expect(screen.queryByText(leakedListToken)).not.toBeInTheDocument();
  expect(screen.queryByText("10.0.0.44")).not.toBeInTheDocument();

  const litellm = screen.getByRole("link", {name: /LiteLLM Admin UI.*keys, teams, and spend/i});
  const grafana = screen.getByRole("link", {name: /Grafana.*fleet dashboards/i});
  expect(new URL(litellm.getAttribute("href")!, location.origin).origin).toBe(location.origin);
  expect(new URL(grafana.getAttribute("href")!, location.origin).origin).toBe(location.origin);
  expect(screen.getByText(/Model definitions remain repository-backed/)).toBeVisible();
});

it("shows a grant token only for its creation response and clears it before reload", async () => {
  // Break caught: persisting a grant token in list-derived page state would redisplay a secret after refresh.
  const requests = installApiFake();
  render(<AgentsPage api={new ApiClient()}/>);
  const user = userEvent.setup();

  await screen.findByRole("table", {name: "Enrolled agents"});
  expect(screen.queryByText(grantToken)).not.toBeInTheDocument();
  await user.type(screen.getByLabelText("Grant node ID"), nodeId);
  const lifetime = screen.getByLabelText("Grant lifetime in seconds");
  expect(lifetime).toHaveAttribute("max", "600");
  await user.clear(lifetime);
  await user.type(lifetime, "300");
  await user.click(screen.getByRole("button", {name: "Create one-time grant"}));

  expect(await screen.findByRole("status", {name: "One-time enrollment grant"})).toHaveTextContent(grantToken);
  expect(screen.getAllByText(grantToken)).toHaveLength(1);
  expect(requests.find(request => request.path.endsWith("/grants"))).toEqual({
    body: {node_id: nodeId, ttl_seconds: 300},
    method: "POST",
    path: "/api/v1/agents/enrollments/grants",
  });

  await user.click(screen.getByRole("button", {name: "Refresh agent data"}));
  expect(screen.queryByText(grantToken)).not.toBeInTheDocument();
  expect(screen.queryByText(leakedListToken)).not.toBeInTheDocument();
});

it("requires keyboard-operable evidence confirmation before approval", async () => {
  // Break caught: an enabled approval action could authorize an agent before evidence is compared.
  const requests = installApiFake();
  render(<AgentsPage api={new ApiClient()}/>);
  const user = userEvent.setup();

  const review = await screen.findByRole("region", {name: `Enrollment evidence for ${nodeId}`});
  const approve = within(review).getByRole("button", {name: "Approve enrollment"});
  expect(approve).toBeDisabled();
  const confirmation = within(review).getByRole("checkbox", {name: /compared all fingerprints/i});
  confirmation.focus();
  await user.keyboard(" ");
  expect(confirmation).toBeChecked();
  expect(approve).toBeEnabled();
  approve.focus();
  await user.keyboard("{Enter}");

  expect(await screen.findByRole("status")).toHaveTextContent(`Enrollment for ${nodeId} approved`);
  expect(requests.some(request => request.method === "POST" && request.path.endsWith("/approve"))).toBe(true);
});

it("requires exact typed administrator confirmation for rejection and certificate revocation", async () => {
  // Break caught: destructive controls could act on the wrong node without an irreversible warning and exact ID check.
  const requests = installApiFake();
  render(<AgentsPage api={new ApiClient()}/>);
  const user = userEvent.setup();

  const review = await screen.findByRole("region", {name: `Enrollment evidence for ${nodeId}`});
  expect(within(review).getByRole("alert")).toHaveTextContent(/cannot be undone/i);
  const reject = within(review).getByRole("button", {name: "Reject enrollment"});
  await user.type(within(review).getByLabelText("Rejection reason"), "Inventory evidence does not match");
  await user.type(within(review).getByLabelText(`Type ${nodeId} to confirm rejection`), nodeId.slice(0, -1));
  expect(reject).toBeDisabled();
  await user.type(within(review).getByLabelText(`Type ${nodeId} to confirm rejection`), nodeId.slice(-1));
  expect(reject).toBeEnabled();
  await user.click(reject);
  expect(await screen.findByRole("status")).toHaveTextContent(`Enrollment for ${nodeId} rejected`);

  const revokeRegion = screen.getByRole("region", {name: `Certificate controls for ${nodeId}`});
  expect(within(revokeRegion).getByRole("alert")).toHaveTextContent(/immediately disconnects.*cannot be undone/i);
  const revoke = within(revokeRegion).getByRole("button", {name: "Revoke node certificate"});
  await user.type(within(revokeRegion).getByLabelText(`Type ${nodeId} to confirm certificate revocation`), nodeId);
  expect(revoke).toBeEnabled();
  await user.click(revoke);
  expect(await screen.findByRole("status")).toHaveTextContent(`Certificate for ${nodeId} revoked`);

  expect(requests.find(request => request.path.endsWith("/reject"))?.body).toEqual({
    reason: "Inventory evidence does not match",
  });
  expect(requests.some(request => request.method === "POST" && request.path.endsWith("/revoke"))).toBe(true);
});

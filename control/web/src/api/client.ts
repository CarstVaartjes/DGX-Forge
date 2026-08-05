import createClient from "openapi-fetch";
import type {paths} from "./generated";
import type {
  AgentsResponse,
  AuditSummary,
  ControlApi,
  DocumentList,
  EnrollmentDecisionResponse,
  EnrollmentGrantResponse,
  EnrollmentListResponse,
  FleetResponse,
  JobSummary,
  ProposalInput,
  ProposalPreview,
} from "./types";

function csrfToken(): string | undefined {
  return document.cookie
    .split(";")
    .map(value => value.trim())
    .find(value => value.startsWith("dgx_csrf="))
    ?.split("=", 2)[1];
}

function resultData<T>(result: {data?: T; response: Response}): T {
  if (result.data === undefined) throw new Error(`Control API returned ${result.response.status}`);
  return result.data;
}

export class ApiClient implements ControlApi {
  private readonly generated = createClient<paths>({
    baseUrl: location.origin,
    credentials: "same-origin",
    headers: {Accept: "application/json"},
  });

  constructor() {
    this.generated.use({
      onRequest({request}) {
        if (["GET", "HEAD"].includes(request.method)) return;
        const csrf = csrfToken();
        if (!csrf) return;
        const headers = new Headers(request.headers);
        headers.set("X-CSRF-Token", csrf);
        return new Request(request, {headers});
      },
    });
  }

  async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    if (!path.startsWith("/api/v1/") || path.includes("..")) throw new Error("Unsafe API path");
    const headers = new Headers(init.headers);
    headers.set("Accept", "application/json");
    if (init.body) headers.set("Content-Type", "application/json");
    const csrf = csrfToken();
    if (csrf && init.method && !["GET", "HEAD"].includes(init.method)) headers.set("X-CSRF-Token", csrf);
    const response = await fetch(path, {...init, headers, credentials: "same-origin"});
    if (!response.ok) throw new Error(`Control API returned ${response.status}`);
    return response.json() as Promise<T>;
  }

  async fleet(): Promise<FleetResponse> {
    return resultData(await this.generated.GET("/api/v1/fleet"));
  }

  async agents(): Promise<AgentsResponse> {
    return resultData(await this.generated.GET("/api/v1/agents"));
  }

  async enrollments(): Promise<EnrollmentListResponse> {
    return resultData(await this.generated.GET("/api/v1/agents/enrollments"));
  }

  async createEnrollmentGrant(nodeId: string, ttlSeconds: number): Promise<EnrollmentGrantResponse> {
    return resultData(await this.generated.POST("/api/v1/agents/enrollments/grants", {
      body: {node_id: nodeId, ttl_seconds: ttlSeconds},
    }));
  }

  async approveEnrollment(enrollmentId: string): Promise<EnrollmentDecisionResponse> {
    return resultData(await this.generated.POST("/api/v1/agents/enrollments/{enrollment_id}/approve", {
      params: {path: {enrollment_id: enrollmentId}},
    }));
  }

  async rejectEnrollment(enrollmentId: string, reason: string): Promise<EnrollmentDecisionResponse> {
    return resultData(await this.generated.POST("/api/v1/agents/enrollments/{enrollment_id}/reject", {
      body: {reason},
      params: {path: {enrollment_id: enrollmentId}},
    }));
  }

  async revokeAgentNode(nodeId: string): Promise<void> {
    const {response} = await this.generated.POST("/api/v1/agents/nodes/{node_id}/revoke", {
      params: {path: {node_id: nodeId}},
    });
    if (!response.ok) throw new Error(`Control API returned ${response.status}`);
  }

  documents(kind: "models" | "profiles") { return this.request<DocumentList>(`/api/v1/documents?kind=${kind}`); }
  jobs() { return this.request<{jobs: JobSummary[]}>("/api/v1/jobs"); }
  audit() { return this.request<{events: AuditSummary[]}>("/api/v1/audit"); }
  preview(input: ProposalInput) { return this.request<ProposalPreview>("/api/v1/proposals", {method: "POST", body: JSON.stringify(input)}); }
  submit(digest: string) { return this.request<Record<string, unknown>>("/api/v1/changes", {method: "POST", body: JSON.stringify({proposal_digest: digest})}); }
  reconcile(digest: string) { return this.request<Record<string, unknown>>("/api/v1/reconciliations", {method: "POST", body: JSON.stringify({plan_digest: digest})}); }
}

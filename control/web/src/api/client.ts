import type {ControlApi, DocumentList, FleetResponse, ProposalInput, ProposalPreview, JobSummary, AuditSummary} from "./types";
function csrfToken(): string | undefined { return document.cookie.split(";").map(v => v.trim()).find(v => v.startsWith("dgx_csrf="))?.split("=", 2)[1]; }
export class ApiClient implements ControlApi {
  async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    if (!path.startsWith("/api/v1/") || path.includes("..")) throw new Error("Unsafe API path");
    const headers = new Headers(init.headers); headers.set("Accept", "application/json");
    if (init.body) headers.set("Content-Type", "application/json");
    const csrf = csrfToken(); if (csrf && init.method && !["GET", "HEAD"].includes(init.method)) headers.set("X-CSRF-Token", csrf);
    const response = await fetch(path, {...init, headers, credentials: "same-origin"});
    if (!response.ok) throw new Error(`Control API returned ${response.status}`);
    return response.json() as Promise<T>;
  }
  fleet() { return this.request<FleetResponse>("/api/v1/fleet"); }
  documents(kind: "models" | "profiles") { return this.request<DocumentList>(`/api/v1/documents?kind=${kind}`); }
  jobs() { return this.request<{jobs: JobSummary[]}>("/api/v1/jobs"); }
  audit() { return this.request<{events: AuditSummary[]}>("/api/v1/audit"); }
  preview(input: ProposalInput) { return this.request<ProposalPreview>("/api/v1/proposals", {method: "POST", body: JSON.stringify(input)}); }
  submit(digest: string) { return this.request<Record<string, unknown>>("/api/v1/changes", {method: "POST", body: JSON.stringify({proposal_digest: digest})}); }
  reconcile(digest: string) { return this.request<Record<string, unknown>>("/api/v1/reconciliations", {method: "POST", body: JSON.stringify({plan_digest: digest})}); }
}

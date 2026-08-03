export type NodeSummary = {id: string; display_name: string; lifecycle: string; healthy?: boolean; profile?: string};
export type FleetResponse = {commit?: string; nodes: NodeSummary[]};
export type DocumentList = {commit: string; documents: string[]};
export type ProposalInput = {base_commit: string; changes: {path: string; document: Record<string, unknown>}[]};
export type ProposalPreview = {base_commit: string; digest: string; patch: string; affected_documents: string[]; validation_results: string[]};
export type JobSummary = {id: string; kind: string; state: string};
export type AuditSummary = {request_id: string; actor: string; action: string; base_commit?: string; targets: string[]};
export interface ControlApi {
  fleet(): Promise<FleetResponse>; documents(kind: "models" | "profiles"): Promise<DocumentList>;
  jobs(): Promise<{jobs: JobSummary[]}>; audit(): Promise<{events: AuditSummary[]}>;
  preview(input: ProposalInput): Promise<ProposalPreview>; submit(digest: string): Promise<Record<string, unknown>>;
  reconcile(digest: string): Promise<Record<string, unknown>>;
}

import type {components} from "./generated";

export type NodeSummary = components["schemas"]["NodeStatus"];
export type FleetResponse = components["schemas"]["FleetStatusResponse"];
export type AgentSummary = components["schemas"]["AgentSummary"];
export type AgentsResponse = components["schemas"]["AgentsResponse"];
export type EnrollmentSummary = components["schemas"]["EnrollmentSummary"];
export type EnrollmentListResponse = components["schemas"]["EnrollmentListResponse"];
export type EnrollmentGrantResponse = components["schemas"]["EnrollmentGrantResponse"];
export type EnrollmentDecisionResponse = components["schemas"]["EnrollmentDecisionResponse"];
export type JobDetail = components["schemas"]["JobDetailResponse"];
export type JobResumeResponse = components["schemas"]["JobResumeResponse"];
export type JobSummary = components["schemas"]["JobSummary"];
export type JobsResponse = components["schemas"]["JobsResponse"];
export type ReconciliationAccepted = components["schemas"]["ReconciliationAcceptedResponse"];
export type ReconciliationPlan = components["schemas"]["ReconciliationPlanResponse"];
export type DocumentList = {commit: string; documents: string[]};
export type ProposalInput = {base_commit: string; changes: {path: string; document: Record<string, unknown>}[]};
export type ProposalPreview = {base_commit: string; digest: string; patch: string; affected_documents: string[]; validation_results: string[]};
export type AuditSummary = {request_id: string; actor: string; action: string; base_commit?: string; targets: string[]};
export interface ControlApi {
  fleet(): Promise<FleetResponse>; documents(kind: "models" | "profiles"): Promise<DocumentList>;
  jobs(): Promise<JobsResponse>; job(jobId: string): Promise<JobDetail>;
  resumeJob(jobId: string): Promise<JobResumeResponse>;
  audit(): Promise<{events: AuditSummary[]}>;
  preview(input: ProposalInput): Promise<ProposalPreview>; submit(digest: string): Promise<Record<string, unknown>>;
  planProfile(profileId: string): Promise<ReconciliationPlan>;
  applyReconciliation(digest: string): Promise<ReconciliationAccepted>;
  agents(): Promise<AgentsResponse>;
  enrollments(): Promise<EnrollmentListResponse>;
  createEnrollmentGrant(nodeId: string, ttlSeconds: number, signal?: AbortSignal): Promise<EnrollmentGrantResponse>;
  approveEnrollment(enrollmentId: string): Promise<EnrollmentDecisionResponse>;
  rejectEnrollment(enrollmentId: string, reason: string): Promise<EnrollmentDecisionResponse>;
  revokeAgentNode(nodeId: string): Promise<void>;
}

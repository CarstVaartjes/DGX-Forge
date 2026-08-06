/**
 * W16's narrow view contract.  The generated ControlApi should implement this
 * once W15 publishes the package route declarations; pages deliberately do
 * not construct paths or inspect raw source metadata themselves.
 */
export type PackageFamily = {
  id: string;
  /** Legacy projection fields remain accepted while W16 migrates to OpenAPI. */
  channel?: string | null;
  channels?: string[];
  promotion_mode?: string;
  candidate_count?: number;
  deployment_count?: number;
};

export type PackageCandidateSummary = {
  id: string;
  family_id: string | null;
  channel: string | null;
  provider?: string | null;
  state: string;
  reason_code?: string | null;
  upstream_version: string;
  updated_at?: string | null;
};

export type PackageCandidate = PackageCandidateSummary & {
  lock?: null | {digest: string; components: string[]; dependencies: string[]; provenance: string};
  compatibility?: {compatible: string[]; incompatible_count: number};
  validations?: {backend: string; state: string; reason_code: string | null}[];
  audit?: {action: string; request_id: string}[];
};

export type PackagePreview = {
  digest: string;
  candidate_id?: string;
  validation_id?: string;
  release_digest?: string;
  expires_at?: string;
  diff?: string;
};

export type PackageDeployment = {
  id: string;
  family_id: string;
  release_digest: string;
  previous_release_digest: string | null;
  state: string;
  /** Present when the control projection can identify the retained rollout. */
  rollout_id?: string | null;
};

export type PackageRolloutPreview = PackagePreview & {
  canary: string[];
  batches: string[][];
  offline_pending: string[];
  download_remaining_bytes: number;
  storage_required_bytes: number;
  resource_envelope?: {
    per_node: PackageResourceValues;
    aggregate: PackageResourceValues;
    required_sparks: number;
    topology: string;
    measurement: string;
    evidence: {kind: string; digest: string}[];
  };
};

export type PackageResourceValues = {
  download_bytes: number;
  installed_bytes: number;
  transient_bytes: number;
  output_bytes: number;
  host_memory_bytes: number;
  gpu_memory_bytes: number;
  kv_cache_base_bytes: number;
  kv_cache_per_token_bytes: number;
};

export type PackageRollout = {
  id: string;
  state: string;
  phase: string;
  failure_reason: string | null;
  nodes: {name: string; state: string}[];
};

/**
 * Read-only projection of the package store on each Spark.  The projection is
 * deliberately separate from deployment state: a package can be downloaded
 * (and useful for a future activation) without being the process currently
 * serving traffic.
 */
export type PackageInventoryEntry = {
  deployment_id: string;
  family_id: string;
  release_digest: string;
  content_group: string;
  state: "downloading" | "staged" | "available" | "active" | "retained" | "leased" | "failed" | string;
  bytes_total: number;
  bytes_complete: number;
  bytes_remaining: number;
  installed_bytes: number;
  reclaimable_bytes: number;
  reserved_bytes: number;
  active: boolean;
  retained: boolean;
  leased: boolean;
  operation_id?: string | null;
  last_operation_state?: string | null;
  last_operation_error?: string | null;
  resources: {
    download_bytes: number;
    installed_bytes: number;
    transient_bytes: number;
    output_bytes: number;
    host_memory_bytes: number;
    gpu_memory_bytes: number;
    kv_cache_base_bytes: number;
    kv_cache_per_token_bytes: number;
    required_sparks: number;
    topology: string;
  };
};

export type PackageInventoryNode = {
  node_id: string;
  display_name?: string;
  storage: {
    total_bytes: number;
    available_bytes?: number;
    used_bytes: number;
    free_bytes?: number;
    reserved_bytes: number;
    reclaimable_bytes?: number;
  };
  packages?: PackageInventoryEntry[];
  online?: boolean;
  current_generation?: string | null;
  resources?: {
    host_memory_total_bytes: number;
    host_memory_free_bytes: number;
    gpu_memory_total_bytes: number;
    gpu_memory_free_bytes: number;
    gpu_count: number;
  };
  observed_at?: string | null;
};

export type PackageGcPreview = PackagePreview & {
  reclaim_bytes?: number;
  storage_bytes?: number;
  nodes?: Array<{node_id: string; state?: string; reclaimable_bytes?: number; blocked_reason?: string | null}>;
  blocked_nodes?: string[];
};

export type PackageGcProgress = {
  id: string;
  state: string;
  phase?: string;
  failure_reason?: string | null;
  nodes?: Array<{name?: string; node_id?: string; state: string}>;
};

export type PackageValidationProgress = {
  id: string;
  state: string;
  plan_digest?: string;
  progress?: {completed: number; failed: number; running: number; total: number};
  failure?: string | null;
  job_id?: string | null;
  nodes?: Array<{node_id: string; state: string; batch_index: number; completed: number; total: number}>;
};

export interface PackageApi {
  packageFamilies(): Promise<PackageFamily[]>;
  packageCandidates(): Promise<PackageCandidateSummary[]>;
  packageCandidate(candidateId: string): Promise<PackageCandidate>;
  previewPackageValidation?(candidateId: string): Promise<PackagePreview>;
  validatePackage?(candidateId: string, previewDigest: string): Promise<PackageValidationProgress>;
  packageValidation?(validationId: string): Promise<PackageValidationProgress>;
  previewPackagePromotion(candidateId: string): Promise<PackagePreview>;
  promotePackage(candidateId: string, previewDigest: string): Promise<{release_digest: string}>;
  deployments(): Promise<PackageDeployment[]>;
  previewPackageRollout(deploymentId: string): Promise<PackageRolloutPreview>;
  startPackageRollout(deploymentId: string, previewDigest: string): Promise<{id: string; plan_digest: string}>;
  packageRollout(deploymentId: string, rolloutId: string): Promise<PackageRollout>;
  previewPackageRollback(deploymentId: string, rolloutId: string): Promise<PackagePreview>;
  rollbackPackage(deploymentId: string, rolloutId: string, previewDigest: string): Promise<{id: string}>;
  /** Optional until the control-plane inventory projection is available. */
  packageInventory?(): Promise<PackageInventoryNode[] | {nodes: PackageInventoryNode[]; total?: number; next_cursor?: string | null}>;
  previewPackageGc?(): Promise<PackageGcPreview>;
  applyPackageGc?(previewDigest: string): Promise<PackageGcProgress>;
  previewPackageRemoval?(input: {deployment_id: string; release_digest: string; node_ids: string[]}): Promise<PackagePreview>;
  removePackageInventory?(previewDigest: string): Promise<PackageGcProgress>;
}

import {render, screen, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {PackageCandidatePage} from "./package-candidate";
import {PackagesPage} from "./packages";


const digest = `sha256:${"a".repeat(64)}`;

function api() {
  return {
    packageFamilies: async () => [{id: "llm-runtime", channel: "stable", candidate_count: 2, deployment_count: 1}],
    packageCandidates: async () => [{
      id: "candidate-1",
      family_id: "llm-runtime",
      channel: "stable",
      provider: "oci",
      state: "unsupported",
      reason_code: "incomplete-checksum",
      upstream_version: "1.2.3",
      updated_at: "2026-08-05T12:00:00Z",
    }],
    packageCandidate: async () => ({
      id: "candidate-1",
      family_id: "llm-runtime",
      channel: "stable",
      provider: "oci",
      state: "promotion-eligible",
      reason_code: null,
      upstream_version: "1.2.3",
      updated_at: "2026-08-05T12:00:00Z",
      lock: {digest, components: ["runtime", "weights"], dependencies: ["cuda-12"], provenance: "verified"},
      compatibility: {compatible: ["Alpha GPU node"], incompatible_count: 1},
      validations: [{backend: "artifact", state: "passed", reason_code: null}],
      audit: [{action: "package.candidate.discovered", request_id: "request-1"}],
    }),
    previewPackagePromotion: async () => ({digest, release_digest: digest, expires_at: "2026-08-05T13:00:00Z", diff: "family: llm-runtime"}),
    promotePackage: async (_id: string, supplied: string) => ({release_digest: supplied}),
  };
}

it("shows unsupported candidates with their structured state without exposing source details", async () => {
  // Break caught: rejected package candidates disappear from administration or
  // the page exposes arbitrary source metadata in place of a stable reason.
  render(<PackagesPage api={api()}/>);

  expect(await screen.findByRole("heading", {name: "Workload packages"})).toBeVisible();
  expect(screen.getAllByText("llm-runtime")).toHaveLength(2);
  expect(screen.getByText("unsupported")).toBeVisible();
  expect(screen.getByText("incomplete-checksum")).toBeVisible();
  expect(screen.getByRole("link", {name: "View candidate candidate-1"})).toHaveAttribute("href", "/packages/candidate-1");
});

it("requires the exact promotion preview digest and projects audit evidence", async () => {
  // Break caught: promotion can be submitted with a stale or partial digest,
  // or package audit records are omitted from the candidate review.
  const calls: string[] = [];
  const control = {...api(), promotePackage: async (_id: string, supplied: string) => {
    calls.push(supplied);
    return {release_digest: supplied};
  }};
  render(<PackageCandidatePage api={control} candidateId="candidate-1"/>);
  const user = userEvent.setup();

  expect(await screen.findByText("promotion-eligible")).toBeVisible();
  expect(screen.getByRole("region", {name: "Package candidate state"})).toHaveTextContent("Components: runtime, weights");
  expect(screen.getByText("package.candidate.discovered")).toBeVisible();
  await user.click(screen.getByRole("button", {name: "Preview package promotion"}));
  expect(await screen.findByLabelText("Type the exact preview digest")).toBeVisible();
  expect(screen.getByRole("button", {name: "Promote exact package preview"})).toBeDisabled();

  await user.type(screen.getByLabelText("Type the exact preview digest"), digest);
  await user.click(screen.getByRole("button", {name: "Promote exact package preview"}));
  expect(calls).toEqual([digest]);
  expect(await screen.findByRole("status")).toHaveTextContent("Package promotion accepted");
});

it("starts validation through an exact plan and keeps the run inspectable", async () => {
  const calls: string[] = [];
  const control = {
    ...api(),
    previewPackageValidation: async () => ({digest: `sha256:${"v".repeat(64)}`, candidate_id: "candidate-1", validation_id: "validation-1"}),
    validatePackage: async (_id: string, supplied: string) => { calls.push(supplied); return {id: "validation-1", state: "running", progress: {completed: 0, failed: 0, running: 2, total: 2}}; },
    packageValidation: async () => ({id: "validation-1", state: "passed", job_id: "job-validation-1", progress: {completed: 2, failed: 0, running: 0, total: 2}, nodes: [{node_id: "node-a", state: "succeeded", batch_index: 0, completed: 2, total: 2}]}),
  };
  render(<PackageCandidatePage api={control} candidateId="candidate-1"/>);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", {name: "Preview package validation"}));
  const validationDigest = `sha256:${"v".repeat(64)}`;
  await user.type(screen.getByLabelText("Type the exact validation digest"), validationDigest);
  await user.click(screen.getByRole("button", {name: "Start exact validation plan"}));
  expect(calls).toEqual([validationDigest]);
  const validationSection = screen.getByRole("heading", {name: "Validation"}).closest("section");
  expect(validationSection).not.toBeNull();
  expect(within(validationSection!).getByRole("status")).toHaveTextContent("Validation state: running");
  await user.click(within(validationSection!).getByRole("button", {name: "Refresh validation"}));
  expect(within(validationSection!).getByRole("status")).toHaveTextContent("Validation state: passed");
  expect(within(validationSection!).getByText("node-a — succeeded (2/2)")).toBeVisible();
});

it("separates downloads from active generations and requires an exact removal preview", async () => {
  const removalDigest = `sha256:${"f".repeat(64)}`;
  const gcDigest = `sha256:${"g".repeat(64)}`;
  const calls: string[] = [];
  const control = {
    ...api(),
    packageInventory: async () => ({nodes: [{
      node_id: "node-a",
      display_name: "Alpha GPU node",
      online: true,
      storage: {total_bytes: 1000, used_bytes: 650, free_bytes: 350, reserved_bytes: 50, reclaimable_bytes: 200},
      resources: {host_memory_total_bytes: 1000, host_memory_free_bytes: 500, gpu_memory_total_bytes: 1000, gpu_memory_free_bytes: 700, gpu_count: 1},
      packages: [
        {deployment_id: "chat", family_id: "llm-runtime", release_digest: removalDigest, content_group: "weights", state: "available", bytes_total: 1000, bytes_complete: 1000, bytes_remaining: 0, installed_bytes: 400, reclaimable_bytes: 400, reserved_bytes: 0, active: false, retained: false, leased: false, resources: {download_bytes: 1000, installed_bytes: 400, transient_bytes: 100, output_bytes: 0, host_memory_bytes: 100, resident_memory_bytes: 100, auxiliary_memory_bytes: 0, activation_memory_bytes: 0, workspace_memory_bytes: 0, gpu_memory_bytes: 200, gpu_count: 1, cpu_millicores: 1, kv_cache_base_bytes: 10, kv_cache_per_token_bytes: 1, required_nodes: 1, topology: "single", world_size: 1, ranks: [{rank: 0, role: "primary"}], fabric: {kind: "none", min_bandwidth_mbps: 0}}},
        {deployment_id: "chat", family_id: "llm-runtime", release_digest: removalDigest, content_group: "runtime", state: "downloading", bytes_total: 2000, bytes_complete: 1000, bytes_remaining: 1000, installed_bytes: 0, reclaimable_bytes: 0, reserved_bytes: 2000, active: false, retained: false, leased: false, resources: {download_bytes: 2000, installed_bytes: 0, transient_bytes: 300, output_bytes: 0, host_memory_bytes: 512, resident_memory_bytes: 512, auxiliary_memory_bytes: 0, activation_memory_bytes: 0, workspace_memory_bytes: 0, gpu_memory_bytes: 768, gpu_count: 1, cpu_millicores: 1, kv_cache_base_bytes: 128, kv_cache_per_token_bytes: 2, required_nodes: 2, topology: "gang", world_size: 2, ranks: [{rank: 0, role: "primary"}, {rank: 1, role: "secondary"}], fabric: {kind: "rdma", min_bandwidth_mbps: 1}}},
        {deployment_id: "chat", family_id: "llm-runtime", release_digest: removalDigest, content_group: "active", state: "active", bytes_total: 100, bytes_complete: 100, bytes_remaining: 0, installed_bytes: 100, reclaimable_bytes: 0, reserved_bytes: 0, active: true, retained: false, leased: true, resources: {download_bytes: 100, installed_bytes: 100, transient_bytes: 0, output_bytes: 0, host_memory_bytes: 100, resident_memory_bytes: 100, auxiliary_memory_bytes: 0, activation_memory_bytes: 0, workspace_memory_bytes: 0, gpu_memory_bytes: 300, gpu_count: 1, cpu_millicores: 1, kv_cache_base_bytes: 10, kv_cache_per_token_bytes: 1, required_nodes: 1, topology: "single", world_size: 1, ranks: [{rank: 0, role: "primary"}], fabric: {kind: "none", min_bandwidth_mbps: 0}}},
      ],
    }], total: 1}),
    previewPackageRemoval: async (input: {deployment_id: string; release_digest: string; node_ids: string[]}) => { calls.push(JSON.stringify(input)); return {digest: removalDigest}; },
    removePackageInventory: async (digest: string) => { calls.push(digest); return {id: "remove-1", state: "accepted", phase: "remove", failure_reason: null, nodes: []}; },
    previewPackageGc: async () => ({digest: gcDigest, reclaim_bytes: 400}),
    applyPackageGc: async (digest: string) => { calls.push(digest); return {id: "gc-1", state: "accepted", phase: "gc", failure_reason: null, nodes: []}; },
  };
  render(<PackagesPage api={control}/>);
  const user = userEvent.setup();
  expect(await screen.findByText("Available")).toBeVisible();
  expect(screen.getByText("Active generation — protected while serving")).toBeVisible();
  expect(screen.getByText("Downloading")).toBeVisible();
  const downloading = screen.getByText("Downloading").closest(".inventory-package");
  expect(downloading).not.toBeNull();
  expect(within(downloading as HTMLElement).getByText("512 B")).toBeVisible();
  expect(within(downloading as HTMLElement).getByText("768 B")).toBeVisible();
  expect(within(downloading as HTMLElement).getByText("128 B")).toBeVisible();
  expect(within(downloading as HTMLElement).getByText("2")).toBeVisible();
  expect(downloading).toHaveTextContent("1000 B of 2.0 KiB");
  expect(downloading).toHaveTextContent("50%");
  expect(downloading).toHaveTextContent("1000 B remaining");
  expect(screen.getByText(/Downloaded packages are local and resumable/)).toBeVisible();
  expect(screen.getByLabelText("Alpha GPU node disk usage")).toBeVisible();
  await user.click(screen.getByRole("button", {name: "Preview removal"}));
  expect(await screen.findByLabelText("Type the exact removal preview digest")).toBeVisible();
  const input = screen.getByLabelText("Type the exact removal preview digest");
  expect(screen.getByRole("button", {name: "Remove exact generation"})).toBeDisabled();
  await user.type(input, removalDigest);
  await user.click(screen.getByRole("button", {name: "Remove exact generation"}));
  expect(calls).toEqual([JSON.stringify({deployment_id: "chat", release_digest: removalDigest, node_ids: ["node-a"]}), removalDigest]);
  await user.click(screen.getByRole("button", {name: "Preview cleanup"}));
  expect(await screen.findByLabelText("Type the exact cleanup preview digest")).toBeVisible();
  const cleanupInput = screen.getByLabelText("Type the exact cleanup preview digest");
  expect(screen.getByRole("button", {name: "Apply safe cleanup"})).toBeDisabled();
  await user.type(cleanupInput, gcDigest);
  await user.click(screen.getByRole("button", {name: "Apply safe cleanup"}));
  expect(calls).toContain(gcDigest);
});

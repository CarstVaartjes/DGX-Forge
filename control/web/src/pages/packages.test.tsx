import {render, screen} from "@testing-library/react";
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
      compatibility: {compatible: ["Alpha Spark"], incompatible_count: 1},
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

it("separates downloads from active generations and requires an exact removal preview", async () => {
  const removalDigest = `sha256:${"f".repeat(64)}`;
  const calls: string[] = [];
  const control = {
    ...api(),
    packageInventory: async () => ({nodes: [{
      node_id: "spark-a",
      online: true,
      storage: {total_bytes: 1000, used_bytes: 650, free_bytes: 350, reserved_bytes: 50, reclaimable_bytes: 200},
      resources: {host_memory_total_bytes: 1000, host_memory_free_bytes: 500, gpu_memory_total_bytes: 1000, gpu_memory_free_bytes: 700, gpu_count: 1},
      packages: [
        {deployment_id: "chat", family_id: "llm-runtime", release_digest: removalDigest, content_group: "weights", state: "available", bytes_total: 1000, bytes_complete: 1000, bytes_remaining: 0, installed_bytes: 400, reclaimable_bytes: 400, reserved_bytes: 0, active: false, retained: false, leased: false, resources: {download_bytes: 1000, installed_bytes: 400, transient_bytes: 100, host_memory_bytes: 100, gpu_memory_bytes: 200, kv_cache_base_bytes: 10, kv_cache_per_token_bytes: 1, required_sparks: 1, topology: "single"}},
        {deployment_id: "chat", family_id: "llm-runtime", release_digest: removalDigest, content_group: "active", state: "active", bytes_total: 100, bytes_complete: 100, bytes_remaining: 0, installed_bytes: 100, reclaimable_bytes: 0, reserved_bytes: 0, active: true, retained: false, leased: true, resources: {download_bytes: 100, installed_bytes: 100, transient_bytes: 0, host_memory_bytes: 100, gpu_memory_bytes: 300, kv_cache_base_bytes: 10, kv_cache_per_token_bytes: 1, required_sparks: 1, topology: "single"}},
      ],
    }], total: 1}),
    previewPackageRemoval: async (input: {deployment_id: string; release_digest: string; node_ids: string[]}) => { calls.push(JSON.stringify(input)); return {digest: removalDigest}; },
    removePackageInventory: async (digest: string) => { calls.push(digest); return {id: "remove-1", state: "accepted", phase: "remove", failure_reason: null, nodes: []}; },
  };
  render(<PackagesPage api={control}/>);
  const user = userEvent.setup();
  expect(await screen.findByText("Available")).toBeVisible();
  expect(screen.getByText("Active generation — protected while serving")).toBeVisible();
  await user.click(screen.getByRole("button", {name: "Preview removal"}));
  expect(await screen.findByLabelText("Type the exact removal preview digest")).toBeVisible();
  const input = screen.getByLabelText("Type the exact removal preview digest");
  expect(screen.getByRole("button", {name: "Remove exact generation"})).toBeDisabled();
  await user.type(input, removalDigest);
  await user.click(screen.getByRole("button", {name: "Remove exact generation"}));
  expect(calls).toEqual([JSON.stringify({deployment_id: "chat", release_digest: removalDigest, node_ids: ["spark-a"]}), removalDigest]);
});

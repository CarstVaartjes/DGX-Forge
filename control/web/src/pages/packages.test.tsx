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

import {render, screen} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {DeploymentsPage} from "./deployments";


const digest = `sha256:${"b".repeat(64)}`;

function api() {
  return {
    deployments: async () => [{
      id: "chat",
      family_id: "llm-runtime",
      release_digest: `sha256:${"c".repeat(64)}`,
      previous_release_digest: `sha256:${"d".repeat(64)}`,
      state: "active",
    }],
    previewPackageRollout: async () => ({
      digest,
      canary: ["Alpha Spark"],
      batches: [["Alpha Spark"], ["Beta Spark"]],
      offline_pending: ["Gamma Spark"],
      download_remaining_bytes: 8 * 1024 ** 3,
      storage_required_bytes: 12 * 1024 ** 3,
    }),
    startPackageRollout: async (_id: string, supplied: string) => ({id: "rollout-1", plan_digest: supplied}),
    packageRollout: async () => ({
      id: "rollout-1",
      state: "rolling-back",
      phase: "rollback",
      failure_reason: "canary-failure",
      nodes: [{name: "Alpha Spark", state: "rolled-back"}, {name: "Gamma Spark", state: "offline-pending"}],
    }),
    previewPackageRollback: async () => ({digest: `sha256:${"e".repeat(64)}`, previous_release_digest: `sha256:${"d".repeat(64)}`}),
    rollbackPackage: async () => ({id: "rollback-1"}),
  };
}

it("shows aggregate capacity, offline progress, and stops at a canary failure", async () => {
  // Break caught: operators cannot see remaining acquisition work, offline
  // Sparks, or an authoritative canary rollback state.
  render(<DeploymentsPage api={api()}/>);
  const user = userEvent.setup();

  expect(await screen.findByText("chat")).toBeVisible();
  await user.click(screen.getByRole("button", {name: "Preview rollout for chat"}));
  const preview = await screen.findByRole("region", {name: "Package rollout preview"});
  expect(preview).toHaveTextContent("8.0 GiB remaining");
  expect(preview).toHaveTextContent("Gamma Spark");
  await user.type(screen.getByLabelText("Type the exact rollout preview digest"), digest);
  await user.click(screen.getByRole("button", {name: "Start exact rollout"}));

  expect(await screen.findByText("canary-failure")).toBeVisible();
  const progress = screen.getByRole("region", {name: "Package rollout progress"});
  expect(progress).toHaveTextContent("rolled-back");
  expect(progress).toHaveTextContent("offline-pending");
});

it("requires exact confirmation before selecting the retained rollback generation", async () => {
  // Break caught: an operator can select a previous release without the
  // server-issued rollback preview digest.
  const calls: string[] = [];
  const control = {...api(), rollbackPackage: async (_id: string, supplied: string) => {
    calls.push(supplied);
    return {id: "rollback-1"};
  }};
  render(<DeploymentsPage api={control}/>);
  const user = userEvent.setup();
  await screen.findByText("chat");
  await user.click(screen.getByRole("button", {name: "Preview rollback for chat"}));
  const rollbackDigest = `sha256:${"e".repeat(64)}`;
  expect(await screen.findByText(rollbackDigest)).toBeVisible();
  await user.type(screen.getByLabelText("Type the exact rollback preview digest"), rollbackDigest);
  await user.click(screen.getByRole("button", {name: "Roll back exact retained generation"}));
  expect(calls).toEqual([rollbackDigest]);
});

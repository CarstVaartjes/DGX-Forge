import {render, screen, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type {components} from "../api/generated";
import type {ControlApi} from "../api/types";
import {JobsPage} from "./jobs";

type Job = components["schemas"]["JobDetailResponse"];
const jobId = "11111111-1111-4111-8111-111111111111";
const nodeId = "spk_0123456789abcdef0123456789abcdef";
const job: Job = {
  base_commit: "a".repeat(40), current_attempt: 2, id: jobId, kind: "reconcile",
  operations: [{attempt: 2, graph_operation_id: "stop:model-a", id: "operation-1", kind: "stop", node_id: nodeId, progress: {phase: "waiting for agent"}, state: "waiting", updated_at: "2026-08-05T12:00:00Z"}],
  progress: {completed: 8, failed: 1, running: 2, total: 12},
  reconciliation_id: "22222222-2222-4222-8222-222222222222", state: "waiting-for-operator",
  status_reason: "Operator must acknowledge compensation", targets: [nodeId],
};

it("shows bounded parent and node-operation progress and resumes operator waits", async () => {
  // Break caught: the list-only page hides durable operation progress or cannot
  // resume a job that explicitly waits for an operator.
  const resumed: string[] = [];
  const api = {
    jobs: async () => ({jobs: [{id: jobId, kind: "reconcile", state: "waiting-for-operator"}]}),
    job: async () => job,
    resumeJob: async (id: string) => { resumed.push(id); return {id, state: "queued"}; },
  } as unknown as ControlApi;
  render(<JobsPage api={api}/>);
  const user = userEvent.setup();

  await user.click(await screen.findByRole("button", {name: `View job ${jobId}`}));
  expect(await screen.findByRole("heading", {name: "Job detail"})).toBeVisible();
  expect(screen.getByText("8 of 12 completed; 2 running; 1 failed")).toBeVisible();
  const operation = screen.getByRole("row", {name: /operation-1/});
  expect(within(operation).getByText(nodeId)).toBeVisible();
  expect(within(operation).getByText("waiting for agent")).toBeVisible();

  await user.click(screen.getByRole("button", {name: "Resume job"}));
  expect(resumed).toEqual([jobId]);
  expect(await screen.findByRole("status")).toHaveTextContent("queued");
});
it("paginates job and operation collections instead of imposing a fleet-sized total cap", async () => {
  // Break caught: a fixed slice silently makes later jobs or operations
  // unreachable, or the page dumps every server-controlled item at once.
  const jobs = Array.from({length: 23}, (_, index) => ({id: `job-${index}`, kind: "reconcile", state: "queued"}));
  const operations = Array.from({length: 23}, (_, index) => ({
    attempt: 1, graph_operation_id: `graph-${index}`, id: `operation-${index}`, kind: "verify", node_id: nodeId,
    progress: {phase: `phase-${index}`}, state: "queued", updated_at: null,
  }));
  const api = {
    jobs: async () => ({jobs}),
    job: async (id: string) => ({...job, id, operations, state: "running"}),
  } as unknown as ControlApi;
  render(<JobsPage api={api}/>);
  const user = userEvent.setup();

  expect(await screen.findByText("Showing jobs 1–20 of 23")).toBeVisible();
  expect(screen.queryByText("job-22")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", {name: "Next jobs"}));
  expect(screen.getByText("job-22")).toBeVisible();
  await user.click(screen.getByRole("button", {name: "View job job-22"}));
  expect(await screen.findByText("Showing operations 1–20 of 23")).toBeVisible();
  expect(screen.queryByText("operation-22")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", {name: "Next operations"}));
  expect(screen.getByText("operation-22")).toBeVisible();
});

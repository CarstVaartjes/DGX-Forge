import {render, screen} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {SparkRunImportPage} from "./sparkrun-import";

const report = [{source_path: "/container", disposition: "resolution_required" as const, destination_path: "/runtime/image", reason_code: "runtime.image_digest", detail: "Resolve linux/arm64 digest.", blocking: true}];

test("previews every field before hash-bound import", async () => {
  const calls: string[] = [];
  const api = {
    previewSparkRun: async (source: string) => { calls.push(`preview:${source}`); return {draft_document: {}, report, source_sha256: "a".repeat(64), report_digest: "b".repeat(64), redacted_source: {}, runnable: false}; },
    applySparkRun: async (source: string, sourceHash: string, reportHash: string) => { calls.push(`apply:${sourceHash}:${reportHash}`); return {recipe_id: "10000000-0000-4000-8000-000000000001", revision_number: 1, lifecycle: "blocked"}; },
  };
  render(<SparkRunImportPage api={api}/>); const user = userEvent.setup();

  await user.type(screen.getByLabelText("SparkRun YAML"), "model: Example/Model");
  await user.click(screen.getByRole("button", {name: "Preview import"}));
  expect(await screen.findByText("Resolution required")).toBeVisible();
  expect(screen.getByText(`Source sha256:${"a".repeat(64)}`)).toBeVisible();
  expect(screen.getByRole("button", {name: "Import blocked draft"})).toBeEnabled();
  await user.click(screen.getByRole("button", {name: "Import blocked draft"}));
  expect(await screen.findByRole("status")).toHaveTextContent("Imported blocked draft revision 1");
  expect(calls[1]).toBe(`apply:${"a".repeat(64)}:${"b".repeat(64)}`);
});

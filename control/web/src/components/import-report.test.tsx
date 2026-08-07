import {render, screen} from "@testing-library/react";
import {ImportReport} from "./import-report";

test("explains every omitted or blocked source field", () => {
  render(<ImportReport items={[
    {source_path: "/mods/0", disposition: "unsupported_blocking", destination_path: null, reason_code: "workload_run.mods_unsupported", detail: "Mods cannot execute from a recipe; publish the behavior in a container.", blocking: true},
    {source_path: "/container", disposition: "resolution_required", destination_path: "/runtime/image", reason_code: "runtime.image_digest", detail: "Resolve the ARM64 image digest.", blocking: true},
    {source_path: "/model", disposition: "imported", destination_path: "/artifacts/0/repository", reason_code: "artifact.repository", detail: "Repository imported.", blocking: false},
  ]}/>);

  expect(screen.getByText("Unsupported — blocks running")).toBeVisible();
  expect(screen.getByText(/mods cannot execute from a recipe/i)).toBeVisible();
  expect(screen.getByText("Resolution required")).toBeVisible();
  expect(screen.getByText("Imported")).toBeVisible();
});

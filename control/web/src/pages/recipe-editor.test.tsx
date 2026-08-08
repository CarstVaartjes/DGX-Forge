import {render, screen} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {RecipeEditorPage} from "./recipe-editor";

test("uploads source first and authors a source-first typed recipe", async () => {
  const created: unknown[] = [];
  const uploaded: string[] = [];
  const api = {
    uploadSourceBundle: async (sha256: string) => {
      uploaded.push(sha256);
      return {sha256, archive_bytes: 1, total_bytes: 1, file_count: 1, files: ["Dockerfile"]};
    },
    createCatalogRecipe: async (input: unknown) => {
      created.push(input);
      return {recipe_id: "10000000-0000-4000-8000-000000000001", revision_number: 1};
    },
  };
  render(<RecipeEditorPage api={api as any}/>);
  const user = userEvent.setup();

  expect(screen.getByRole("heading", {name: "Create local recipe"})).toBeVisible();
  expect((screen.getByLabelText("Dockerfile") as HTMLTextAreaElement).value).toContain("USER 65532:65532");

  await user.type(screen.getByLabelText("Recipe slug"), "my-model");
  await user.type(screen.getByLabelText("Title"), "My model");
  await user.type(screen.getByLabelText("Description"), "A locally authored model recipe.");
  await user.type(screen.getByLabelText("Artifact repository"), "Example/MyModel");
  await user.type(screen.getByLabelText("Artifact revision"), "0123456789abcdef0123456789abcdef01234567");
  await user.clear(screen.getByLabelText("Artifact bytes"));
  await user.type(screen.getByLabelText("Artifact bytes"), "1000000");
  await user.click(screen.getByRole("button", {name: "Verify source & save draft"}));
  expect(await screen.findByRole("status")).toHaveTextContent("Source verified and draft saved as revision 1");

  expect(uploaded).toHaveLength(1);
  expect(created).toHaveLength(1);
  const input = created[0] as {slug: string; document: Record<string, any>};
  expect(input.slug).toBe("my-model");
  expect(input.document.runtime.security.privileged).toBe(false);
  expect(input.document.runtime.security.host_network).toBe(false);
  expect(input.document.build.platform).toBe("linux/arm64");
  expect(input.document.runtime.interface).toBe("vonk.runtime.v1");
  expect(input.document.runtime).not.toHaveProperty("image");
  expect(input.document.build.context.sha256).toBe(uploaded[0]);
  expect(input.document.deployment_profiles[0].node_count).toBe(1);
});

test("attaches local test evidence and exports for an exact publisher namespace", async () => {
  const recipe = {
    recipe_id: "10000000-0000-4000-8000-000000000001", revision_number: 2,
    slug: "qwen", title: "Qwen", description: "Test", origin: "local", lifecycle: "resolved",
    content_sha256: "a".repeat(64), schema_version: 1, created_by: "admin", created_at: "2026-08-07T10:00:00Z",
    document: {identity: {publisher: "local", slug: "qwen"}, metadata: {title: "Qwen", description: "Test", tags: []}, workload: {family: "qwen", capabilities: ["openai.chat"]}, artifacts: [{kind: "huggingface.snapshot", repository: "Qwen/Qwen", revision: "b".repeat(40), expected_bytes: 1}], runtime: {interface: "vonk.runtime.v1", family: "vllm", image: `ghcr.io/vonk/qwen@sha256:${"c".repeat(64)}`, architecture: "linux/arm64", arguments: []}, resources: {per_node: {download_bytes: 1, installed_bytes: 1, staging_bytes: 1, resident_memory_bytes: 1, activation_memory_bytes: 1}, measurement: "measured"}, topology: {kind: "single", min_nodes: 1, max_nodes: 1, tested_node_counts: [1]}, endpoint: {protocol: "openai", port: 8000, model_aliases: ["qwen"], health_path: "/v1/models"}, security: {devices: ["nvidia.com/gpu=all"], capabilities: [], host_network: false, privileged: false, mounts: [{source: "model", target: "/models", read_only: true}, {source: "state", target: "/state", read_only: false}]}, provenance: {source_kind: "local", source_reference: null, attribution: []}},
  } as any;
  const reports: unknown[] = [];
  const exports: string[] = [];
  const api = {
    createCatalogRecipe: async () => recipe,
    catalogRecipe: async () => recipe,
    attachPublicationReport: async (_id: string, report: unknown) => { reports.push(report); },
    publicationExport: async (_id: string, publisher: string) => { exports.push(publisher); return {recipe: recipe.document, test_report: {}}; },
  };
  const createObjectURL = vi.fn(() => "blob:export");
  Object.defineProperty(URL, "createObjectURL", {value: createObjectURL, configurable: true});
  const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
  render(<RecipeEditorPage api={api} recipeId={recipe.recipe_id}/>);
  const user = userEvent.setup();
  const report = {schema_version: 1, recipe_sha256: "a".repeat(64)};

  await screen.findByText(/Canonical content/);
  await user.upload(screen.getByLabelText("Local test report JSON"), new File([JSON.stringify(report)], "report.json", {type: "application/json"}));
  expect(reports).toEqual([report]);
  await user.clear(screen.getByLabelText("Target publisher namespace"));
  await user.type(screen.getByLabelText("Target publisher namespace"), "ada-lab");
  await user.click(screen.getByRole("button", {name: "Download publication JSON"}));

  expect(exports).toEqual(["ada-lab"]);
  expect(createObjectURL).toHaveBeenCalled();
  expect(click).toHaveBeenCalled();
  click.mockRestore();
});

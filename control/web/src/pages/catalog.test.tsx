import {render, screen} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {CatalogPage} from "./catalog";

const recipes = [
  {recipe_id: "10000000-0000-4000-8000-000000000001", slug: "local-qwen", title: "Local Qwen", origin: "local" as const, revision_number: 2, lifecycle: "resolved" as const, content_sha256: "a".repeat(64), runtime_family: "vllm", runtime_image: `ghcr.io/vonk/vllm@sha256:${"b".repeat(64)}`, artifact_count: 1, expected_download_bytes: 61_000_000_000, installed_bytes_per_node: 66_000_000_000, resident_memory_bytes_per_node: 72_000_000_000, activation_memory_bytes_per_node: 8_000_000_000, min_nodes: 1, max_nodes: 1},
  {recipe_id: "10000000-0000-4000-8000-000000000002", slug: "deepseek", title: "DeepSeek", origin: "sparkrun" as const, revision_number: 1, lifecycle: "draft" as const, content_sha256: null, runtime_family: "sglang", runtime_image: `ghcr.io/demo/sglang@sha256:${"c".repeat(64)}`, artifact_count: 2, expected_download_bytes: 120_000_000_000, installed_bytes_per_node: 130_000_000_000, resident_memory_bytes_per_node: 90_000_000_000, activation_memory_bytes_per_node: 10_000_000_000, min_nodes: 2, max_nodes: 4},
  {recipe_id: "10000000-0000-4000-8000-000000000003", slug: "global-model", title: "Global model", origin: "global" as const, revision_number: 4, lifecycle: "resolved" as const, content_sha256: "d".repeat(64), runtime_family: "llama.cpp", runtime_image: `ghcr.io/demo/llama@sha256:${"e".repeat(64)}`, artifact_count: 1, expected_download_bytes: 10_000_000_000, installed_bytes_per_node: 12_000_000_000, resident_memory_bytes_per_node: 16_000_000_000, activation_memory_bytes_per_node: 2_000_000_000, min_nodes: 1, max_nodes: 1},
];

test("separates local, SparkRun, and global recipe origins", async () => {
  render(<CatalogPage api={{catalogRecipes: async () => ({recipes, next_cursor: null})}}/>);

  expect(await screen.findByRole("heading", {name: "Recipe catalog"})).toBeVisible();
  expect(screen.getByText("Local")).toBeVisible();
  expect(screen.getByText("Imported from SparkRun")).toBeVisible();
  expect(screen.getByText("Downloaded from vonkforge.ai")).toBeVisible();
  expect(screen.getByText("66.0 GB disk / node")).toBeVisible();
  expect(screen.getByText("72.0 GB + 8.0 GB activation / node")).toBeVisible();
  expect(screen.getByText("2–4 nodes")).toBeVisible();
  expect(screen.getByRole("link", {name: "Create local recipe"})).toHaveAttribute("href", "/catalog/new");
});

test("reviews an exact public revision before importing it locally", async () => {
  const imported: string[] = [];
  const uri = `vonk://catalog/vonk/qwen3-vllm@sha256:${"a".repeat(64)}`;
  const api = {
    catalogRecipes: async () => ({recipes: [], next_cursor: null}),
    previewGlobalRecipe: async (value: string) => ({
      publisher: "vonk", slug: "qwen3-vllm", revision_number: 2,
      recipe_id: "00000000-0000-4000-8000-000000000002",
      revision_id: "10000000-0000-4000-8000-000000000002",
      content_sha256: "a".repeat(64), published_at: "2026-08-07T10:00:00Z",
      document: {metadata: {title: "Qwen3"}, resources: {per_node: {installed_bytes: 66_000_000_000, resident_memory_bytes: 72_000_000_000}}, topology: {kind: "single", min_nodes: 1, max_nodes: 1}},
    }),
    importGlobalRecipe: async (value: string, digest: string) => {
      imported.push(`${value}:${digest}`);
      return {...recipes[2], id: "20000000-0000-4000-8000-000000000001", description: "Imported", schema_version: 1 as const, document: {}, created_by: "admin", created_at: "2026-08-07T10:00:00Z"};
    },
  };
  render(<CatalogPage api={api}/>);
  const user = userEvent.setup();

  await user.type(screen.getByLabelText("Immutable vonkforge.ai URI"), uri);
  await user.click(screen.getByRole("button", {name: "Review global recipe"}));

  expect(await screen.findByRole("heading", {name: "Review Qwen3"})).toBeVisible();
  expect(screen.getByText("66.0 GB disk / node")).toBeVisible();
  expect(screen.getByText("72.0 GB RAM / node")).toBeVisible();
  await user.click(screen.getByRole("button", {name: "Import exact revision"}));
  expect(imported).toEqual([`${uri}:${"a".repeat(64)}`]);
  expect(await screen.findByRole("status")).toHaveTextContent("available offline");
});

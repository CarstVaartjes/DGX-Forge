import {render, screen} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {RecipeEditorPage} from "./recipe-editor";

test("authors a typed recipe without exposing command or shell fields", async () => {
  const created: unknown[] = [];
  const api = {
    createCatalogRecipe: async (input: unknown) => {
      created.push(input);
      return {recipe_id: "10000000-0000-4000-8000-000000000001", revision_number: 1};
    },
  };
  render(<RecipeEditorPage api={api}/>);
  const user = userEvent.setup();

  expect(screen.getByRole("heading", {name: "Create local recipe"})).toBeVisible();
  expect(screen.queryByLabelText(/shell|command/i)).not.toBeInTheDocument();
  expect(screen.queryByRole("textbox", {name: /json/i})).not.toBeInTheDocument();

  await user.type(screen.getByLabelText("Recipe slug"), "my-model");
  await user.type(screen.getByLabelText("Title"), "My model");
  await user.type(screen.getByLabelText("Description"), "A locally authored model recipe.");
  await user.type(screen.getByLabelText("Artifact repository"), "Example/MyModel");
  await user.type(screen.getByLabelText("Artifact revision"), "0123456789abcdef0123456789abcdef01234567");
  await user.type(screen.getByLabelText("Artifact bytes"), "1000000");
  await user.type(screen.getByLabelText("Runtime image digest"), `ghcr.io/example/vllm@sha256:${"a".repeat(64)}`);
  await user.click(screen.getByRole("button", {name: "Save draft"}));

  expect(created).toHaveLength(1);
  const input = created[0] as {slug: string; document: Record<string, any>};
  expect(input.slug).toBe("my-model");
  expect(input.document.security.privileged).toBe(false);
  expect(input.document.security.host_network).toBe(false);
  expect(input.document.runtime.architecture).toBe("linux/arm64");
  expect(await screen.findByRole("status")).toHaveTextContent("Draft saved as revision 1");
});

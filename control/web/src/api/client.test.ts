import {ApiClient} from "./client";

afterEach(() => {
  document.cookie = "dgx_csrf=; Max-Age=0; path=/";
  vi.unstubAllGlobals();
});

it("uses the generated fleet operation with same-origin credentials", async () => {
  let captured: Request | undefined;
  vi.stubGlobal("fetch", async (input: RequestInfo | URL) => {
    captured = input as Request;
    return new Response(JSON.stringify({commit: "a".repeat(40), nodes: []}), {
      headers: {"Content-Type": "application/json"},
      status: 200,
    });
  });

  const fleet = await new ApiClient().fleet();

  expect(fleet).toEqual({commit: "a".repeat(40), nodes: []});
  expect(new URL(captured!.url).pathname).toBe("/api/v1/fleet");
  expect(captured!.credentials).toBe("same-origin");
});

it("adds the session CSRF token to generated enrollment mutations", async () => {
  document.cookie = "dgx_csrf=csrf-value; path=/";
  let captured: Request | undefined;
  vi.stubGlobal("fetch", async (input: RequestInfo | URL) => {
    captured = input as Request;
    return new Response(JSON.stringify({
      expires_at: "2026-08-05T10:15:00Z",
      id: "grant-001",
      node_id: "spk_0123456789abcdef0123456789abcdef",
      token: "g".repeat(48),
    }), {headers: {"Content-Type": "application/json"}, status: 201});
  });

  await new ApiClient().createEnrollmentGrant("spk_0123456789abcdef0123456789abcdef", 300);

  expect(captured!.method).toBe("POST");
  expect(captured!.headers.get("X-CSRF-Token")).toBe("csrf-value");
  expect(captured!.credentials).toBe("same-origin");
});

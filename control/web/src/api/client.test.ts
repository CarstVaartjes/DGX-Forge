import {ApiClient} from "./client";
it("uses only versioned same-origin API calls with credentials", async () => { const mock = vi.fn().mockResolvedValue({ok: true, json: async () => ({nodes: []})}); vi.stubGlobal("fetch", mock); await new ApiClient().fleet(); expect(mock).toHaveBeenCalledWith("/api/v1/fleet", expect.objectContaining({credentials: "same-origin"})); });

import {makeSourceBundle} from "./source-bundle";

test("uses the same canonical manifest digest as the controller", async () => {
  const bundle = await makeSourceBundle({Dockerfile: "FROM scratch\nUSER 65532:65532\n"});

  expect(bundle.sha256).toBe("a63195c43994ff10e1d22dbfdc4a8632abafc2e04626c4947a4dfa554b416d80");
  expect(bundle.totalBytes).toBe(30);
  expect(bundle.files).toEqual(["Dockerfile"]);
});

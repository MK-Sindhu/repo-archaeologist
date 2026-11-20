import { describe, expect, it } from "vitest";
import { repoSlugFromId } from "./repoId";

describe("repoSlugFromId", () => {
  it("recovers owner/name from GitHub clone ids", () => {
    expect(repoSlugFromId("pallets__click-1a2b3c4d")).toBe("pallets/click");
    expect(repoSlugFromId("MK-Sindhu__obligraph-41d38f74")).toBe("MK-Sindhu/obligraph");
    expect(repoSlugFromId("pallets__markupsafe-2575b5b5")).toBe("pallets/markupsafe");
  });
  it("returns null for local clones and odd ids", () => {
    expect(repoSlugFromId("toy-45a5e8a7")).toBeNull();
    expect(repoSlugFromId("a__b")).toBeNull();
    expect(repoSlugFromId("a__b-XYZ12345")).toBeNull();
  });
});

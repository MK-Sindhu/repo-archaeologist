import { describe, expect, it } from "vitest";
import { parseRepoView, serializeRepoView } from "./urlState";

describe("repo view URL state", () => {
  it("defaults to the Ask tab", () => {
    expect(parseRepoView("")).toEqual({ tab: "ask", dir: "", file: null, symbol: null });
    expect(parseRepoView("?tab=nonsense")).toMatchObject({ tab: "ask" });
  });

  it("reads the map state", () => {
    expect(parseRepoView("?tab=map&dir=src/&file=src%2Fpkg%2Fretry.py&symbol=backoff")).toEqual({
      tab: "map",
      dir: "src",
      file: "src/pkg/retry.py",
      symbol: "backoff",
    });
    expect(parseRepoView("?tab=map&symbol=backoff").symbol).toBeNull(); // a symbol needs a file
  });

  it("round-trips and keeps unrelated parameters", () => {
    const view = { tab: "map" as const, dir: "src", file: "src/a b.py", symbol: "A.run" };
    const search = serializeRepoView(view, "?mock=1&tab=guide");
    expect(search).toContain("mock=1");
    expect(parseRepoView(search)).toEqual(view);
    expect(serializeRepoView({ tab: "ask", dir: "x", file: "y", symbol: "z" }, "?mock=1")).toBe("?mock=1");
    expect(serializeRepoView({ tab: "guide", dir: "", file: null, symbol: null })).toBe("?tab=guide");
  });
});

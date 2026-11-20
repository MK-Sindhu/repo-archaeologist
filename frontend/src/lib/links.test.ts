import { describe, expect, it } from "vitest";
import type { Find } from "../api/types";
import { FINDS } from "../dev/mockData";
import { edges, resolveRef } from "./links";

const ids = new Set(["pr:12", "issue:7", "commit:c6d98fc00bcb20f09cbca555c1c69b297ad532cc"]);

describe("resolveRef", () => {
  it("matches exact ids", () => expect(resolveRef("pr:12", ids)).toBe("pr:12"));
  it("matches gh:N to a PR or issue", () => {
    expect(resolveRef("gh:12", ids)).toBe("pr:12");
    expect(resolveRef("gh:7", ids)).toBe("issue:7");
  });
  it("falls back across pr/issue (shared number space)", () => {
    expect(resolveRef("issue:12", ids)).toBe("pr:12");
    expect(resolveRef("pr:7", ids)).toBe("issue:7");
  });
  it("matches commit sha prefixes of at least 7", () => {
    expect(resolveRef("commit:c6d98fc", ids)).toBe("commit:c6d98fc00bcb20f09cbca555c1c69b297ad532cc");
    expect(resolveRef("commit:c6d98f", ids)).toBeNull();
    expect(resolveRef("commit:deadbeef", ids)).toBeNull();
  });
  it("returns null for unknown refs", () => expect(resolveRef("pr:99", ids)).toBeNull());
});

describe("edges", () => {
  it("links strata shallow to deep, once per pair", () => {
    const e = edges([FINDS.c2, FINDS.pr12, FINDS.issue7, FINDS.backoff], { reduce: false });
    expect(e).toEqual([
      { from: FINDS.c2.id, to: "pr:12" },
      { from: FINDS.c2.id, to: "issue:7" },
      { from: "pr:12", to: "issue:7" },
    ]);
  });

  it("drops relations implied by a chain (Harris-matrix reduction)", () => {
    const e = edges([FINDS.c2, FINDS.pr12, FINDS.issue7, FINDS.backoff]);
    expect(e).toEqual([
      { from: FINDS.c2.id, to: "pr:12" },
      { from: "pr:12", to: "issue:7" },
    ]);
  });

  it("keeps a long link when nothing in between connects", () => {
    expect(edges([FINDS.c2, FINDS.issue7])).toEqual([{ from: FINDS.c2.id, to: "issue:7" }]);
  });

  it("orients deep-to-shallow refs (PR naming its merge commit)", () => {
    const pr: Find = { ...FINDS.pr12, refs: [`commit:${FINDS.c2.id.slice(7, 19)}`] };
    expect(edges([pr, FINDS.c2])).toEqual([{ from: FINDS.c2.id, to: "pr:12" }]);
  });

  it("skips same-layer links and self links", () => {
    const c: Find = { ...FINDS.c3, refs: [FINDS.c2.id, FINDS.c3.id] };
    expect(edges([c, FINDS.c2].map((f) => ({ ...f, refs: f === FINDS.c2 ? [] : f.refs })))).toEqual([]);
  });
});

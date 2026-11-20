import { describe, expect, it } from "vitest";
import { citedIds, findIdFromHref, isRef, linkCitations, parseGroup } from "./citations";

const labels: Record<string, string> = {
  "pr:12": "F-04",
  "issue:7": "F-05",
  "commit:c6d98fc00bcb20f09cbca555c1c69b297ad532cc": "F-02",
  "code:src/pkg/retry.py#L8-L13": "F-01",
};
const label = (id: string) => labels[id];

describe("isRef / parseGroup", () => {
  it("accepts canonical refs", () => {
    for (const r of ["commit:c6d98fc", "pr:12", "issue:7", "gh:9", "code:src/a.py#L1-L5", "doc:README.md#settings"]) {
      expect(isRef(r)).toBe(true);
    }
  });
  it("rejects other bracket text", () => {
    for (const r of ["x", "commit:xyz", "pr:abc", "see pr:12", "1"]) expect(isRef(r)).toBe(false);
    expect(parseGroup("pr:12, note")).toBeNull();
    expect(parseGroup("pr:12, issue:7")).toEqual(["pr:12", "issue:7"]);
  });
});

describe("linkCitations", () => {
  it("turns known citations into find links", () => {
    const out = linkCitations("Capped [issue:7] via [pr:12].", label);
    expect(out).toBe("Capped [F-05](#find:issue%3A7) via [F-04](#find:pr%3A12).");
  });

  it("splits grouped citations", () => {
    expect(linkCitations("x [pr:12; issue:7]", label)).toBe(
      "x [F-04](#find:pr%3A12) [F-05](#find:issue%3A7)",
    );
  });

  it("encodes ids with paths and anchors", () => {
    const out = linkCitations("see [code:src/pkg/retry.py#L8-L13]", label);
    expect(out).toBe("see [F-01](#find:code%3Asrc%2Fpkg%2Fretry.py%23L8-L13)");
    expect(findIdFromHref("#find:code%3Asrc%2Fpkg%2Fretry.py%23L8-L13")).toBe("code:src/pkg/retry.py#L8-L13");
  });

  it("leaves unknown ids as plain text", () => {
    expect(linkCitations("x [pr:99]", label)).toBe("x [pr:99]");
  });

  it("leaves code, links, images and plain brackets alone", () => {
    const md = [
      "`[pr:12]` and [docs](https://x.y) and ![img](a.png) and [note] and [x][ref]",
      "```",
      "a = b[pr:12]",
      "```",
    ].join("\n");
    expect(linkCitations(md, label)).toBe(md);
  });

  it("handles markdown around citations", () => {
    const md = "- `backoff()` returns `min(2 ** attempt, MAX_BACKOFF)` [code:src/pkg/retry.py#L8-L13].";
    expect(linkCitations(md, label)).toContain("[F-01](#find:");
    expect(linkCitations(md, label)).toContain("`min(2 ** attempt, MAX_BACKOFF)`");
  });
});

describe("citedIds", () => {
  it("lists ids in order outside code", () => {
    expect(citedIds("a [pr:12] b [issue:7, pr:12] `[gh:1]`")).toEqual(["pr:12", "issue:7"]);
  });
  it("ignores ordinary links", () => {
    expect(findIdFromHref("https://github.com")).toBeNull();
  });
});

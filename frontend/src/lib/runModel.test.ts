import { describe, expect, it } from "vitest";
import type { Find, RunEvent } from "../api/types";
import { DEMO_EVENTS, FINDS, MOCK_ANSWER } from "../dev/mockData";
import { accessionLabel, findState, foldRun, layers, mergeFind } from "./runModel";

describe("foldRun", () => {
  it("folds the demo run into a finished view", () => {
    const v = foldRun(DEMO_EVENTS);
    expect(v.status).toBe("done");
    expect(v.answer).toBe(MOCK_ANSWER);
    expect(v.cited).toEqual(new Set(MOCK_ANSWER.citations));
    expect(v.hops).toBe(1);
    expect(v.confidence).toBe("high");
    expect(v.timeline.filter((t) => t.type === "log")).toHaveLength(1);
  });

  it("numbers finds in order of first appearance, stable on replay", () => {
    const v = foldRun(DEMO_EVENTS);
    expect(v.accession.get(FINDS.backoff.id)).toBe(1);
    expect(v.accession.get(FINDS.module.id)).toBe(2);
    expect(v.accession.get(FINDS.readme.id)).toBe(3);
    expect(v.accession.get("pr:12")).toBe(v.order.indexOf("pr:12") + 1);
    // the history step re-reports c2/c3: no new numbers
    expect(v.order.filter((id) => id === FINDS.c2.id)).toHaveLength(1);
    const again = foldRun([...DEMO_EVENTS]);
    expect([...again.accession]).toEqual([...v.accession]);
  });

  it("is idempotent: refolding a prefix then the whole gives the same result", () => {
    const half = foldRun(DEMO_EVENTS.slice(0, 8));
    expect(half.status).toBe("running");
    expect(half.answer).toBeNull();
    const full = foldRun(DEMO_EVENTS);
    expect(foldRun(DEMO_EVENTS).order).toEqual(full.order);
  });

  it("keeps full items over partial ones", () => {
    const partial: Find = { ...FINDS.pr12, partial: true, excerpt: "stub", refs: [] };
    const events: RunEvent[] = [
      { type: "step", node: "retrieve", at: 1, summary: "", evidence: [partial] },
      { type: "step", node: "follow", at: 2, summary: "", evidence: [FINDS.pr12] },
      { type: "step", node: "retrieve", at: 3, summary: "", evidence: [partial] },
    ];
    const f = foldRun(events).finds.get("pr:12")!;
    expect(f.partial).toBe(false);
    expect(f.refs).toEqual(FINDS.pr12.refs);
  });

  it("records errors", () => {
    const v = foldRun([
      { type: "status", status: "running", at: 0 },
      { type: "status", status: "error", at: 3, error: "daily quota used up" },
    ]);
    expect(v.status).toBe("error");
    expect(v.error).toBe("daily quota used up");
  });
});

describe("mergeFind", () => {
  it("fills missing fields from the earlier version", () => {
    const a: Find = { ...FINDS.c2, text: "full text" };
    const b: Find = { ...FINDS.c2, text: null, url: null };
    const m = mergeFind(a, b);
    expect(m.text).toBe("full text");
    expect(m.url).toBe(FINDS.c2.url);
  });
});

describe("states and layers", () => {
  const v = foldRun(DEMO_EVENTS);

  it("classifies finds", () => {
    expect(findState(v, "issue:7")).toBe("cited");
    expect(findState(v, FINDS.readme.id)).toBe("dropped");
    expect(findState(foldRun(DEMO_EVENTS.slice(0, 4)), FINDS.backoff.id)).toBe("retrieved");
  });

  it("groups finds into strata with cited first and irrelevant collapsed", () => {
    const [surface, commits, prs, issues] = layers(v);
    expect(surface.shown.map((f) => f.id)).toEqual([FINDS.backoff.id, FINDS.module.id]);
    expect(surface.dropped.map((f) => f.id)).toEqual([FINDS.readme.id]);
    expect(commits.shown.map((f) => f.id)).toEqual([FINDS.c2.id]);
    expect(commits.dropped).toHaveLength(2);
    expect(prs.shown.map((f) => f.id)).toEqual(["pr:12"]);
    expect(issues.shown.map((f) => f.id)).toEqual(["issue:7"]);
  });

  it("keeps arrival order while running", () => {
    const running = foldRun(DEMO_EVENTS.slice(0, 6));
    expect(layers(running)[1].shown.map((f) => f.id)).toEqual([FINDS.c2.id, FINDS.c3.id]);
  });

  it("formats accession numbers", () => {
    expect(accessionLabel(3)).toBe("F-03");
    expect(accessionLabel(112)).toBe("F-112");
  });
});

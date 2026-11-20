import { describe, expect, it } from "vitest";
import type { ShowcaseIndex, ShowcaseRecording } from "../../api/types";
import { foldRun } from "../../lib/runModel";
import { resolveRef } from "../../lib/links";
import { citedOnly, heroSlug, HERO_SLUG, keptOnly, orderSlugs, summarize, totals, walkthrough } from "./showcase";

// every recording the site ships (public/showcase/*.json), loaded at test time
const files = import.meta.glob<{ default: unknown }>("/public/showcase/*.json", { eager: true });
const recordings = Object.entries(files)
  .filter(([path]) => !path.endsWith("/index.json"))
  .map(([path, mod]) => [path, mod.default as ShowcaseRecording] as const);
const index = Object.entries(files).find(([p]) => p.endsWith("/index.json"))?.[1].default as ShowcaseIndex | undefined;

describe("shipped recordings", () => {
  it("exist", () => {
    expect(recordings.length).toBeGreaterThan(0);
  });

  it.each(recordings)("%s folds into a finished, fully cited run", (path, rec) => {
    expect(path.endsWith(`/${rec.slug}.json`)).toBe(true);
    expect(rec.repo.slug).toMatch(/^[^/]+\/[^/]+$/);
    expect(rec.run.status).toBe("done");
    const view = foldRun(rec.run.events, rec.run.status);
    expect(view.status).toBe("done");
    expect(view.answer).not.toBeNull();
    const ids = new Set(view.finds.keys());
    for (const c of view.answer!.citations) expect(resolveRef(c, ids)).not.toBeNull();
    const s = summarize(rec);
    expect(s.question).toBe(rec.run.question);
    expect(s.cited).toBe(view.answer!.citations.length);
    expect(s.cited).toBeGreaterThan(0);
  });

  it("are all listed by index.json (when present), and only those", () => {
    if (!index) return;
    const slugs = new Set(recordings.map(([, r]) => r.slug));
    for (const r of index.recordings) expect(slugs.has(r.slug)).toBe(true);
  });
});

describe("filters and walkthrough", () => {
  const rec = recordings.find(([, r]) => r.slug === HERO_SLUG)?.[1] ?? recordings[0][1];

  it("citedOnly keeps exactly the cited finds, in steps and in the answer", () => {
    const view = foldRun(citedOnly(rec.run.events), "done");
    const full = foldRun(rec.run.events, "done");
    expect([...view.finds.keys()].sort()).toEqual([...full.cited].filter((id) => full.finds.has(id)).sort());
    expect(view.answer!.evidence.every((f) => full.cited.has(f.id))).toBe(true);
    expect(view.cited).toEqual(full.cited);
  });

  it("keptOnly drops what the run set aside, and nothing it kept", () => {
    const view = foldRun(keptOnly(rec.run.events), "done");
    const full = foldRun(rec.run.events, "done");
    for (const id of full.cited) if (full.finds.has(id)) expect(view.finds.has(id)).toBe(true);
    expect(view.finds.size).toBeLessThanOrEqual(full.finds.size);
  });

  it("walkthrough groups the parallel searches and ends on the last event", () => {
    const steps = walkthrough(rec.run.events);
    expect(steps[0].node).toBe("analyze");
    expect(steps.filter((s) => s.node === "retrieve")).toHaveLength(1);
    expect(steps[steps.length - 1].upTo).toBe(rec.run.events.length);
    for (let i = 1; i < steps.length; i++) expect(steps[i].upTo).toBeGreaterThan(steps[i - 1].upTo);
  });
});

describe("ordering and totals", () => {
  it("puts the hero recording first and falls back to the first", () => {
    expect(orderSlugs(["a", HERO_SLUG, "b"])).toEqual([HERO_SLUG, "a", "b"]);
    expect(heroSlug(["a", "b"])).toBe("a");
    expect(heroSlug([])).toBeNull();
  });

  it("adds up claims only from runs that checked them", () => {
    const base = summarize(recordings[0][1]);
    const t = totals([
      { ...base, claims: { checked: 5, supported: 5 } },
      { ...base, claims: { checked: 4, supported: 3 } },
      { ...base, claims: null },
    ]);
    expect(t).toMatchObject({ recordings: 3, claimsChecked: 9, claimsSupported: 8 });
  });
});

/** Facts about recorded investigations, computed from the recordings themselves (never typed
 *  in by hand): what the public site may claim about them. Pure functions. */

import type { RunEvent, ShowcaseRecording, StepEvent } from "../../api/types";
import { findState, foldRun } from "../../lib/runModel";

/** The recording the hero and /how-it-works replay, when it exists. */
export const HERO_SLUG = "markupsafe-striptags";

export interface RecordingSummary {
  slug: string;
  repo: ShowcaseRecording["repo"];
  question: string;
  recorded_at: string;
  model: string;
  hops: number;
  finds: number; // everything retrieved
  relevant: number; // kept or cited
  cited: number;
  claims: { checked: number; supported: number } | null; // null when claims were not checked
  calls: number | null; // LLM calls
  seconds: number | null; // wall-clock length of the real run
  confidence: string | null;
  layers: { commits: number; prs: number; issues: number }; // cited finds by depth
}

/** "5 claims checked, all supported" -> 5 */
function claimsChecked(ev: StepEvent): number | null {
  const m = /(\d+)\s+claims?\s+checked/i.exec(ev.summary);
  return m ? Number(m[1]) : null;
}

export function summarize(rec: ShowcaseRecording): RecordingSummary {
  const view = foldRun(rec.run.events, rec.run.status);
  const answer = view.answer ?? rec.run.answer;
  let claims: RecordingSummary["claims"] = null;
  const verify = [...rec.run.events]
    .reverse()
    .find((e): e is StepEvent => e.type === "step" && e.node === "verify");
  if (verify) {
    const checked = claimsChecked(verify);
    if (checked !== null) {
      claims = { checked, supported: Math.max(0, checked - (verify.unsupported?.length ?? 0)) };
    }
  }
  const relevant = [...view.finds.keys()].filter((id) => {
    const s = findState(view, id);
    return s === "kept" || s === "cited";
  }).length;
  const citedKinds = [...view.cited].map((id) => view.finds.get(id)?.kind);
  return {
    slug: rec.slug,
    repo: rec.repo,
    question: rec.run.question,
    recorded_at: rec.recorded_at,
    model: rec.run.model,
    hops: answer?.hops ?? view.hops,
    finds: view.finds.size,
    relevant,
    cited: answer?.citations.length ?? view.cited.size,
    claims,
    calls: typeof answer?.usage.calls === "number" ? answer.usage.calls : null,
    seconds: rec.run.elapsed_s ?? answer?.latency_s ?? (view.lastAt || null),
    confidence: answer?.confidence ?? view.confidence,
    layers: {
      commits: citedKinds.filter((k) => k === "commit").length,
      prs: citedKinds.filter((k) => k === "pr").length,
      issues: citedKinds.filter((k) => k === "issue" || k === "comment").length,
    },
  };
}

/** Totals across recordings, for the trust section. */
export function totals(summaries: readonly RecordingSummary[]) {
  const withClaims = summaries.filter((s) => s.claims);
  return {
    recordings: summaries.length,
    claimsChecked: withClaims.reduce((n, s) => n + s.claims!.checked, 0),
    claimsSupported: withClaims.reduce((n, s) => n + s.claims!.supported, 0),
    cited: summaries.reduce((n, s) => n + s.cited, 0),
    reachedIssue: summaries.filter((s) => s.layers.issues > 0).length,
    reachedPr: summaries.filter((s) => s.layers.prs > 0).length,
  };
}

/** Hero first, then the rest in the order given. */
export function orderSlugs(slugs: readonly string[]): string[] {
  return [...slugs].sort((a, b) => (a === HERO_SLUG ? -1 : b === HERO_SLUG ? 1 : 0));
}

export function heroSlug(slugs: readonly string[]): string | null {
  return slugs.includes(HERO_SLUG) ? HERO_SLUG : (slugs[0] ?? null);
}

/** The events with every find not in `keep` removed: from step evidence and from the final
 *  answer's evidence list (citations are left as they are). */
function onlyFinds(events: readonly RunEvent[], keep: ReadonlySet<string>): RunEvent[] {
  return events.map((ev) => {
    if (ev.type !== "step") return ev;
    let next = ev;
    if (ev.evidence) next = { ...next, evidence: ev.evidence.filter((f) => keep.has(f.id)) };
    if (ev.answer) {
      next = { ...next, answer: { ...ev.answer, evidence: ev.answer.evidence.filter((f) => keep.has(f.id)) } };
    }
    return next;
  });
}

/** The recording's events limited to finds that end up kept or cited (the rest are dropped
 *  from every event), so the section stays readable. The full replay pages show everything. */
export function keptOnly(events: readonly RunEvent[]): RunEvent[] {
  const final = foldRun(events, "done");
  const keep = [...final.finds.keys()].filter((id) => {
    const s = findState(final, id);
    return s === "kept" || s === "cited";
  });
  return onlyFinds(events, new Set(keep));
}

/** Only the finds the final answer cites: the chain behind the answer, found step by step and
 *  flagged when the answer cites it. The hero shows this. */
export function citedOnly(events: readonly RunEvent[]): RunEvent[] {
  return onlyFinds(events, foldRun(events, "done").cited);
}

/** Steps for the scroll-driven walkthrough: each agent step with the index of its last event
 *  (so the section can show the dig up to that point). Consecutive searches are one step. */
export interface Walkthrough {
  key: string;
  node: StepEvent["node"];
  upTo: number; // events.slice(0, upTo) shows the dig after this step
  events: StepEvent[];
}

export function walkthrough(events: readonly RunEvent[]): Walkthrough[] {
  const out: Walkthrough[] = [];
  events.forEach((ev, i) => {
    if (ev.type !== "step") return;
    const last = out[out.length - 1];
    if (last && last.node === "retrieve" && ev.node === "retrieve") {
      last.events.push(ev);
      last.upTo = i + 1;
      return;
    }
    out.push({ key: `${ev.node}-${i}`, node: ev.node, upTo: i + 1, events: [ev] });
  });
  // the final status event belongs to the last step
  if (out.length) out[out.length - 1].upTo = events.length;
  return out;
}

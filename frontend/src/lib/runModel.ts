/** Fold a run's event stream into the view model the run page renders.
 *  `foldRun` is pure and idempotent: the page keeps the raw events and refolds them, so a
 *  replayed stream (reload mid-run, reconnect) can never double-apply anything. */

import type {
  AnswerPayload,
  Confidence,
  EvidenceKind,
  Find,
  Grade,
  RunEvent,
  RunStatus,
  StepEvent,
} from "../api/types";

export type LogEvent = Extract<RunEvent, { type: "log" }>;
export type TimelineItem = StepEvent | LogEvent;

/** Strata, surface first. Order is meaning: present state on top, the original need deepest. */
export const LAYERS = [
  { key: "surface", label: "Code now", kinds: ["code", "doc"] },
  { key: "commits", label: "Commits · what changed", kinds: ["commit"] },
  { key: "prs", label: "Pull requests · the goal", kinds: ["pr"] },
  { key: "issues", label: "Issues · the need", kinds: ["issue", "comment"] },
] as const satisfies readonly { key: string; label: string; kinds: readonly EvidenceKind[] }[];

export type LayerKey = (typeof LAYERS)[number]["key"];

export function layerIndex(kind: EvidenceKind): number {
  const i = LAYERS.findIndex((l) => (l.kinds as readonly string[]).includes(kind));
  return i < 0 ? 0 : i;
}

export interface RunView {
  status: RunStatus;
  error: string | null;
  timeline: TimelineItem[]; // steps and notices, in arrival order
  finds: Map<string, Find>;
  order: string[]; // find ids in order of first appearance
  accession: Map<string, number>; // find id -> 1-based accession number
  grades: Map<string, Grade>;
  kept: Set<string>;
  cited: Set<string>;
  frontier: string[];
  hops: number;
  confidence: Confidence | null;
  answer: AnswerPayload | null;
  lastAt: number; // seconds since start of the latest event
}

export function emptyView(status: RunStatus = "queued"): RunView {
  return {
    status,
    error: null,
    timeline: [],
    finds: new Map(),
    order: [],
    accession: new Map(),
    grades: new Map(),
    kept: new Set(),
    cited: new Set(),
    frontier: [],
    hops: 0,
    confidence: null,
    answer: null,
    lastAt: 0,
  };
}

/** Merge a newly reported version of a find into the one already known: a full item replaces
 *  a partial one (never the reverse), and missing fields are filled from the other. */
export function mergeFind(current: Find | undefined, next: Find): Find {
  if (!current) return next;
  if (current.partial && !next.partial) return { ...next, text: next.text ?? current.text };
  if (!current.partial && next.partial) {
    return { ...current, text: current.text ?? next.text };
  }
  return {
    ...current,
    ...next,
    url: next.url ?? current.url,
    date: next.date ?? current.date,
    text: next.text ?? current.text,
    excerpt: next.excerpt || current.excerpt,
    refs: next.refs.length ? next.refs : current.refs,
    meta: { ...current.meta, ...next.meta },
  };
}

export function foldRun(events: readonly RunEvent[], initial: RunStatus = "queued"): RunView {
  const view = emptyView(initial);
  const addFind = (find: Find) => {
    if (!view.finds.has(find.id)) {
      view.order.push(find.id);
      view.accession.set(find.id, view.order.length);
    }
    view.finds.set(find.id, mergeFind(view.finds.get(find.id), find));
  };

  for (const ev of events) {
    if ("at" in ev && typeof ev.at === "number") view.lastAt = Math.max(view.lastAt, ev.at);
    if (ev.type === "status") {
      view.status = ev.status;
      if (ev.error) view.error = ev.error;
      continue;
    }
    if (ev.type === "log") {
      view.timeline.push(ev);
      continue;
    }
    // step
    view.timeline.push(ev);
    for (const f of ev.evidence ?? []) addFind(f);
    for (const g of ev.grades ?? []) view.grades.set(g.id, g);
    if (ev.kept) view.kept = new Set(ev.kept);
    if (ev.frontier) view.frontier = ev.frontier;
    if (typeof ev.hops === "number") view.hops = ev.hops;
    if (ev.draft) view.confidence = ev.draft.confidence;
    if (ev.answer) {
      view.answer = ev.answer;
      for (const f of ev.answer.evidence) addFind(f);
      view.cited = new Set(ev.answer.citations);
      view.hops = ev.answer.hops;
      if (ev.answer.confidence) view.confidence = ev.answer.confidence;
    }
  }
  if (view.answer && view.status !== "error") view.status = "done";
  return view;
}

/** A find's relevance grade (0-3), or null while ungraded. */
export function gradeOf(view: RunView, id: string): number | null {
  const g = view.grades.get(id);
  return g ? g.score : null;
}

export type FindState = "retrieved" | "kept" | "cited" | "dropped";

export function findState(view: RunView, id: string): FindState {
  if (view.cited.has(id)) return "cited";
  if (view.kept.has(id)) return "kept";
  const grade = gradeOf(view, id);
  if (grade === null) return "retrieved";
  return grade >= 2 ? "kept" : "dropped";
}

export interface LayerView {
  key: LayerKey;
  label: string;
  shown: Find[]; // visible tags, in display order
  dropped: Find[]; // graded 0-1 and not cited: collapsed behind "+N not relevant"
}

const STATE_RANK: Record<FindState, number> = { cited: 0, kept: 1, retrieved: 2, dropped: 3 };

/** Group finds into strata. While running, keep arrival order so tags don't jump as they
 *  land; once the answer is in, cited finds lead, then kept ones by grade. */
export function layers(view: RunView): LayerView[] {
  const settled = view.answer !== null;
  return LAYERS.map((layer, i) => {
    const all = view.order
      .map((id) => view.finds.get(id)!)
      .filter((f) => layerIndex(f.kind) === i);
    const shown: Find[] = [];
    const dropped: Find[] = [];
    for (const f of all) (findState(view, f.id) === "dropped" ? dropped : shown).push(f);
    if (settled) {
      shown.sort((a, b) => {
        const sa = STATE_RANK[findState(view, a.id)];
        const sb = STATE_RANK[findState(view, b.id)];
        if (sa !== sb) return sa - sb;
        const ga = gradeOf(view, a.id) ?? -1;
        const gb = gradeOf(view, b.id) ?? -1;
        if (ga !== gb) return gb - ga;
        return view.accession.get(a.id)! - view.accession.get(b.id)!;
      });
    }
    return { key: layer.key, label: layer.label, shown, dropped };
  });
}

export function accessionLabel(n: number | undefined): string {
  return n === undefined ? "F-??" : `F-${String(n).padStart(2, "0")}`;
}

export function isTerminal(status: RunStatus): boolean {
  return status === "done" || status === "error";
}

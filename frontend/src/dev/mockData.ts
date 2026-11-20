/** Realistic fixtures for the dev mock (`?mock=1`) and the unit tests: the toy repo's
 *  "why is backoff capped at 30 seconds?" investigation, event by event. */

import type {
  AnswerPayload,
  Find,
  RepoSummary,
  Run,
  RunEvent,
  RunSummary,
  Status,
} from "../api/types";

export const MOCK_STATUS: Status = {
  provider: "gemini",
  model: "gemini-3.5-flash",
  grader_model: "gemini-3.5-flash-lite",
  llm_ready: true,
  llm_hint: null,
  github: "gh",
  allow_local_paths: true,
  version: "0.1.0",
};

const C2 = "c6d98fc00bcb20f09cbca555c1c69b297ad532cc";
const C3 = "9b1f2e7a44d0c3e1f6a8b2d5c7e9f0a1b3c5d7e9";
const C1 = "3e4d5c6b7a8f9e0d1c2b3a4f5e6d7c8b9a0f1e2d";

function find(f: Partial<Find> & Pick<Find, "id" | "kind" | "title">): Find {
  return {
    url: null,
    source: "search:code",
    date: null,
    score: 0,
    refs: [],
    excerpt: "",
    text: null,
    partial: false,
    meta: {},
    ...f,
  };
}

export const FINDS = {
  backoff: find({
    id: "code:src/pkg/retry.py#L8-L13",
    kind: "code",
    title: "backoff in src/pkg/retry.py",
    source: "search:code",
    url: "https://github.com/acme/toy/blob/1e9a5fd/src/pkg/retry.py#L8-L13",
    excerpt:
      'def backoff(attempt, jitter=True):\n    """Seconds to wait before retry number `attempt`."""\n    delay = min(2 ** attempt, MAX_BACKOFF)\n    if jitter:\n        delay = random.uniform(0, delay)\n    return delay',
    meta: { path: "src/pkg/retry.py", start_line: 8, end_line: 13, symbol: "backoff" },
  }),
  module: find({
    id: "code:src/pkg/retry.py#L1-L5",
    kind: "code",
    title: "src/pkg/retry.py (module)",
    source: "search:code",
    url: "https://github.com/acme/toy/blob/1e9a5fd/src/pkg/retry.py#L1-L5",
    excerpt: '"""Retry helpers."""\n\nimport random\n\nMAX_BACKOFF = 30',
    meta: { path: "src/pkg/retry.py", start_line: 1, end_line: 5 },
  }),
  readme: find({
    id: "doc:README.md#settings",
    kind: "doc",
    title: "README.md · Settings",
    source: "search:docs",
    excerpt: "## Settings\n\n`MAX_BACKOFF` controls the cap.",
    meta: { path: "README.md" },
  }),
  c2: find({
    id: `commit:${C2}`,
    kind: "commit",
    title: "Cap backoff at 30s to avoid huge sleeps (#12)",
    source: "search:commits",
    date: "2024-02-15T12:00:00+00:00",
    url: `https://github.com/acme/toy/commit/${C2}`,
    refs: ["pr:12", "issue:7"],
    excerpt:
      "Cap backoff at 30s to avoid huge sleeps (#12)\n\nFixes #7. Users reported multi-minute sleeps.\n\nFiles: src/pkg/retry.py",
    meta: { author: "Dev", short_sha: "c6d98fc" },
  }),
  c3: find({
    id: `commit:${C3}`,
    kind: "commit",
    title: "Add jitter to backoff (#15)",
    source: "search:commits",
    date: "2024-03-15T12:00:00+00:00",
    url: `https://github.com/acme/toy/commit/${C3}`,
    refs: ["pr:15", "gh:9"],
    excerpt: "Add jitter to backoff (#15)\n\nThundering herd when many clients retry. See #9.",
    meta: { author: "Dev", short_sha: "9b1f2e7" },
  }),
  c1: find({
    id: `commit:${C1}`,
    kind: "commit",
    title: "Add retry helper",
    source: "history",
    date: "2024-01-15T12:00:00+00:00",
    url: `https://github.com/acme/toy/commit/${C1}`,
    excerpt: "Add retry helper\n\nFiles: src/pkg/retry.py, README.md",
    meta: { author: "Dev", short_sha: "3e4d5c6" },
  }),
  pr15: find({
    id: "pr:15",
    kind: "pr",
    title: "Add jitter to backoff",
    source: "search:prs",
    date: "2024-03-14T09:30:00+00:00",
    url: "https://github.com/acme/toy/pull/15",
    excerpt: "Spread retries out so clients do not retry in lockstep.",
    partial: true,
  }),
  pr12: find({
    id: "pr:12",
    kind: "pr",
    title: "Cap backoff at 30 seconds",
    source: "fetch",
    date: "2024-02-14T16:20:00+00:00",
    url: "https://github.com/acme/toy/pull/12",
    refs: ["issue:7", `commit:${C2.slice(0, 12)}`],
    excerpt:
      "Exponential backoff grows without bound: attempt 8 already waits over four minutes. This caps the delay at 30 seconds so retries stay responsive. Fixes #7.",
    meta: { author: "maintainer" },
  }),
  issue7: find({
    id: "issue:7",
    kind: "issue",
    title: "Users reported multi-minute sleeps",
    source: "fetch",
    date: "2024-02-10T08:05:00+00:00",
    url: "https://github.com/acme/toy/issues/7",
    excerpt:
      "After a few failed requests the client sleeps for several minutes before trying again. Our job runner times out first. Can the wait be capped?",
    meta: { author: "user42" },
  }),
};

const withText = (f: Find, score: number): Find => ({ ...f, score, text: f.excerpt });

export const MOCK_ANSWER: AnswerPayload = {
  answer: [
    `The delay is capped at 30 seconds because uncapped exponential backoff produced multi-minute sleeps that users reported [issue:7]. The cap was added in [commit:${C2}] through [pr:12].`,
    "",
    "### What changed",
    "",
    "- `backoff()` returns `min(2 ** attempt, MAX_BACKOFF)` instead of `2 ** attempt` [code:src/pkg/retry.py#L8-L13].",
    "- `MAX_BACKOFF = 30` was introduced as a module constant [code:src/pkg/retry.py#L1-L5].",
    "",
    "### Why",
    "",
    "After a few failed requests the client slept for several minutes, longer than the job runner's own timeout [issue:7]. The pull request caps the wait so retries stay responsive [pr:12].",
  ].join("\n"),
  citations: ["issue:7", `commit:${C2}`, "pr:12", "code:src/pkg/retry.py#L8-L13", "code:src/pkg/retry.py#L1-L5"],
  dropped_citations: [],
  unsupported_claims: [],
  confidence: "high",
  missing: [],
  evidence: [
    withText(FINDS.c2, 3),
    withText(FINDS.pr12, 3),
    withText(FINDS.issue7, 3),
    withText(FINDS.backoff, 3),
    withText(FINDS.module, 2),
  ],
  retrieved: 9,
  hops: 1,
  usage: { calls: 6, input_tokens: 21480, output_tokens: 3912 },
  latency_s: 110.3,
  model: "gemini-3.5-flash",
};

export const DEMO_EVENTS: RunEvent[] = [
  { type: "status", status: "running", at: 0 },
  {
    type: "step",
    node: "analyze",
    at: 4.2,
    summary: "why question; targets src/pkg/retry.py:backoff; 4 queries",
    analysis: {
      question_type: "why",
      targets: [{ path: "src/pkg/retry.py", symbol: "backoff" }],
      queries: [
        { source: "code", query: "backoff MAX_BACKOFF min cap" },
        { source: "commits", query: "cap backoff 30 seconds" },
        { source: "prs", query: "backoff cap 30s" },
        { source: "docs", query: "retry delay cap" },
      ],
    },
  },
  { type: "step", node: "retrieve", at: 5.0, summary: "code: 2 hits", source: "code", query: "backoff MAX_BACKOFF min cap", evidence: [FINDS.backoff, FINDS.module] },
  { type: "step", node: "retrieve", at: 5.1, summary: "docs: 1 hit", source: "docs", query: "retry delay cap", evidence: [FINDS.readme] },
  { type: "step", node: "retrieve", at: 5.3, summary: "commits: 2 hits", source: "commits", query: "cap backoff 30 seconds", evidence: [FINDS.c2, FINDS.c3] },
  { type: "step", node: "retrieve", at: 6.0, summary: "prs: 1 hit", source: "prs", query: "backoff cap 30s", evidence: [FINDS.pr15] },
  {
    type: "step",
    node: "trace_history",
    at: 6.8,
    summary: "src/pkg/retry.py:backoff: 3 commits",
    evidence: [{ ...FINDS.c2, source: "history" }, { ...FINDS.c3, source: "history" }, FINDS.c1],
  },
  {
    type: "step",
    node: "grade",
    at: 31.5,
    summary: "graded 7: kept 3; following pr:12, issue:7",
    grades: [
      { id: FINDS.c2.id, score: 3, reason: "States the cap and the reason." },
      { id: FINDS.backoff.id, score: 3, reason: "The code in question." },
      { id: FINDS.module.id, score: 2, reason: "Defines MAX_BACKOFF." },
      { id: FINDS.readme.id, score: 1, reason: "Mentions the setting only." },
      { id: FINDS.c3.id, score: 1, reason: "Later jitter change." },
      { id: FINDS.c1.id, score: 1, reason: "Original helper, before the cap." },
      { id: FINDS.pr15.id, score: 1, reason: "About jitter, not the cap." },
    ],
    kept: [FINDS.c2.id, FINDS.backoff.id, FINDS.module.id],
    frontier: ["pr:12", "issue:7"],
  },
  { type: "step", node: "follow", at: 33.0, summary: "hop 1: fetched pr:12, issue:7", hops: 1, evidence: [FINDS.pr12, FINDS.issue7] },
  {
    type: "step",
    node: "grade",
    at: 58.4,
    summary: "graded 2: kept 5",
    grades: [
      { id: "pr:12", score: 3, reason: "Explains the cap." },
      { id: "issue:7", score: 3, reason: "The original report." },
    ],
    kept: [FINDS.c2.id, "pr:12", "issue:7", FINDS.backoff.id, FINDS.module.id],
    frontier: [],
  },
  { type: "log", level: "warning", message: "Gemini: HTTP 503 from gemini-3.5-flash; retrying in 4s (attempt 1/6)", at: 60.2 },
  {
    type: "step",
    node: "synthesize",
    at: 84.7,
    summary: "draft from 5 items, confidence high",
    draft: { confidence: "high", cited_ids: MOCK_ANSWER.citations, missing: [] },
  },
  { type: "step", node: "verify", at: 110.1, summary: "4 claims checked, all supported", unsupported: [], retrying: false },
  { type: "step", node: "finalize", at: 110.3, summary: "5 citations, 5 evidence items, 1 hop", answer: MOCK_ANSWER },
  { type: "status", status: "done", at: 110.3 },
];

export const MOCK_REPOS: RepoSummary[] = [
  {
    id: "toy-45a5e8a7",
    name: "toy",
    slug: "acme/toy",
    path: "/Users/dev/.cache/repo-archaeologist/clones/acme__toy",
    web_url: "https://github.com/acme/toy",
    added_at: new Date(Date.now() - 3 * 3600e3).toISOString(),
    indexed_at: new Date(Date.now() - 2 * 3600e3).toISOString(),
    head: "1e9a5fd",
    counts: { code: 5, doc: 3, commit: 4 },
    shallow: false,
    runs: 2,
    last_run_at: new Date(Date.now() - 25 * 60e3).toISOString(),
  },
  {
    id: "click-1a2b3c4d",
    name: "click",
    slug: "pallets/click",
    path: "/Users/dev/.cache/repo-archaeologist/clones/pallets__click",
    web_url: "https://github.com/pallets/click",
    added_at: new Date(Date.now() - 4 * 86400e3).toISOString(),
    indexed_at: new Date(Date.now() - 4 * 86400e3).toISOString(),
    head: "8f2c1d0",
    counts: { code: 1412, doc: 96, commit: 2904 },
    shallow: false,
    runs: 0,
    last_run_at: null,
  },
];

const QUESTION = "Why is the backoff delay capped at 30 seconds?";

export function mockRunSummary(id: string, status: RunSummary["status"]): RunSummary {
  const done = status === "done";
  return {
    id,
    repo_id: "toy-45a5e8a7",
    question: QUESTION,
    status,
    options: { follow_links: true, verify: true, offline: false },
    model: "gemini-3.5-flash",
    created_at: new Date(Date.now() - (done ? 26 * 60e3 : 5e3)).toISOString(),
    finished_at: done ? new Date(Date.now() - 24 * 60e3).toISOString() : null,
    elapsed_s: done ? 110.3 : null,
    confidence: done ? "high" : null,
    citations: done ? MOCK_ANSWER.citations.length : 0,
    error: null,
  };
}

export function mockRun(id: string): Run {
  if (id === "live") return { ...mockRunSummary("live", "running"), events: DEMO_EVENTS.slice(0, 1), answer: null };
  if (id === "queued") return { ...mockRunSummary("queued", "queued"), events: [], answer: null };
  if (id === "failed") {
    const error =
      "Gemini free-tier daily quota for gemini-3.5-flash is used up (limit 20/day). It resets daily; meanwhile set ARCH_MODEL / ARCH_GRADER_MODEL to another model, or enable billing.";
    return {
      ...mockRunSummary("failed", "error"),
      error,
      events: [...DEMO_EVENTS.slice(0, 7), { type: "status", status: "error", at: 31.0, error }],
      answer: null,
    };
  }
  return { ...mockRunSummary(id, "done"), events: DEMO_EVENTS, answer: MOCK_ANSWER };
}

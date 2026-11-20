/** Mock fixtures for the repository endpoints (survey, outline, code, history, suggestions,
 *  guide). The toy repo's data is exact (it matches tests/conftest.py's four commits); the
 *  "click" repo gets a deterministic synthetic file tree, large enough to exercise the map;
 *  the "fresh" repo has a single commit (thin history). Dev/test only. */

import type {
  CodeSlice,
  Guide,
  HistoryResponse,
  OutlineResponse,
  RepoSummary,
  Survey,
  SurveyFile,
  Suggestions,
} from "../api/types";
import { FINDS, MOCK_REPOS } from "./mockData";

const epoch = (iso: string) => Math.floor(Date.parse(iso) / 1000);

/** A repository with one commit: thin history. */
export const MOCK_FRESH_REPO: RepoSummary = {
  ...MOCK_REPOS[0],
  id: "acme__fresh-0a1b2c3d",
  name: "fresh",
  slug: "acme/fresh",
  path: "/Users/dev/.cache/repo-archaeologist/clones/acme__fresh",
  web_url: "https://github.com/acme/fresh",
  head: "41d38f7",
  counts: { code: 957, doc: 36, commit: 1 },
  runs: 0,
  last_run_at: null,
};

// -- survey ----------------------------------------------------------------------------

const TOY_SURVEY: Survey = {
  status: "ready",
  head: "1e9a5fd72c0357374dff9c0fed4ea2481c3f1724",
  truncated: false,
  totals: {
    files: 2,
    bytes: 1530,
    commits: 4,
    authors: 1,
    first_commit: "2024-01-15T12:00:00+00:00",
    last_commit: "2024-04-15T12:00:00+00:00",
  },
  languages: [
    { name: "Python", bytes: 1286, files: 1 },
    { name: "Markdown", bytes: 244, files: 1 },
  ],
  files: [
    ["src/pkg/retry.py", 1286, 3, epoch("2024-03-15T12:00:00Z")],
    ["README.md", 244, 2, epoch("2024-04-15T12:00:00Z")],
  ],
  months: [
    { month: "2024-01", commits: 1 },
    { month: "2024-02", commits: 1 },
    { month: "2024-03", commits: 1 },
    { month: "2024-04", commits: 1 },
  ],
  authors: [{ name: "Dev", commits: 4 }],
  hotspots: [
    { path: "src/pkg/retry.py", changes: 3, recent_changes: 0, last_change: "2024-03-15T12:00:00+00:00" },
    { path: "README.md", changes: 2, recent_changes: 0, last_change: "2024-04-15T12:00:00+00:00" },
  ],
};

/** Deterministic pseudo-random numbers (mulberry32), so the synthetic tree is stable. */
function rng(seed: number) {
  return () => {
    seed |= 0;
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function syntheticSurvey(): Survey {
  const rand = rng(7);
  const now = epoch("2026-11-01T00:00:00Z");
  const year = 365 * 86400;
  const dirs: [string, string, number][] = [
    ["src/click", ".py", 26],
    ["src/click/_compat", ".py", 6],
    ["tests", ".py", 34],
    ["docs", ".rst", 28],
    ["docs/api", ".rst", 9],
    ["examples/repo", ".py", 5],
    ["examples/complex/complex", ".py", 7],
    [".github/workflows", ".yaml", 4],
  ];
  const files: SurveyFile[] = [];
  for (const [dir, ext, n] of dirs) {
    for (let i = 0; i < n; i++) {
      const name = `${dir}/${["core", "types", "parser", "termui", "utils", "decorators", "shell", "testing", "formatting", "exceptions", "globals", "options"][i % 12]}${i >= 12 ? `_${i}` : ""}${ext}`;
      const bytes = Math.round(400 + rand() ** 2 * 90000);
      const changes = Math.round(rand() ** 3 * 400);
      const last = changes === 0 ? null : now - Math.round(rand() ** 2 * 9 * year);
      files.push([name, bytes, changes, last]);
    }
  }
  files.push(["CHANGES.rst", 120000, 860, now - 20 * 86400], ["README.md", 3100, 41, now - 200 * 86400]);
  const months = [];
  for (let y = 2014; y <= 2026; y++) {
    for (let m = 1; m <= 12 && !(y === 2026 && m > 10); m++) {
      months.push({ month: `${y}-${String(m).padStart(2, "0")}`, commits: Math.round(rand() ** 2 * 60) });
    }
  }
  const bytes = files.reduce((s, f) => s + f[1], 0);
  const hot = [...files].sort((a, b) => b[2] - a[2]).slice(0, 20);
  return {
    status: "ready",
    head: "8f2c1d0aa0e2f3b4c5d6e7f8091a2b3c4d5e6f70",
    truncated: true,
    totals: {
      files: files.length,
      bytes,
      commits: 2904,
      authors: 412,
      first_commit: "2014-04-24T09:00:00+00:00",
      last_commit: "2026-10-28T17:12:00+00:00",
    },
    languages: [
      { name: "Python", bytes: Math.round(bytes * 0.62), files: 78 },
      { name: "reStructuredText", bytes: Math.round(bytes * 0.35), files: 38 },
      { name: "YAML", bytes: Math.round(bytes * 0.03), files: 4 },
    ],
    files,
    months,
    authors: [
      { name: "David Lord", commits: 1104 },
      { name: "Armin Ronacher", commits: 802 },
      { name: "Andreas Backx", commits: 61 },
      { name: "Kevin Hu", commits: 44 },
    ],
    hotspots: hot.map(([path, , changes, last]) => ({
      path,
      changes,
      recent_changes: Math.round(changes / 9),
      last_change: new Date((last ?? now) * 1000).toISOString(),
    })),
  };
}

const FRESH_SURVEY: Survey = {
  ...syntheticSurvey(),
  head: "41d38f743bda0000000000000000000000000000",
  truncated: false,
  totals: {
    files: 214,
    bytes: 1_010_000,
    commits: 1,
    authors: 1,
    first_commit: "2026-11-20T14:50:50+00:00",
    last_commit: "2026-11-20T14:50:50+00:00",
  },
  months: [{ month: "2026-11", commits: 1 }],
  authors: [{ name: "MK Sindhu", commits: 1 }],
  hotspots: [],
};

export function mockSurvey(repoId: string): Survey {
  if (repoId === MOCK_REPOS[0].id) return TOY_SURVEY;
  if (repoId === MOCK_FRESH_REPO.id) {
    const touched = epoch("2026-11-20T14:50:50Z");
    return { ...FRESH_SURVEY, files: FRESH_SURVEY.files.map(([p, b]) => [p, b, 1, touched]) };
  }
  return syntheticSurvey();
}

// -- outline / code / history ------------------------------------------------------------

export const RETRY_SOURCE = `"""Retry helpers."""

import random

MAX_BACKOFF = 30


def backoff(attempt, jitter=True):
    """Seconds to wait before retry number \`attempt\`."""
    delay = min(2 ** attempt, MAX_BACKOFF)
    if jitter:
        delay = random.uniform(0, delay)
    return delay


class Retrier:
    def __init__(self, attempts=3):
        self.attempts = attempts

    def run(self, fn):
        for i in range(self.attempts):
            try:
                return fn()
            except Exception:
                if i == self.attempts - 1:
                    raise
`;

export function mockOutline(path: string): OutlineResponse {
  if (path === "src/pkg/retry.py") {
    return {
      status: "ready",
      path,
      language: "python",
      supported: true,
      symbols: [
        { name: "backoff", qualified: "backoff", kind: "function", start: 8, end: 13, signature: "def backoff(attempt, jitter=True):", doc: "Seconds to wait before retry number `attempt`." },
        { name: "Retrier", qualified: "Retrier", kind: "class", start: 16, end: 26, signature: "class Retrier:", doc: null },
        { name: "__init__", qualified: "Retrier.__init__", kind: "method", start: 17, end: 18, signature: "def __init__(self, attempts=3):", doc: null },
        { name: "run", qualified: "Retrier.run", kind: "method", start: 20, end: 26, signature: "def run(self, fn):", doc: null },
      ],
    };
  }
  const language = path.endsWith(".py") ? "python" : path.endsWith(".md") ? "markdown" : null;
  return { status: "ready", path, language, supported: language === "python", symbols: [] };
}

export function mockCode(path: string, start = 1, end?: number): CodeSlice {
  const lines = (path === "src/pkg/retry.py" ? RETRY_SOURCE : `# ${path}\n`).split("\n");
  if (lines[lines.length - 1] === "") lines.pop();
  const a = Math.max(1, start);
  const b = Math.min(lines.length, end ?? lines.length, a + 399);
  return {
    path,
    start: a,
    end: b,
    total_lines: lines.length,
    language: path.endsWith(".py") ? "python" : null,
    text: lines.slice(a - 1, b).join("\n"),
  };
}

export function mockHistory(path: string, symbol: string | null, resolve: boolean): HistoryResponse {
  const commits = path === "src/pkg/retry.py" ? [FINDS.c3, FINDS.c2, FINDS.c1] : [];
  return {
    status: "ready",
    path,
    symbol,
    start: symbol === "backoff" ? 8 : null,
    end: symbol === "backoff" ? 13 : null,
    resolved: resolve,
    commits: commits.map((c) => ({ ...c, source: "history" })),
    prs: commits.length ? [FINDS.pr15, FINDS.pr12, FINDS.issue7] : [],
  };
}

// -- suggestions / guide -------------------------------------------------------------------

export function mockSuggestions(repoId: string, source: "template" | "llm" = "llm"): Suggestions {
  const orientation: Suggestions["items"] = [
    { question: "What does this repository do, and how is it organised?", kind: "what", path: null, symbol: null },
    { question: "Where does a retry start, and what calls backoff()?", kind: "where", path: "src/pkg/retry.py", symbol: "backoff" },
    { question: "How does Retrier.run decide when to give up?", kind: "how", path: "src/pkg/retry.py", symbol: "Retrier.run" },
  ];
  const why: Suggestions["items"] = [
    { question: "Why does backoff() cap the delay at MAX_BACKOFF?", kind: "why", path: "src/pkg/retry.py", symbol: "backoff" },
    { question: "Why is the delay randomised with random.uniform in backoff()?", kind: "why", path: "src/pkg/retry.py", symbol: "backoff" },
  ];
  if (repoId === MOCK_FRESH_REPO.id) {
    return {
      status: "ready",
      source,
      head: FRESH_SURVEY.head,
      items: [
        { question: "What does this repository do, and how is it organised?", kind: "what", path: null, symbol: null },
        { question: "Where does execution start?", kind: "where", path: null, symbol: null },
        { question: "How do the TypeScript frontend and the Python backend talk to each other?", kind: "how", path: null, symbol: null },
      ],
    };
  }
  return {
    status: "ready",
    source,
    head: TOY_SURVEY.head,
    items: source === "template" ? orientation.slice(0, 1) : [...orientation, ...why],
  };
}

export function mockGuide(state: Guide["status"] = "ready"): Guide {
  if (state !== "ready") {
    return {
      status: state,
      head: TOY_SURVEY.head,
      markdown: null,
      citations: [],
      evidence: [],
      generated_at: null,
      model: null,
      error: state === "failed" ? "Gemini free-tier daily quota for gemini-3.5-flash is used up (limit 20/day)." : null,
      job: state === "pending" ? { id: "job-guide", kind: "guide", status: "running" } : null,
    };
  }
  return {
    status: "ready",
    head: TOY_SURVEY.head,
    markdown: [
      "## What it is",
      "A tiny Python package with one job: deciding how long to wait between retries [code:src/pkg/retry.py#L8-L13].",
      "",
      "## How it's organised",
      "- `src/pkg/retry.py` holds everything: the `backoff()` delay function and the `Retrier` class [code:src/pkg/retry.py#L1-L5].",
      "- `README.md` documents the retry settings [doc:README.md#settings].",
      "",
      "## Where to look first",
      "Start with `backoff()`: the cap and the jitter are the two decisions most changes have been about [code:src/pkg/retry.py#L8-L13].",
    ].join("\n"),
    citations: ["code:src/pkg/retry.py#L8-L13", "code:src/pkg/retry.py#L1-L5", "doc:README.md#settings"],
    evidence: [FINDS.backoff, FINDS.module, FINDS.readme].map((f) => ({ ...f, text: f.excerpt, score: 3 })),
    generated_at: new Date(Date.now() - 3600e3).toISOString(),
    model: "gemini-3.5-flash",
    error: null,
    job: null,
  };
}

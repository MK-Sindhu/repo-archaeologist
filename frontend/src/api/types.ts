/**
 * Web API contract between the FastAPI backend (src/archaeologist/web/) and this frontend.
 * The backend implements exactly these shapes; change both sides together.
 *
 * Base path: /api. JSON everywhere. Errors: HTTP 4xx/5xx with body {detail: string}
 * (a sentence meant for the user, e.g. "pallets/clik was not found on GitHub").
 *
 * Streams (Server-Sent Events): GET /api/jobs/{id}/events and GET /api/runs/{id}/events.
 * Each message is `id: <index>\ndata: <JSON event>\n\n`. A stream first replays every event
 * so far (honouring Last-Event-ID), then sends new ones live, and closes after the terminal
 * `status` event (status "done" or "error"). Clients must close their EventSource when they
 * receive a terminal status (otherwise the browser reconnects). Comment lines (": ping")
 * are sent every 15 s as keep-alives.
 */

// ---------------------------------------------------------------- status

export type Provider = "anthropic" | "gemini";

/** GET /api/status */
export interface Status {
  provider: Provider;
  model: string; // e.g. "gemini-3.5-flash"
  grader_model: string; // used for relevance grading
  llm_ready: boolean; // an API key for `provider` is configured
  llm_hint: string | null; // when !llm_ready: how to fix it, one or two sentences
  github: "token" | "gh" | "none"; // GITHUB_TOKEN set / `gh` login / unauthenticated (60 req/h)
  allow_local_paths: boolean; // whether POST /api/repos accepts local filesystem paths
  version: string;
}

// ---------------------------------------------------------------- repositories

export interface RepoCounts {
  code: number;
  doc: number;
  commit: number;
}

export interface RepoSummary {
  id: string; // stable, URL-safe, e.g. "click-1a2b3c4d"
  name: string; // "click"
  slug: string | null; // "pallets/click" when the clone has a GitHub remote
  path: string; // local clone path
  web_url: string | null; // "https://github.com/pallets/click"
  added_at: string; // ISO-8601
  indexed_at: string | null; // ISO-8601, null until the first index finished
  head: string | null; // short sha of the indexed HEAD
  counts: RepoCounts | null; // indexed chunks per kind
  shallow: boolean; // shallow clone: history is cut off (answers may be incomplete)
  runs: number; // questions asked about this repo
  last_run_at: string | null;
}

/** GET /api/repos -> RepoSummary[] (most recently used first)
 *  GET /api/repos/{id} -> RepoSummary (404 if unknown)
 *  POST /api/repos {source} -> 202 AddRepoResponse
 *     source: "owner/name", a GitHub URL (https or git@), or a local path to a git clone
 *     (local paths only when Status.allow_local_paths). GitHub repos are cloned with full
 *     history. 400 {detail} when the source cannot be understood or is not a git repo.
 *  POST /api/repos/{id}/reindex -> 202 {job: JobRef} (pulls if cloned, then re-indexes)
 *  DELETE /api/repos/{id} -> 204 (removes it from the list; files are kept) */
export interface AddRepoRequest {
  source: string;
}
export interface AddRepoResponse {
  repo_id: string;
  job: JobRef;
}

// ---------------------------------------------------------------- jobs (clone + index)

export type JobStatus = "queued" | "running" | "done" | "error";

export interface JobRef {
  id: string;
  kind: "add_repo" | "reindex";
  status: JobStatus;
}

/** GET /api/jobs/{id} */
export interface Job extends JobRef {
  repo_id: string;
  error: string | null;
  events: JobEvent[];
}

export type JobEvent =
  /** stage: "clone" | "pull" | "files" | "commits" | "embed"; total null when unknown */
  | { type: "progress"; stage: string; done: number; total: number | null; message: string }
  | { type: "log"; level: "info" | "warning" | "error"; message: string }
  /** terminal when status is "done" (repo is the refreshed summary) or "error" */
  | { type: "status"; status: JobStatus; error?: string | null; repo?: RepoSummary | null };

// ---------------------------------------------------------------- runs (questions)

export type RunStatus = "queued" | "running" | "done" | "error";
export type Confidence = "high" | "medium" | "low";
export type EvidenceKind = "code" | "commit" | "pr" | "issue" | "doc" | "comment";

export interface RunOptions {
  follow_links: boolean; // follow commit -> PR -> issue links (default true)
  verify: boolean; // check each claim against the evidence (default true)
  offline: boolean; // local git only; no GitHub calls (default false)
}

/** POST /api/runs -> 202 RunSummary (status "queued" or "running").
 *  400 {detail} for an empty question or unknown repo; 503 {detail: llm_hint} when no
 *  LLM key is configured. */
export interface CreateRunRequest extends Partial<RunOptions> {
  repo_id: string;
  question: string;
}

/** GET /api/runs?repo_id=<id>&limit=<n> -> RunSummary[] (newest first) */
export interface RunSummary {
  id: string;
  repo_id: string;
  question: string;
  status: RunStatus;
  options: RunOptions;
  model: string;
  created_at: string; // ISO-8601
  finished_at: string | null;
  elapsed_s: number | null;
  confidence: Confidence | null;
  citations: number; // number of cited evidence items (0 until done)
  error: string | null;
}

/** GET /api/runs/{id} -> Run (everything needed to render a finished or running run) */
export interface Run extends RunSummary {
  events: RunEvent[];
  answer: AnswerPayload | null;
}

/** One piece of evidence ("find"). Ids are canonical refs used as citations in answers:
 *  "commit:<sha>", "pr:<n>", "issue:<n>", "code:<path>#L<a>-L<b>", "doc:<path>#<slug>". */
export interface Find {
  id: string;
  kind: EvidenceKind;
  title: string;
  url: string | null; // GitHub link when known
  source: string; // which step produced it: "search:code", "history", "fetch", ...
  date: string | null; // ISO-8601
  score: number; // retrieval score in step events; 0-3 relevance grade in AnswerPayload
  refs: string[]; // canonical refs this item links to (commit -> "pr:12", PR -> "issue:7")
  excerpt: string; // first ~600 chars of the text, for cards
  text: string | null; // full text (<= 6000 chars) — only in AnswerPayload.evidence
  partial: boolean; // a PR/issue known only from search (no discussion fetched yet)
  meta: {
    path?: string;
    start_line?: number;
    end_line?: number;
    symbol?: string;
    author?: string;
    short_sha?: string;
    shallow_boundary?: boolean;
  };
}

export interface Grade {
  id: string;
  score: 0 | 1 | 2 | 3; // 3 explains the why, 2 relevant context, 1 tangential, 0 irrelevant
  reason: string;
}

export type NodeName =
  | "analyze" // plan: question type, code targets, one search query per source
  | "retrieve" // one search (runs in parallel, one event per source)
  | "trace_history" // git history of the targeted code
  | "grade" // relevance grades; picks links to follow
  | "follow" // fetches linked commits / PRs / issues (one hop)
  | "synthesize" // drafts the cited answer
  | "verify" // checks each claim against the evidence
  | "finalize"; // final answer

export interface StepEvent {
  type: "step";
  node: NodeName;
  at: number; // seconds since the run started
  summary: string; // one human-readable line, e.g. "4 claims checked, all supported"
  // present depending on the node:
  analysis?: {
    question_type: string;
    targets: { path: string | null; symbol: string | null }[];
    queries: { source: string; query: string }[];
  };
  source?: string; // retrieve: "code" | "docs" | "commits" | "prs"
  query?: string; // retrieve
  evidence?: Find[]; // items this step added (retrieve, trace_history, follow)
  grades?: Grade[]; // grade
  kept?: string[]; // grade: ids kept so far (score >= 2), best first
  frontier?: string[]; // grade: refs that the next hop will fetch
  hops?: number; // follow: hops done so far
  draft?: { confidence: Confidence; cited_ids: string[]; missing: string[] }; // synthesize
  unsupported?: string[]; // verify: claims the evidence did not support
  retrying?: boolean; // verify: another retrieve round follows
  answer?: AnswerPayload; // finalize
}

export type RunEvent =
  | { type: "status"; status: RunStatus; at: number; error?: string | null }
  | StepEvent
  /** backend notices while running, e.g. "Gemini: HTTP 429; retrying in 37s" */
  | { type: "log"; level: "info" | "warning" | "error"; message: string; at: number };

export interface AnswerPayload {
  answer: string; // markdown; cites evidence inline as [<find id>], e.g. [commit:c6d98fc…]
  citations: string[]; // ids cited inline, in order of first citation
  dropped_citations: string[]; // ids the model invented (removed from the text)
  unsupported_claims: string[];
  confidence: Confidence | null;
  missing: string[]; // parts of the question the evidence does not establish
  evidence: Find[]; // kept evidence (score = 0-3 grade), with full text
  retrieved: number; // how many items were retrieved in total
  hops: number;
  usage: { calls?: number; input_tokens?: number; output_tokens?: number };
  latency_s: number | null;
  model: string;
}

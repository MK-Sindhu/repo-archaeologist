/** Typed client for the web API (see ./types.ts). Requests go to VITE_API_URL (or the same
 *  origin) and carry the site's access code when one is stored: as the X-Access-Code header,
 *  or as the `access` query parameter on event streams. With `?mock=1` (remembered for the
 *  tab) every call is served by the in-browser mock in ../dev/mock.ts instead. */

import { mockApi, mockCheckAccess, mockSubscribe } from "../dev/mock";
import { access } from "./access";
import { apiBase, apiHost, buildUrl } from "./config";
import { ApiError } from "./errors";
import type {
  AddRepoResponse,
  CodeSlice,
  CreateRunRequest,
  Guide,
  HistoryResponse,
  Job,
  JobEvent,
  JobRef,
  RepoSummary,
  Run,
  RunEvent,
  RunSummary,
  Status,
  Suggestions,
  SurveyResponse,
  OutlineResponse,
} from "./types";

export { ApiError, isNotApi, isUnreachable } from "./errors";

/** What to tell the person when the API address answers with something else (usually the
 *  website itself: on Vercel without VITE_API_URL, "/api/..." is the website's own host). */
export function notApiMessage(base = apiBase()): string {
  const host = apiHost(base);
  if (!host) {
    return (
      "This website isn't connected to its API: /api answered with a web page. If the website " +
      "is on Vercel, set VITE_API_URL to your API's address (e.g. " +
      "https://repo-archaeologist-api.onrender.com) under Settings → Environment Variables, " +
      "then redeploy."
    );
  }
  return (
    `The address in VITE_API_URL (${host}) didn't answer like the Repo Archaeologist API. Check ` +
    "that it is your API's address, without /api at the end, then redeploy the website."
  );
}

type UnauthorizedListener = (detail: string) => void;
const unauthorizedListeners = new Set<UnauthorizedListener>();

/** Called with the server's detail whenever a request is refused for a missing/wrong code. */
export function onUnauthorized(fn: UnauthorizedListener): () => void {
  unauthorizedListeners.add(fn);
  return () => unauthorizedListeners.delete(fn);
}

let mockFlag: boolean | null = null;

export function isMock(): boolean {
  if (mockFlag !== null) return mockFlag;
  let on = new URLSearchParams(window.location.search).get("mock") === "1";
  try {
    if (on) sessionStorage.setItem("arch-mock", "1");
    else on = sessionStorage.getItem("arch-mock") === "1";
  } catch {
    /* storage unavailable: the URL flag still works for this page */
  }
  mockFlag = on;
  return on;
}

interface RequestOptions {
  code?: string | null; // send this access code instead of the stored one
  quiet401?: boolean; // do not tell the app about a 401 (the caller handles it)
}

async function request<T>(method: string, path: string, body?: unknown, opts: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const code = opts.code !== undefined ? opts.code : access.get();
  if (code) headers["X-Access-Code"] = code;
  let resp: Response;
  try {
    resp = await fetch(buildUrl(apiBase(), path), {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "Cannot reach the Repo Archaeologist server.");
  }
  if (resp.status === 204) return undefined as T;
  const isJson = (resp.headers.get("content-type") ?? "").includes("application/json");
  if (!isJson && ![502, 503, 504].includes(resp.status)) {
    // a 502/503/504 page is a host waking up or briefly down: left to isUnreachable
    throw new ApiError(resp.status, notApiMessage(), null, true);
  }
  let data: unknown = null;
  try {
    data = await resp.json();
  } catch {
    /* non-JSON body */
  }
  if (!resp.ok) {
    const sent =
      data && typeof data === "object" && "detail" in data && typeof data.detail === "string"
        ? data.detail
        : null;
    const reason = resp.statusText ? ` (${resp.statusText})` : "";
    const message = sent ?? `The server answered with an error ${resp.status}${reason}.`;
    if (resp.status === 401 && !opts.quiet401) unauthorizedListeners.forEach((fn) => fn(message));
    throw new ApiError(resp.status, message, sent);
  }
  return data as T;
}

const http = {
  status: () => request<Status>("GET", "/status"),
  repos: () => request<RepoSummary[]>("GET", "/repos"),
  repo: (id: string) => request<RepoSummary>("GET", `/repos/${encodeURIComponent(id)}`),
  addRepo: (source: string) => request<AddRepoResponse>("POST", "/repos", { source }),
  reindex: (id: string) =>
    request<{ job: JobRef }>("POST", `/repos/${encodeURIComponent(id)}/reindex`),
  deleteRepo: (id: string) => request<void>("DELETE", `/repos/${encodeURIComponent(id)}`),
  job: (id: string) => request<Job>("GET", `/jobs/${encodeURIComponent(id)}`),
  runs: (repoId?: string, limit = 50) => {
    const q = new URLSearchParams({ limit: String(limit) });
    if (repoId) q.set("repo_id", repoId);
    return request<RunSummary[]>("GET", `/runs?${q}`);
  },
  run: (id: string) => request<Run>("GET", `/runs/${encodeURIComponent(id)}`),
  createRun: (req: CreateRunRequest) => request<RunSummary>("POST", "/runs", req),
  suggestions: (id: string) => request<Suggestions>("GET", `${repoPath(id)}/suggestions`),
  refreshSuggestions: (id: string) => request<Suggestions>("POST", `${repoPath(id)}/suggestions`),
  survey: (id: string) => request<SurveyResponse>("GET", `${repoPath(id)}/survey`),
  outline: (id: string, path: string) =>
    request<OutlineResponse>("GET", `${repoPath(id)}/outline?${query({ path })}`),
  code: (id: string, path: string, start?: number, end?: number) =>
    request<CodeSlice>("GET", `${repoPath(id)}/code?${query({ path, start, end })}`),
  history: (id: string, q: HistoryQuery) =>
    request<HistoryResponse>(
      "GET",
      `${repoPath(id)}/history?${query({ ...q, resolve: q.resolve ? 1 : undefined })}`,
    ),
  guide: (id: string) => request<Guide>("GET", `${repoPath(id)}/guide`),
  writeGuide: (id: string) => request<{ job: JobRef }>("POST", `${repoPath(id)}/guide`),
};

export interface HistoryQuery {
  path: string;
  symbol?: string | null;
  start?: number | null;
  end?: number | null;
  resolve?: boolean; // also ask GitHub for each commit's pull request (uses GitHub quota)
}

function repoPath(id: string): string {
  return `/repos/${encodeURIComponent(id)}`;
}

/** Query string from defined, non-null values. */
function query(values: Record<string, string | number | boolean | null | undefined>): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(values)) if (v !== undefined && v !== null && v !== "") q.set(k, String(v));
  return q.toString();
}

export type Api = typeof http;

/** Check an access code against an endpoint that needs it, without storing it. Throws
 *  ApiError (401 when the code is wrong). */
export async function checkAccess(code: string): Promise<void> {
  if (isMock()) return mockCheckAccess(code);
  await request<unknown>("GET", "/repos", undefined, { code, quiet401: true });
}

export const api: Api = new Proxy(http, {
  get(target, key: keyof Api) {
    return isMock() ? mockApi[key] : target[key];
  },
});

export interface StreamHandlers<E> {
  /** Called for every event with its stream index (replayed ones included). */
  onEvent: (event: E, index: number) => void;
  /** Called once if the stream breaks for good. */
  onError?: (message: string) => void;
}

function isTerminalEvent(ev: { type: string; status?: string }): boolean {
  return ev.type === "status" && (ev.status === "done" || ev.status === "error");
}

/** Follow an SSE stream. Closes itself after the terminal status event; returns a cleanup
 *  function for unmount. */
export function subscribe<E extends RunEvent | JobEvent>(
  path: string,
  handlers: StreamHandlers<E>,
): () => void {
  if (isMock()) return mockSubscribe(path, handlers as StreamHandlers<RunEvent | JobEvent>);
  const source = new EventSource(buildUrl(apiBase(), path, access.get()));
  let fallback = 0;
  let failures = 0;
  source.onmessage = (msg) => {
    failures = 0;
    let event: E;
    try {
      event = JSON.parse(msg.data) as E;
    } catch {
      return;
    }
    const index = msg.lastEventId === "" ? fallback : Number(msg.lastEventId);
    fallback = index + 1;
    handlers.onEvent(event, index);
    if (isTerminalEvent(event)) source.close();
  };
  source.onerror = () => {
    failures += 1;
    // the browser retries on its own; give up after repeated failures or a hard close
    if (source.readyState === EventSource.CLOSED || failures >= 5) {
      source.close();
      handlers.onError?.("Lost the connection to the server. Reload the page to catch up.");
    }
  };
  return () => source.close();
}

export const streams = {
  run: (id: string, h: StreamHandlers<RunEvent>) =>
    subscribe<RunEvent>(`/runs/${encodeURIComponent(id)}/events`, h),
  job: (id: string, h: StreamHandlers<JobEvent>) =>
    subscribe<JobEvent>(`/jobs/${encodeURIComponent(id)}/events`, h),
};

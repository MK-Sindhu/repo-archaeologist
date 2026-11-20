/** Typed client for the web API (see ./types.ts). With `?mock=1` (remembered for the tab)
 *  every call is served by the in-browser mock in ../dev/mock.ts instead. */

import { mockApi, mockSubscribe } from "../dev/mock";
import type {
  AddRepoResponse,
  CreateRunRequest,
  Job,
  JobEvent,
  JobRef,
  RepoSummary,
  Run,
  RunEvent,
  RunSummary,
  Status,
} from "./types";

export class ApiError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
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

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  let resp: Response;
  try {
    resp = await fetch(`/api${path}`, {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "Cannot reach the Archaeologist server. Is it still running?");
  }
  if (resp.status === 204) return undefined as T;
  let data: unknown = null;
  try {
    data = await resp.json();
  } catch {
    /* non-JSON body */
  }
  if (!resp.ok) {
    const detail =
      data && typeof data === "object" && "detail" in data && typeof data.detail === "string"
        ? data.detail
        : `The server answered ${resp.status} ${resp.statusText}.`;
    throw new ApiError(resp.status, detail);
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
};

export type Api = typeof http;

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
  const source = new EventSource(`/api${path}`);
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

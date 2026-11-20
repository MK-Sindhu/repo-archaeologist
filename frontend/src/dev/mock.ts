/** In-browser stand-in for the backend, enabled with `?mock=1`. Serves the toy-repo fixtures:
 *  /run/demo (finished), /run/live (streams the same investigation on a timer),
 *  /run/queued, /run/failed; opening any repository streams a fake clone + index job. */

import type { Api, StreamHandlers } from "../api/client";
import type { JobEvent, RepoSummary, RunEvent, RunSummary } from "../api/types";
import {
  DEMO_EVENTS,
  MOCK_REPOS,
  MOCK_STATUS,
  mockRun,
  mockRunSummary,
} from "./mockData";

const delay = <T>(value: T, ms = 120) => new Promise<T>((r) => setTimeout(() => r(value), ms));

const repos: RepoSummary[] = [...MOCK_REPOS];
const runs: RunSummary[] = [mockRunSummary("demo", "done"), mockRunSummary("failed", "error")];
runs[1] = { ...runs[1], created_at: new Date(Date.now() - 50 * 60e3).toISOString(), error: mockRun("failed").error };

const jobRepo = new Map<string, string>();

export const mockApi: Api = {
  status: () => delay(MOCK_STATUS),
  repos: () => delay(repos),
  repo: async (id) => {
    const r = repos.find((x) => x.id === id);
    if (!r) throw Object.assign(new Error("No repository with that id."), { status: 404 });
    return delay(r);
  },
  addRepo: async (source) => {
    const name = source.replace(/\.git$/, "").split(/[/:]/).filter(Boolean).pop() ?? "repo";
    const existing = repos.find((r) => r.name === name);
    const id = existing?.id ?? `${name}-0a1b2c3d`;
    if (!existing) {
      repos.unshift({ ...MOCK_REPOS[1], id, name, slug: source.includes("/") ? source : null, runs: 0 });
    }
    const jobId = `job-${id}`;
    jobRepo.set(jobId, id);
    return delay({ repo_id: id, job: { id: jobId, kind: "add_repo", status: "running" } });
  },
  reindex: async (id) => {
    const jobId = `job-${id}`;
    jobRepo.set(jobId, id);
    return delay({ job: { id: jobId, kind: "reindex", status: "running" } });
  },
  deleteRepo: async () => delay(undefined),
  job: async (id) => delay({ id, kind: "add_repo", status: "running", repo_id: jobRepo.get(id) ?? "", error: null, events: [] }),
  runs: async (repoId) => delay(runs.filter((r) => !repoId || r.repo_id === repoId)),
  run: async (id) => delay(mockRun(id)),
  createRun: async (req) => {
    const run = { ...mockRunSummary("live", "running"), question: req.question, repo_id: req.repo_id };
    runs.unshift(run);
    return delay(run);
  },
};

function jobScript(repoId: string): { event: JobEvent; wait: number }[] {
  const repo = repos.find((r) => r.id === repoId) ?? MOCK_REPOS[0];
  const steps: { event: JobEvent; wait: number }[] = [
    { event: { type: "progress", stage: "clone", done: 0, total: null, message: "Cloning" }, wait: 300 },
  ];
  for (let i = 1; i <= 4; i++) {
    steps.push({ event: { type: "progress", stage: "files", done: i * 120, total: 480, message: "" }, wait: 350 });
  }
  for (let i = 1; i <= 3; i++) {
    steps.push({ event: { type: "progress", stage: "commits", done: i * 900, total: 2904, message: "" }, wait: 350 });
  }
  steps.push({ event: { type: "progress", stage: "embed", done: 1508, total: 1508, message: "" }, wait: 400 });
  steps.push({ event: { type: "status", status: "done", repo: { ...repo, indexed_at: new Date().toISOString() } }, wait: 300 });
  return steps;
}

export function mockSubscribe(path: string, h: StreamHandlers<RunEvent | JobEvent>): () => void {
  let timers: ReturnType<typeof setTimeout>[] = [];
  const play = (script: { event: RunEvent | JobEvent; wait: number }[]) => {
    let t = 0;
    script.forEach(({ event, wait }, i) => {
      t += wait;
      timers.push(setTimeout(() => h.onEvent(event, i), t));
    });
  };
  const run = /^\/runs\/([^/]+)\/events$/.exec(path);
  const job = /^\/jobs\/([^/]+)\/events$/.exec(path);
  if (run) {
    const id = decodeURIComponent(run[1]);
    if (id === "live") {
      // replay at ~1 event per 0.9-1.6s so the dig can be watched
      play(DEMO_EVENTS.map((event, i) => ({ event, wait: i === 0 ? 50 : event.type === "step" && event.node === "retrieve" ? 500 : 1300 })));
    } else {
      play(mockRun(id).events.map((event) => ({ event, wait: 0 })));
    }
  } else if (job) {
    play(jobScript(jobRepo.get(decodeURIComponent(job[1])) ?? MOCK_REPOS[0].id));
  }
  return () => {
    timers.forEach(clearTimeout);
    timers = [];
  };
}

/** /app/run/:id — loads a run, follows its live events, and renders it with RunView. */

import { useCallback, useEffect, useMemo, useState } from "react";
import { api, streams } from "../api/client";
import type { Run as RunData, RunEvent, Status } from "../api/types";
import { RunView } from "../components/RunView";
import { useDocumentTitle, useNow } from "../hooks";
import { clock, duration, since } from "../lib/format";
import { foldRun, isTerminal } from "../lib/runModel";
import { Link, navigate, paths } from "../router";
import { NotFound } from "./NotFound";

export function Run({ id, status }: { id: string; status: Status | null }) {
  const [run, setRun] = useState<RunData | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [missing, setMissing] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [streamError, setStreamError] = useState<string | null>(null);
  const [repoName, setRepoName] = useState<string | null>(null);
  const [selected, setSelectedState] = useState<string | null>(
    () => new URLSearchParams(window.location.search).get("find"),
  );
  // the open find is part of the URL (?find=<id>), so it can be linked and survives reloads
  const setSelected = useCallback((fid: string | null) => {
    setSelectedState(fid);
    const url = new URL(window.location.href);
    if (fid) url.searchParams.set("find", fid);
    else url.searchParams.delete("find");
    window.history.replaceState(null, "", url.pathname + url.search);
  }, []);
  const [retrying, setRetrying] = useState(false);

  const load = useCallback(() => {
    return api
      .run(id)
      .then((r) => {
        setRun(r);
        setEvents((prev) => (prev.length > r.events.length ? prev : r.events));
        return r;
      })
      .catch((e) => {
        if (e?.status === 404) setMissing(true);
        else setLoadError(e.message);
        return null;
      });
  }, [id]);

  useEffect(() => {
    setRun(null);
    setEvents([]);
    setStreamError(null);
    let stop: (() => void) | undefined;
    let live = true;
    load().then((r) => {
      if (!live || !r || isTerminal(r.status)) return;
      stop = streams.run(id, {
        onEvent(ev, index) {
          setEvents((prev) => {
            if (index < prev.length) return prev; // replayed
            return [...prev, ev];
          });
          if (ev.type === "status" && isTerminal(ev.status)) load();
        },
        onError: setStreamError,
      });
    });
    return () => {
      live = false;
      stop?.();
    };
  }, [id, load]);

  useEffect(() => {
    if (!run) return;
    api
      .repo(run.repo_id)
      .then((r) => setRepoName(r.name))
      .catch(() => setRepoName(null));
  }, [run?.repo_id]);

  const view = useMemo(() => foldRun(events, run?.status ?? "queued"), [events, run?.status]);
  const running = view.status === "running" || view.status === "queued";
  const now = useNow(running);
  useDocumentTitle(run?.question ?? "");

  if (missing) return <NotFound what="question" />;
  if (loadError) {
    return (
      <main className="container run">
        <p className="error-text">{loadError}</p>
      </main>
    );
  }
  if (!run) return <main className="container run" aria-busy="true" />;

  const elapsed = running
    ? since(run.created_at, now)
    : (run.elapsed_s ?? view.answer?.latency_s ?? view.lastAt);
  const sinceLast = running ? Math.max(0, since(run.created_at, now) - view.lastAt) : undefined;

  const askAgain = async () => {
    setRetrying(true);
    try {
      const next = await api.createRun({ repo_id: run.repo_id, question: run.question, ...run.options });
      navigate(paths.run(next.id));
    } catch (e) {
      setRetrying(false);
      setStreamError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <main className="container">
      <RunView
        view={view}
        question={run.question}
        model={run.model}
        options={run.options}
        elapsedText={running ? clock(elapsed) : duration(elapsed)}
        selected={selected}
        onSelect={setSelected}
        sinceLast={sinceLast}
        slowNote={status?.provider === "gemini" ? "Free-tier Gemini calls take about 20–40 seconds each." : null}
        crumbs={
          <nav className="crumbs" aria-label="Breadcrumb">
            <Link to={paths.repo(run.repo_id)}>{repoName ?? "Repository"}</Link>
          </nav>
        }
        failure={
          <div className="failure" role="alert">
            <p className="failure__title">This question could not be answered.</p>
            <p className="failure__msg">{view.error ?? run.error ?? "The run stopped with an error."}</p>
            <button type="button" className="btn btn-primary" onClick={askAgain} disabled={retrying}>
              Ask again
            </button>
          </div>
        }
        notice={streamError && <p className="error-text">{streamError}</p>}
      />
    </main>
  );
}

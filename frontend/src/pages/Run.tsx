import { useCallback, useEffect, useMemo, useState } from "react";
import { api, streams } from "../api/client";
import type { Confidence, Run as RunData, RunEvent, Status } from "../api/types";
import { AnswerBody } from "../components/Answer";
import { FieldLog } from "../components/FieldLog";
import { FindDrawer } from "../components/FindDrawer";
import { Section } from "../components/Section";
import { useDocumentTitle, useNow } from "../hooks";
import { clock, duration, num, plural, since } from "../lib/format";
import { edges as edgesOf } from "../lib/links";
import { findState, foldRun, gradeOf, isTerminal, layers as layersOf } from "../lib/runModel";
import { Link, navigate, paths } from "../router";
import { NotFound } from "./NotFound";

const CONFIDENCE_LEVEL: Record<Confidence, number> = { low: 1, medium: 2, high: 3 };
const STATUS_TEXT = { queued: "Waiting", running: "Digging", done: "Answered", error: "Failed" } as const;

export function Run({ id, status }: { id: string; status: Status | null }) {
  const [run, setRun] = useState<RunData | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [missing, setMissing] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [streamError, setStreamError] = useState<string | null>(null);
  const [repoName, setRepoName] = useState<string | null>(null);
  const [highlight, setHighlight] = useState<string | null>(null);
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
  const layers = useMemo(() => layersOf(view), [view]);
  // lines only between finds that are on show (irrelevant ones start collapsed)
  const edges = useMemo(
    () => edgesOf([...view.finds.values()].filter((f) => findState(view, f.id) !== "dropped")),
    [view],
  );
  const stateOf = useCallback((fid: string) => findState(view, fid), [view]);
  const gradeFor = useCallback((fid: string) => gradeOf(view, fid), [view]);
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
  const answer = view.answer;
  const done = view.status === "done" && answer !== null;
  const counts = {
    found: view.finds.size,
    kept: [...view.finds.keys()].filter((f) => stateOf(f) === "kept" || stateOf(f) === "cited").length,
    cited: view.cited.size,
  };
  const emptyText = (key: string) => {
    if (running) return "Nothing yet";
    if (run.options.offline && (key === "prs" || key === "issues")) return "Not searched (offline)";
    return "Nothing found at this depth";
  };
  const selectedFind = selected ? view.finds.get(selected) : undefined;

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

  const log = (
    <FieldLog
      timeline={view.timeline}
      running={running}
      sinceLast={sinceLast}
      slowNote={status?.provider === "gemini" ? "Free-tier Gemini calls take about 20–40 seconds each." : null}
    />
  );

  return (
    <main className="container run">
      <nav className="crumbs" aria-label="Breadcrumb">
        <Link to={paths.repo(run.repo_id)}>{repoName ?? "Repository"}</Link>
      </nav>
      <h1 className="run__q">{run.question}</h1>
      <p className="run__meta">
        <span className={`run__status is-${view.status}`}>
          {running && <span className="live-dot" aria-hidden />}
          {STATUS_TEXT[view.status]}
        </span>
        <span className="mono">{running ? clock(elapsed) : duration(elapsed)}</span>
        <span className="mono muted">{run.model}</span>
        {!run.options.follow_links && <span className="muted">links not followed</span>}
        {!run.options.verify && <span className="muted">claims not checked</span>}
        {run.options.offline && <span className="muted">offline</span>}
      </p>

      <div className={done ? "run__grid is-done" : "run__grid"}>
        <div className="run__dig">
          <p className="run__dig-head">
            <span className="eyebrow">Evidence by depth</span>
            <span className="muted">
              {plural(counts.found, "find")} · {counts.kept} relevant
              {done && <> · {counts.cited} cited</>}
            </span>
          </p>
          <Section
            label="Evidence by depth"
            layers={layers}
            stateOf={stateOf}
            gradeOf={gradeFor}
            accession={view.accession}
            edges={edges}
            cited={view.cited}
            highlight={highlight}
            selected={selected}
            onHover={setHighlight}
            onSelect={setSelected}
            emptyText={emptyText}
          />
        </div>

        <div className="run__report">
          {view.status === "queued" && <p className="note">Waiting for the previous question to finish.</p>}
          {view.status === "error" && (
            <div className="failure" role="alert">
              <p className="failure__title">This question could not be answered.</p>
              <p className="failure__msg">{view.error ?? run.error ?? "The run stopped with an error."}</p>
              <button type="button" className="btn btn-primary" onClick={askAgain} disabled={retrying}>
                Ask again
              </button>
            </div>
          )}
          {streamError && <p className="error-text">{streamError}</p>}

          {done && answer && (
            <article className="answer" aria-label="Answer">
              <AnswerBody
                markdown={answer.answer}
                finds={view.finds}
                accession={view.accession}
                highlight={highlight}
                onHover={setHighlight}
                onSelect={setSelected}
              />
              <div className="answer__notes">
                {answer.confidence && (
                  <p className={`confidence is-${answer.confidence}`}>
                    <span className="grade" aria-hidden>
                      {[1, 2, 3].map((i) => (
                        <i key={i} className={i <= CONFIDENCE_LEVEL[answer.confidence!] ? "on" : undefined} />
                      ))}
                    </span>
                    {answer.confidence[0].toUpperCase() + answer.confidence.slice(1)} confidence
                  </p>
                )}
                {answer.missing.length > 0 && (
                  <div className="answer__gaps">
                    <p className="eyebrow">Not established by the evidence</p>
                    <ul>
                      {answer.missing.map((m) => (
                        <li key={m}>{m}</li>
                      ))}
                    </ul>
                  </div>
                )}
                {answer.unsupported_claims.length > 0 && (
                  <div className="answer__gaps is-unverified">
                    <p className="eyebrow">Unverified claims</p>
                    <p className="muted">The check could not find support for these in the cited evidence:</p>
                    <ul>
                      {answer.unsupported_claims.map((c) => (
                        <li key={c}>{c}</li>
                      ))}
                    </ul>
                  </div>
                )}
                {answer.dropped_citations.length > 0 && (
                  <p className="muted answer__dropped">
                    Removed {plural(answer.dropped_citations.length, "citation")} to evidence that was never
                    retrieved.
                  </p>
                )}
              </div>
              <p className="answer__facts">
                {plural(answer.citations.length, "source")} cited · {plural(answer.hops, "hop")}
                {typeof answer.usage.calls === "number" && <> · {plural(answer.usage.calls, "LLM call")}</>}
                {(answer.usage.input_tokens ?? 0) + (answer.usage.output_tokens ?? 0) > 0 && (
                  <> · {num((answer.usage.input_tokens ?? 0) + (answer.usage.output_tokens ?? 0))} tokens</>
                )}
                {answer.latency_s !== null && <> · {duration(answer.latency_s)}</>}
              </p>
            </article>
          )}

          {done ? (
            <details className="how">
              <summary>How this answer was found</summary>
              {log}
            </details>
          ) : (
            view.status !== "queued" && (
              <section aria-labelledby="log-title">
                <h2 id="log-title" className="eyebrow run__log-title">
                  Field log
                </h2>
                {log}
              </section>
            )
          )}
        </div>
      </div>

      {selectedFind && (
        <FindDrawer
          find={selectedFind}
          number={view.accession.get(selectedFind.id)}
          state={stateOf(selectedFind.id)}
          grade={view.grades.get(selectedFind.id)}
          finds={view.finds}
          accession={view.accession}
          onSelect={setSelected}
          onClose={() => setSelected(null)}
        />
      )}
    </main>
  );
}

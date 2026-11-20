/** Ask tab: suggested questions, the question box with its options, and past questions. */

import { type FormEvent, type KeyboardEvent, type RefObject, useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../../api/client";
import type { RunOptions, RunSummary, Suggestion, Suggestions } from "../../api/types";
import { duration, plural, relativeTime } from "../../lib/format";
import { Link, navigate, paths } from "../../router";

const STATUS_TEXT: Record<RunSummary["status"], string> = {
  queued: "Waiting",
  running: "Digging",
  done: "Answered",
  error: "Failed",
};

interface Props {
  repoId: string;
  question: string;
  setQuestion: (q: string) => void;
  inputRef: RefObject<HTMLTextAreaElement | null>;
  thinHistory: boolean;
}

export function AskTab({ repoId, question, setQuestion, inputRef, thinHistory }: Props) {
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [options, setOptions] = useState<RunOptions>({ follow_links: true, verify: true, offline: false });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadRuns = useCallback(() => {
    api.runs(repoId).then(setRuns).catch(() => setRuns((r) => r ?? []));
  }, [repoId]);
  useEffect(loadRuns, [loadRuns]);
  const active = runs?.some((r) => r.status === "running" || r.status === "queued");
  useEffect(() => {
    if (!active) return;
    const t = setInterval(loadRuns, 5000);
    return () => clearInterval(t);
  }, [active, loadRuns]);

  const ask = async (e?: FormEvent) => {
    e?.preventDefault();
    const q = question.trim();
    if (!q) {
      setError("Type a question first, or pick one of the suggestions.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const run = await api.createRun({ repo_id: repoId, question: q, ...options });
      navigate(paths.run(run.id));
    } catch (err) {
      setBusy(false);
      setError(err instanceof ApiError || err instanceof Error ? err.message : String(err));
    }
  };
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) ask();
  };
  const toggle = (key: keyof RunOptions) => setOptions((o) => ({ ...o, [key]: !o[key] }));
  const pick = (s: Suggestion) => {
    setQuestion(s.question);
    setError(null);
    requestAnimationFrame(() => {
      const el = inputRef.current;
      if (!el) return;
      el.focus();
      el.setSelectionRange(el.value.length, el.value.length);
    });
  };

  return (
    <>
      <form className="ask" onSubmit={ask}>
        <label htmlFor="question" className="ask__label">
          Ask why the code is the way it is
        </label>
        <SuggestionChips repoId={repoId} onPick={pick} hideWhy={thinHistory} />
        <textarea
          id="question"
          ref={inputRef}
          className="field ask__input"
          rows={3}
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={onKey}
          placeholder={
            thinHistory ? "What does this repository do, and how is it organised?" : "Why does backoff() cap the delay at 30 seconds?"
          }
          disabled={busy}
        />
        <div className="ask__row">
          <fieldset className="ask__options">
            <legend className="visually-hidden">Options</legend>
            <label className="check">
              <input type="checkbox" checked={options.follow_links} onChange={() => toggle("follow_links")} />
              Follow links to pull requests and issues
            </label>
            <label className="check">
              <input type="checkbox" checked={options.verify} onChange={() => toggle("verify")} />
              Check every claim
            </label>
            <label className="check">
              <input type="checkbox" checked={options.offline} onChange={() => toggle("offline")} />
              Offline (local git only)
            </label>
          </fieldset>
          <button type="submit" className="btn btn-primary ask__go" disabled={busy}>
            Ask
          </button>
        </div>
        {error && <p className="error-text">{error}</p>}
      </form>

      <section className="past" aria-labelledby="past-title">
        <h2 id="past-title" className="eyebrow">
          Past questions
        </h2>
        {runs && runs.length === 0 && (
          <p className="muted past__empty">No questions yet. Ask one above; the first takes a few minutes.</p>
        )}
        {runs && runs.length > 0 && (
          <ul className="past__list">
            {runs.map((r) => (
              <li key={r.id}>
                <Link to={paths.run(r.id)} className="past__row">
                  <span className="past__q">{r.question}</span>
                  <span className="past__meta">
                    <span className={`past__status is-${r.status}`}>
                      {(r.status === "running" || r.status === "queued") && <span className="live-dot" aria-hidden />}
                      {STATUS_TEXT[r.status]}
                    </span>
                    {r.confidence && <span>{r.confidence} confidence</span>}
                    {r.status === "done" && <span>{plural(r.citations, "source")}</span>}
                    {r.elapsed_s !== null && <span className="mono">{duration(r.elapsed_s)}</span>}
                    <span className="muted">{relativeTime(r.created_at)}</span>
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>
    </>
  );
}

const ROWS: { title: string; kinds: Suggestion["kind"][] }[] = [
  { title: "Find your way around", kinds: ["what", "how", "where"] },
  { title: "Why is it like this?", kinds: ["why"] },
];

/** Suggested questions: ready at once as templates, rewritten by the LLM in the background
 *  (polled while pending). Picking one fills the question box; it never asks by itself. */
function SuggestionChips({
  repoId,
  onPick,
  hideWhy,
}: {
  repoId: string;
  onPick: (s: Suggestion) => void;
  hideWhy: boolean;
}) {
  const [data, setData] = useState<Suggestions | null>(null);

  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = () =>
      api
        .suggestions(repoId)
        .then((s) => {
          if (!live) return;
          setData(s);
          if (s.status === "pending") timer = setTimeout(load, 4000);
        })
        .catch(() => live && setData((d) => d));
    load();
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [repoId]);

  if (!data) return null;
  const rows = ROWS.filter((r) => !(hideWhy && r.kinds.includes("why")))
    .map((r) => ({ ...r, items: data.items.filter((s) => r.kinds.includes(s.kind)) }))
    .filter((r) => r.items.length > 0);
  if (!rows.length && data.status !== "pending") return null;

  return (
    <div className="suggest" aria-label="Suggested questions">
      {rows.map((row) => (
        <div key={row.title} className="suggest__row">
          <p className="suggest__title">{row.title}</p>
          <ul className="suggest__list">
            {row.items.map((s) => (
              <li key={s.question}>
                <button type="button" className="qchip" onClick={() => onPick(s)}>
                  {s.question}
                </button>
              </li>
            ))}
          </ul>
        </div>
      ))}
      {data.status === "pending" && (
        <p className="suggest__pending muted" role="status">
          <span className="live-dot" aria-hidden /> Writing suggestions…
        </p>
      )}
    </div>
  );
}

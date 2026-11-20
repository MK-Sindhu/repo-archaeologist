import { ArrowUpRight, RefreshCw, TriangleAlert } from "lucide-react";
import { type FormEvent, type KeyboardEvent, useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import type { RepoSummary, RunOptions, RunSummary } from "../api/types";
import { JobProgress } from "../components/JobProgress";
import { useDocumentTitle } from "../hooks";
import { duration, num, plural, relativeTime } from "../lib/format";
import { Link, navigate, paths } from "../router";
import { NotFound } from "./NotFound";

const STATUS_TEXT: Record<RunSummary["status"], string> = {
  queued: "Waiting",
  running: "Digging",
  done: "Answered",
  error: "Failed",
};

export function Repo({ id }: { id: string }) {
  const [repo, setRepo] = useState<RepoSummary | null>(null);
  const [missing, setMissing] = useState(false);
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [question, setQuestion] = useState("");
  const [options, setOptions] = useState<RunOptions>({ follow_links: true, verify: true, offline: false });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  useDocumentTitle(repo?.name ?? "");

  useEffect(() => {
    setRepo(null);
    setMissing(false);
    api
      .repo(id)
      .then(setRepo)
      .catch((e) => (e?.status === 404 ? setMissing(true) : setError(e.message)));
  }, [id]);

  const loadRuns = useCallback(() => {
    api.runs(id).then(setRuns).catch(() => setRuns((r) => r ?? []));
  }, [id]);
  useEffect(loadRuns, [loadRuns]);
  const active = runs?.some((r) => r.status === "running" || r.status === "queued");
  useEffect(() => {
    if (!active) return;
    const t = setInterval(loadRuns, 5000);
    return () => clearInterval(t);
  }, [active, loadRuns]);

  if (missing) return <NotFound what="repository" />;

  const ask = async (e?: FormEvent) => {
    e?.preventDefault();
    const q = question.trim();
    if (!q) {
      setError("Type a question first.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const run = await api.createRun({ repo_id: id, question: q, ...options });
      navigate(paths.run(run.id));
    } catch (err) {
      setBusy(false);
      setError(err instanceof ApiError || err instanceof Error ? err.message : String(err));
    }
  };
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) ask();
  };
  const reindex = async () => {
    setError(null);
    try {
      const res = await api.reindex(id);
      setJobId(res.job.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };
  const toggle = (key: keyof RunOptions) => setOptions((o) => ({ ...o, [key]: !o[key] }));

  return (
    <main className="container repo">
      <nav className="crumbs" aria-label="Breadcrumb">
        <Link to={paths.home()}>Repositories</Link>
      </nav>
      <header className="repo__head">
        <div>
          <h1 className="display repo__name">{repo?.name ?? " "}</h1>
          {repo && (
            <p className="repo__facts">
              {repo.web_url ? (
                <a href={repo.web_url} target="_blank" rel="noreferrer" className="mono">
                  {repo.slug} <ArrowUpRight size={13} aria-hidden />
                </a>
              ) : (
                <span className="mono">{repo.path}</span>
              )}
              {repo.indexed_at ? (
                <span className="muted">
                  indexed {relativeTime(repo.indexed_at)}
                  {repo.head && (
                    <>
                      {" at "}
                      <span className="mono">{repo.head}</span>
                    </>
                  )}
                </span>
              ) : (
                <span className="muted">not indexed yet</span>
              )}
              {repo.counts && (
                <span className="muted">
                  {num(repo.counts.code)} code · {num(repo.counts.doc)} docs · {num(repo.counts.commit)} commits
                </span>
              )}
            </p>
          )}
        </div>
        <button type="button" className="btn btn-secondary btn-small" onClick={reindex} disabled={!!jobId}>
          <RefreshCw size={14} aria-hidden /> Re-index
        </button>
      </header>
      {repo?.shallow && (
        <p className="note repo__warn">
          <TriangleAlert size={14} aria-hidden /> This is a shallow clone, so its history is cut off and
          answers may stop short. Run <span className="mono">git fetch --unshallow</span> in it, then
          re-index.
        </p>
      )}
      {jobId && (
        <JobProgress
          jobId={jobId}
          onDone={(r) => {
            setJobId(null);
            if (r) setRepo(r);
          }}
          onError={() => setJobId(null)}
        />
      )}

      <form className="ask" onSubmit={ask}>
        <label htmlFor="question" className="ask__label">
          Ask why the code is the way it is
        </label>
        <textarea
          id="question"
          className="field ask__input"
          rows={3}
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={onKey}
          placeholder="Why does backoff() cap the delay at 30 seconds?"
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
    </main>
  );
}

/** /app — open a repository, or go back to one opened before. (The public landing page sells
 *  the product; this page just gets to work.) */

import { ArrowRight } from "lucide-react";
import { type FormEvent, useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import type { RepoSummary, Status } from "../api/types";
import { JobProgress } from "../components/JobProgress";
import { useDocumentTitle } from "../hooks";
import { plural, relativeTime } from "../lib/format";
import { Link, navigate, paths } from "../router";

const EXAMPLES = ["pallets/click", "encode/httpx", "pallets/markupsafe"];

export function Home({ status }: { status: Status | null }) {
  useDocumentTitle("Repositories");
  const [source, setSource] = useState("");
  const [busy, setBusy] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [repos, setRepos] = useState<RepoSummary[] | null>(null);

  useEffect(() => {
    api.repos().then(setRepos).catch(() => setRepos([]));
  }, []);

  const local = !status || status.allow_local_paths;
  const placeholder = local ? "pallets/click, a GitHub URL, or a local path" : "pallets/click or a GitHub URL";

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const value = source.trim();
    if (!value) {
      setError("Enter a repository first, for example pallets/click.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const res = await api.addRepo(value);
      setJobId(res.job.id);
    } catch (err) {
      setBusy(false);
      setError(err instanceof ApiError || err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <main className="container apphome">
      <h1 className="display apphome__title">Open a repository</h1>
      <p className="apphome__lede">
        {local ? "Name a GitHub repository, paste its URL, or give a local path." : "Name a GitHub repository or paste its URL."}{" "}
        It's cloned with its full history and indexed; then ask why its code is the way it is, or explore its map.
      </p>

      <form className="open" onSubmit={submit} noValidate>
        <label htmlFor="source" className="open__label">
          Repository
        </label>
        <div className="open__row">
          <input
            id="source"
            className="field"
            value={source}
            onChange={(e) => setSource(e.target.value)}
            placeholder={placeholder}
            autoComplete="off"
            spellCheck={false}
            disabled={busy}
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? "source-error" : undefined}
          />
          <button type="submit" className="btn btn-primary" disabled={busy}>
            Open repository
          </button>
        </div>
        {error && (
          <p id="source-error" className="error-text open__error">
            {error}
          </p>
        )}
        {jobId && (
          <JobProgress
            jobId={jobId}
            onDone={(repo) => repo && navigate(paths.repo(repo.id))}
            onError={() => {
              setBusy(false);
              setJobId(null);
            }}
          />
        )}
      </form>

      <section className="recent" aria-labelledby="recent-title">
        <h2 id="recent-title" className="eyebrow">
          Recent repositories
        </h2>
        {repos === null ? null : repos.length === 0 ? (
          <div className="recent__empty">
            <p className="muted">Nothing opened yet. Start with a project you know, or one of these:</p>
            <div className="recent__examples">
              {EXAMPLES.map((ex) => (
                <button key={ex} type="button" className="btn btn-secondary btn-small mono-btn" onClick={() => setSource(ex)}>
                  {ex}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <ul className="recent__list">
            {repos.map((r) => (
              <li key={r.id}>
                <Link to={paths.repo(r.id)} className="recent__row">
                  <span className="recent__name">{r.name}</span>
                  <span className="recent__slug mono">{r.slug ?? r.path}</span>
                  <span className="recent__meta muted">
                    {r.indexed_at ? `indexed ${relativeTime(r.indexed_at)}` : "not indexed yet"}
                    {r.counts && <> · {plural(r.counts.commit, "commit")}</>}
                    {" · "}
                    {plural(r.runs, "question")}
                  </span>
                  <ArrowRight size={16} className="recent__go" aria-hidden />
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>
    </main>
  );
}

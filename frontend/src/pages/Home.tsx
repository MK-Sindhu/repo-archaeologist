import { ArrowRight } from "lucide-react";
import { type FormEvent, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api/client";
import type { Find, RepoSummary, Status } from "../api/types";
import { FINDS } from "../dev/mockData";
import { JobProgress } from "../components/JobProgress";
import { Section } from "../components/Section";
import { useDocumentTitle } from "../hooks";
import { plural, relativeTime } from "../lib/format";
import { edges as edgesOf } from "../lib/links";
import { type LayerView, LAYERS } from "../lib/runModel";
import { Link, navigate, paths } from "../router";

// The thesis, drawn with the example repository's real history: one line of code on the
// surface, the reason for it three layers down.
const HERO: Find[] = [
  {
    ...FINDS.backoff,
    title: "return min(2 ** attempt, MAX_BACKOFF)",
    refs: [FINDS.c2.id],
    meta: { path: "src/pkg/retry.py", start_line: 10, end_line: 10 },
  },
  { ...FINDS.c2, refs: ["pr:12"] },
  { ...FINDS.pr12, refs: ["issue:7"] },
  { ...FINDS.issue7, refs: [] },
];
const HERO_LAYERS: LayerView[] = LAYERS.map((l, i) => ({
  key: l.key,
  label: l.label,
  shown: [HERO[i]],
  dropped: [],
}));
const HERO_ACCESSION = new Map(HERO.map((f, i) => [f.id, [3, 7, 11, 12][i]]));
const HERO_CITED = new Set(["issue:7"]);
const HERO_MONO = new Set([HERO[0].id]);
const EXAMPLES = ["pallets/click", "encode/httpx"];

export function Home({ status }: { status: Status | null }) {
  useDocumentTitle("");
  const [source, setSource] = useState("");
  const [busy, setBusy] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [repos, setRepos] = useState<RepoSummary[] | null>(null);
  const heroEdges = useMemo(() => edgesOf(HERO), []);

  useEffect(() => {
    api.repos().then(setRepos).catch(() => setRepos([]));
  }, []);

  const placeholder =
    status && !status.allow_local_paths
      ? "pallets/click or a GitHub URL"
      : "pallets/click, a GitHub URL, or a local path";

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
    <main className="home">
      <div className="container home__grid">
        <div className="home__intro">
          <h1 className="display home__title">Every line of code was written for a&nbsp;reason.</h1>
          <p className="home__lede">
            Ask why. Archaeologist traces the code back through its commits, pull requests and
            issues, and cites everything it finds.
          </p>

          <form className="open" onSubmit={submit} noValidate>
            <label htmlFor="source" className="open__label">
              Open a repository
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
        </div>

        <figure className="home__figure">
          <Section
            label="Example: why backoff() is capped, from the code down to the issue"
            layers={HERO_LAYERS}
            stateOf={(id) => (HERO_CITED.has(id) ? "cited" : "kept")}
            gradeOf={() => null}
            accession={HERO_ACCESSION}
            edges={heroEdges}
            cited={HERO_CITED}
            monoIds={HERO_MONO}
            intro
            staircase
          />
          <figcaption className="home__caption muted">
            One question, excavated: the line as it is today, the commit that changed it, the pull
            request that proposed it, and the issue that asked for it.
          </figcaption>
        </figure>
      </div>
    </main>
  );
}

/** /app/repo/:id — a repository: what it is (facts), and three tabs: Ask (suggested
 *  questions, the question box, past questions), Map (a newcomer's survey: site plan, history,
 *  hotspots, file panels with biographies) and Guide (a cited field guide, on request). */

import { ArrowUpRight, Info, RefreshCw, TriangleAlert } from "lucide-react";
import { type KeyboardEvent, lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { repoSlugFromId } from "../api/repoId";
import type { RepoSummary } from "../api/types";
import { JobProgress } from "../components/JobProgress";
import { useDocumentTitle } from "../hooks";
import { num, plural, relativeTime } from "../lib/format";
import { Link, navigate, paths } from "../router";
import { NotFound } from "./NotFound";
import { AskTab } from "./repo/AskTab";
import { Reopen } from "./repo/Reopen";
import { type RepoTab, TABS, useRepoView } from "./repo/urlState";

const MapTab = lazy(() => import("./repo/MapTab"));
const GuideTab = lazy(() => import("./repo/GuideTab"));

const TAB_TEXT: Record<RepoTab, string> = { ask: "Ask", map: "Map", guide: "Guide" };
const THIN = 3; // commits; at or below this there is little history to dig through

export function Repo({ id }: { id: string }) {
  const [repo, setRepo] = useState<RepoSummary | null>(null);
  const [missing, setMissing] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [question, setQuestion] = useState("");
  const [view, setView] = useRepoView();
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const tabRefs = useRef(new Map<RepoTab, HTMLButtonElement>());
  const [focusAsk, setFocusAsk] = useState(0);
  useDocumentTitle(repo?.name ?? "");

  const loadRepo = useCallback(() => {
    setMissing(false);
    setLoadError(null);
    api
      .repo(id)
      .then(setRepo)
      .catch((e) => (e?.status === 404 ? setMissing(true) : setLoadError(e.message)));
  }, [id]);
  useEffect(() => {
    setRepo(null);
    loadRepo();
  }, [loadRepo]);

  // "Ask why" from the map: switch to Ask, then focus the filled-in box
  useEffect(() => {
    if (!focusAsk || view.tab !== "ask") return;
    const el = inputRef.current;
    if (!el) return;
    el.focus();
    el.setSelectionRange(el.value.length, el.value.length);
  }, [focusAsk, view.tab]);
  const askWhy = useCallback(
    (q: string) => {
      setQuestion(q);
      setView({ tab: "ask", file: null });
      setFocusAsk((n) => n + 1);
    },
    [setView],
  );

  if (missing) {
    const slug = repoSlugFromId(id);
    if (!slug) return <NotFound what="repository" />;
    return (
      <Reopen
        slug={slug}
        onReopened={(r) => {
          if (r && r.id !== id) navigate(paths.repo(r.id), { replace: true });
          else loadRepo();
        }}
      />
    );
  }

  const reindex = async () => {
    setError(null);
    try {
      const res = await api.reindex(id);
      setJobId(res.job.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const commits = repo?.counts?.commit ?? null;
  const thin = commits !== null && commits <= THIN;

  const onTabKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const i = TABS.indexOf(view.tab);
    let next: RepoTab | null = null;
    if (e.key === "ArrowRight") next = TABS[(i + 1) % TABS.length];
    else if (e.key === "ArrowLeft") next = TABS[(i - 1 + TABS.length) % TABS.length];
    else if (e.key === "Home") next = TABS[0];
    else if (e.key === "End") next = TABS[TABS.length - 1];
    if (!next) return;
    e.preventDefault();
    setView({ tab: next });
    tabRefs.current.get(next)?.focus();
  };

  return (
    <main className="container repo">
      <nav className="crumbs" aria-label="Breadcrumb">
        <Link to={paths.app()}>Repositories</Link>
      </nav>
      <header className="repo__head">
        <div>
          <h1 className="display repo__name">{repo?.name ?? " "}</h1>
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
                  {num(repo.counts.code)} code · {plural(repo.counts.doc, "doc")} · {plural(repo.counts.commit, "commit")}
                </span>
              )}
            </p>
          )}
        </div>
        <button type="button" className="btn btn-secondary btn-small" onClick={reindex} disabled={!!jobId || !repo}>
          <RefreshCw size={14} aria-hidden /> Re-index
        </button>
      </header>
      {loadError && <p className="error-text">{loadError}</p>}
      {error && <p className="error-text">{error}</p>}
      {repo?.shallow && (
        <p className="note repo__warn">
          <TriangleAlert size={14} aria-hidden /> This is a shallow clone, so its history is cut off and answers may
          stop short. Run <span className="mono">git fetch --unshallow</span> in it, then re-index.
        </p>
      )}
      {thin && commits !== null && (
        <p className="note repo__thin">
          <Info size={14} aria-hidden />
          <span>
            This repository has {commits === 0 ? "no commits" : plural(commits, "commit")}, so there's little history to
            dig through. Answers will explain the code as it is now.
          </span>
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

      <div className="tabs" role="tablist" aria-label="Repository views" onKeyDown={onTabKey}>
        {TABS.map((t) => (
          <button
            key={t}
            ref={(el) => {
              if (el) tabRefs.current.set(t, el);
            }}
            type="button"
            role="tab"
            id={`tab-${t}`}
            aria-selected={view.tab === t}
            aria-controls={`panel-${t}`}
            tabIndex={view.tab === t ? 0 : -1}
            className={view.tab === t ? "tabs__tab is-on" : "tabs__tab"}
            onClick={() => setView({ tab: t })}
          >
            {TAB_TEXT[t]}
          </button>
        ))}
      </div>

      {TABS.map((t) => (
        <div
          key={t}
          id={`panel-${t}`}
          role="tabpanel"
          aria-labelledby={`tab-${t}`}
          className="tabpanel"
          hidden={view.tab !== t}
        >
          {t === "ask" && view.tab === "ask" && (
            <AskTab repoId={id} question={question} setQuestion={setQuestion} inputRef={inputRef} thinHistory={thin} />
          )}
          {t !== "ask" && view.tab === t && repo && (
            <Suspense fallback={<p className="muted map__msg" aria-busy="true" />}>
              {t === "map" ? (
                <MapTab repo={repo} view={view} setView={setView} onAskWhy={askWhy} />
              ) : (
                <GuideTab repo={repo} />
              )}
            </Suspense>
          )}
        </div>
      ))}
    </main>
  );
}

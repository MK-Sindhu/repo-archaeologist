/** Guide tab: a field guide for newcomers, written on request (never automatically), cached
 *  per repository version. Every claim cites the code or docs it rests on, with the same
 *  find tags as answers. Loaded lazily with the tab. */

import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../../api/client";
import type { Guide, RepoSummary } from "../../api/types";
import { AnswerBody } from "../../components/Answer";
import { FindDrawer } from "../../components/FindDrawer";
import { KIND_LABEL } from "../../components/FindTag";
import { JobProgress } from "../../components/JobProgress";
import { relativeTime } from "../../lib/format";
import { accessionLabel } from "../../lib/runModel";

export default function GuideTab({ repo }: { repo: RepoSummary }) {
  const [guide, setGuide] = useState<Guide | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [highlight, setHighlight] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoadError(null);
    return api
      .guide(repo.id)
      .then((g) => {
        setGuide(g);
        if (g.status === "pending" && g.job) setJobId(g.job.id);
        return g;
      })
      .catch((e) => {
        setLoadError(e instanceof Error ? e.message : String(e));
        return null;
      });
  }, [repo.id]);

  useEffect(() => {
    load();
  }, [load]);

  // pending without a job to follow (e.g. after a reload): poll until it settles
  useEffect(() => {
    if (guide?.status !== "pending" || jobId) return;
    const t = setTimeout(load, 4000);
    return () => clearTimeout(t);
  }, [guide, jobId, load]);

  const write = async () => {
    setStarting(true);
    setActionError(null);
    try {
      const res = await api.writeGuide(repo.id);
      setJobId(res.job.id);
      setGuide((g) => (g ? { ...g, status: "pending", job: res.job } : g));
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    } finally {
      setStarting(false);
    }
  };

  const finds = useMemo(() => new Map((guide?.evidence ?? []).map((f) => [f.id, f])), [guide]);
  const accession = useMemo(() => new Map((guide?.evidence ?? []).map((f, i) => [f.id, i + 1])), [guide]);
  const cited = useMemo(() => new Set(guide?.citations ?? []), [guide]);

  if (loadError) return <p className="error-text guide__msg">{loadError}</p>;
  if (!guide) return <p className="muted guide__msg" aria-busy="true" />;

  const writing = guide.status === "pending" || jobId !== null;
  const selectedFind = selected ? finds.get(selected) : undefined;

  return (
    <div className="guide">
      {writing && (
        <div className="guide__writing">
          <p className="guide__lede">Writing the field guide. It reads the README, the folders and the entry points, then checks every claim against the code.</p>
          {jobId ? (
            <JobProgress
              jobId={jobId}
              onDone={() => {
                setJobId(null);
                load();
              }}
              onError={(message) => {
                setJobId(null);
                setActionError(message);
                load();
              }}
            />
          ) : (
            <p className="muted" role="status">
              <span className="live-dot" aria-hidden /> Writing…
            </p>
          )}
        </div>
      )}

      {!writing && guide.status === "missing" && (
        <div className="guide__empty">
          <h2 className="guide__title">A field guide for newcomers</h2>
          <p className="guide__lede">
            A short overview of {repo.name}: what it is, how it's organised, the key ideas, where execution starts,
            how to run and test it, and where to look first. Every claim cites the code or docs it comes from.
          </p>
          <p className="muted guide__cost">
            It's written once for this version of the repository and kept. Writing it takes a few minutes and a
            handful of LLM calls.
          </p>
          <button type="button" className="btn btn-primary" onClick={write} disabled={starting}>
            Write the field guide
          </button>
        </div>
      )}

      {!writing && guide.status === "failed" && (
        <div className="failure" role="alert">
          <p className="failure__title">The field guide could not be written.</p>
          {guide.error && <p className="failure__msg">{guide.error}</p>}
          <button type="button" className="btn btn-primary" onClick={write} disabled={starting}>
            Try again
          </button>
        </div>
      )}

      {actionError && <p className="error-text">{actionError}</p>}

      {!writing && guide.status === "ready" && guide.markdown && (
        <div className="guide__grid">
          <article className="answer guide__body" aria-label={`Field guide to ${repo.name}`}>
            <AnswerBody
              markdown={guide.markdown}
              finds={finds}
              accession={accession}
              highlight={highlight}
              onHover={setHighlight}
              onSelect={setSelected}
            />
            <p className="answer__facts">
              Written {relativeTime(guide.generated_at)}
              {guide.model && (
                <>
                  {" "}
                  with <span className="mono">{guide.model}</span>
                </>
              )}
              {" · "}
              <button type="button" className="linkish" onClick={write} disabled={starting}>
                Write it again
              </button>
            </p>
          </article>
          {guide.evidence.length > 0 && (
            <aside className="guide__sources" aria-labelledby="sources-title">
              <h3 id="sources-title" className="eyebrow">
                Sources
              </h3>
              <ol className="sources">
                {guide.evidence.map((f) => (
                  <li key={f.id}>
                    <button
                      type="button"
                      className={[
                        "sources__item",
                        cited.has(f.id) && "is-cited",
                        highlight === f.id && "is-highlighted",
                      ]
                        .filter(Boolean)
                        .join(" ")}
                      onMouseEnter={() => setHighlight(f.id)}
                      onMouseLeave={() => setHighlight(null)}
                      onFocus={() => setHighlight(f.id)}
                      onBlur={() => setHighlight(null)}
                      onClick={() => setSelected(f.id)}
                    >
                      <span className="sources__no mono">{accessionLabel(accession.get(f.id))}</span>
                      <span className="sources__title">{f.title}</span>
                      <span className="sources__kind">{KIND_LABEL[f.kind]}</span>
                    </button>
                  </li>
                ))}
              </ol>
            </aside>
          )}
        </div>
      )}

      {selectedFind && (
        <FindDrawer
          find={selectedFind}
          number={accession.get(selectedFind.id)}
          state={cited.has(selectedFind.id) ? "cited" : "kept"}
          grade={undefined}
          finds={finds}
          accession={accession}
          onSelect={setSelected}
          onClose={() => setSelected(null)}
        />
      )}
    </div>
  );
}

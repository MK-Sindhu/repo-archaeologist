/** Shown when a GitHub repository's id is no longer known to the server (free hosting plans
 *  forget everything when they restart): offers to open the same repository again. */

import { useState } from "react";
import { api } from "../../api/client";
import type { RepoSummary } from "../../api/types";
import { JobProgress } from "../../components/JobProgress";
import { useDocumentTitle } from "../../hooks";
import { Link, paths } from "../../router";

export function Reopen({ slug, onReopened }: { slug: string; onReopened: (repo: RepoSummary | null) => void }) {
  useDocumentTitle(slug);
  const [jobId, setJobId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const reopen = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await api.addRepo(slug);
      setJobId(res.job.id);
    } catch (e) {
      setBusy(false);
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <main className="container reopen">
      <nav className="crumbs" aria-label="Breadcrumb">
        <Link to={paths.app()}>Repositories</Link>
      </nav>
      <h1 className="display reopen__title">{slug}</h1>
      <p className="reopen__text">
        This repository isn't open on the server any more (free servers forget them when they restart). Opening it
        again clones and indexes it from GitHub; past questions about it are gone.
      </p>
      <p>
        <button type="button" className="btn btn-primary" onClick={reopen} disabled={busy}>
          Open {slug} again
        </button>
      </p>
      {error && <p className="error-text">{error}</p>}
      {jobId && (
        <JobProgress
          jobId={jobId}
          onDone={onReopened}
          onError={() => {
            setBusy(false);
            setJobId(null);
          }}
        />
      )}
    </main>
  );
}

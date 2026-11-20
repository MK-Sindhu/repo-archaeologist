/** Live progress of a job (clone + index, or writing a field guide), from its event stream. */

import { useEffect, useState } from "react";
import { streams } from "../api/client";
import type { JobEvent, RepoSummary } from "../api/types";
import { num } from "../lib/format";

type Progress = Extract<JobEvent, { type: "progress" }>;
type Notice = Extract<JobEvent, { type: "log" }>;

function stageText(p: Progress): string {
  const of = (label: string) =>
    p.total ? `${label} ${num(p.done)}/${num(p.total)}` : p.done ? `${label} ${num(p.done)}` : label;
  switch (p.stage) {
    case "clone":
      return "Cloning (full history)";
    case "pull":
      return "Updating the clone";
    case "files":
      return of("Indexing files");
    case "commits":
      return of("Indexing commits");
    case "embed":
      return of("Embedding");
    case "survey":
      return "Surveying the history";
    case "suggest":
      return "Writing suggestions";
    case "gather":
      return "Gathering the repository's facts";
    case "write":
      return "Writing the guide";
    case "check":
      return "Checking every claim";
    default:
      return p.message || p.stage;
  }
}

interface Props {
  jobId: string;
  onDone: (repo: RepoSummary | null) => void;
  onError?: (message: string) => void;
}

export function JobProgress({ jobId, onDone, onError }: Props) {
  const [progress, setProgress] = useState<Progress | null>(null);
  const [notices, setNotices] = useState<Notice[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let seen = -1;
    const stop = streams.job(jobId, {
      onEvent(ev, index) {
        if (index <= seen) return;
        seen = index;
        if (ev.type === "progress") setProgress(ev);
        else if (ev.type === "log") setNotices((n) => [...n, ev]);
        else if (ev.status === "done") onDone(ev.repo ?? null);
        else if (ev.status === "error") {
          const message = ev.error ?? "The job failed.";
          setError(message);
          onError?.(message);
        }
      },
      onError(message) {
        setError(message);
        onError?.(message);
      },
    });
    return stop;
  }, [jobId]);

  const ratio = progress?.total ? Math.min(1, progress.done / progress.total) : null;
  return (
    <div className="job" role="status" aria-live="polite">
      {error ? (
        <p className="error-text">{error}</p>
      ) : (
        <>
          <p className="job__stage">
            <span className="live-dot" aria-hidden /> {progress ? stageText(progress) : "Starting"}
          </p>
          <div className={ratio === null ? "job__bar is-indeterminate" : "job__bar"} aria-hidden>
            <span style={ratio === null ? undefined : { width: `${ratio * 100}%` }} />
          </div>
        </>
      )}
      {notices.map((n, i) => (
        <p key={i} className={n.level === "error" ? "error-text" : "job__notice muted"}>
          {n.message}
        </p>
      ))}
    </div>
  );
}

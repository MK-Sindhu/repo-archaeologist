/** The investigation as a dated log: each agent step in order, timed from the start of the
 *  run, plus backend notices (retries, rate limits). */

import type { StepEvent } from "../api/types";
import { clock, refLabel } from "../lib/format";
import type { TimelineItem } from "../lib/runModel";

const SOURCE_LABEL: Record<string, string> = {
  code: "code",
  docs: "docs",
  commits: "commits",
  prs: "pull requests",
};

function stepTitle(ev: StepEvent): string {
  switch (ev.node) {
    case "analyze":
      return "Plan";
    case "retrieve":
      return `Search ${SOURCE_LABEL[ev.source ?? ""] ?? ev.source ?? ""}`.trim();
    case "trace_history":
      return "Trace history";
    case "grade":
      return "Grade evidence";
    case "follow":
      return ev.hops ? `Follow links · hop ${ev.hops}` : "Follow links";
    case "synthesize":
      return "Draft answer";
    case "verify":
      return "Check claims";
    case "finalize":
      return "Done";
  }
}

function StepDetail({ ev }: { ev: StepEvent }) {
  if (ev.node === "analyze" && ev.analysis) {
    const targets = ev.analysis.targets
      .map((t) => [t.path, t.symbol].filter(Boolean).join(" · "))
      .filter(Boolean);
    return (
      <>
        <p>
          A <strong>{ev.analysis.question_type}</strong> question
          {targets.length > 0 && (
            <>
              {" "}about <span className="mono">{targets.join(", ")}</span>
            </>
          )}
          .
        </p>
        {ev.analysis.queries.length > 0 && (
          <ul className="log__queries">
            {ev.analysis.queries.map((q) => (
              <li key={q.source}>
                <span className="log__src">{SOURCE_LABEL[q.source] ?? q.source}</span>
                <span className="mono">{q.query}</span>
              </li>
            ))}
          </ul>
        )}
      </>
    );
  }
  if (ev.node === "retrieve") {
    const n = ev.evidence?.length ?? 0;
    return (
      <p>
        {n === 0 ? "Nothing found" : `${n} found`}
        {ev.query && (
          <>
            {" · "}
            <span className="mono">{ev.query}</span>
          </>
        )}
      </p>
    );
  }
  if (ev.node === "grade") {
    return (
      <p>
        {ev.kept ? `${ev.kept.length} kept as relevant` : ev.summary}
        {ev.frontier && ev.frontier.length > 0 && (
          <>
            {"; following "}
            <span className="mono">{ev.frontier.map(refLabel).join(", ")}</span>
          </>
        )}
      </p>
    );
  }
  if (ev.node === "follow" && ev.evidence) {
    return (
      <p>
        {ev.evidence.length ? (
          <>
            Fetched <span className="mono">{ev.evidence.map((f) => refLabel(f.id)).join(", ")}</span>
          </>
        ) : (
          ev.summary
        )}
      </p>
    );
  }
  if (ev.node === "synthesize" && ev.draft) {
    return <p>{`${ev.draft.confidence[0].toUpperCase()}${ev.draft.confidence.slice(1)} confidence, ${ev.draft.cited_ids.length} citations`}</p>;
  }
  if (ev.node === "verify") {
    const n = ev.unsupported?.length ?? 0;
    return (
      <p>
        {n === 0 ? ev.summary : `${n} claim${n === 1 ? "" : "s"} not supported${ev.retrying ? "; searching again" : ""}`}
      </p>
    );
  }
  return <p>{ev.summary}</p>;
}

interface Props {
  timeline: TimelineItem[];
  running: boolean;
  sinceLast?: number; // seconds since the last event, while running
  slowNote?: string | null;
}

export function FieldLog({ timeline, running, sinceLast, slowNote }: Props) {
  return (
    <div className="log">
      {slowNote && running && <p className="log__slow">{slowNote}</p>}
      <ol className="log__list" aria-live="polite" aria-relevant="additions">
        {timeline.map((item, i) =>
          item.type === "log" ? (
            <li key={i} className={`log__row log__row--note log__row--${item.level}`}>
              <span className="log__at mono">{clock(item.at)}</span>
              <div className="log__body">
                <p>{item.message}</p>
              </div>
            </li>
          ) : (
            <li key={i} className={`log__row log__row--${item.node}`}>
              <span className="log__at mono">{clock(item.at)}</span>
              <div className="log__body">
                <h4 className="log__title">{stepTitle(item)}</h4>
                <StepDetail ev={item} />
              </div>
            </li>
          ),
        )}
        {running && (
          <li className="log__row log__row--working" aria-hidden>
            <span className="log__at">
              <span className="live-dot" />
            </span>
            <div className="log__body">
              <p className="muted">
                Working{typeof sinceLast === "number" && sinceLast >= 3 ? ` · ${clock(sinceLast)} on this step` : "…"}
              </p>
            </div>
          </li>
        )}
      </ol>
    </div>
  );
}

/** The investigation layout: question, status line, the stratigraphic section, the answer and
 *  the field log, plus the find drawer. Presentational: it renders any folded run (live in the
 *  app, or a recording replayed on the public site), so it never fetches. Server-render safe.
 *
 *  Owned by the lead; builders use it, changes go through the lead. */

import { type ReactNode, useCallback, useMemo, useState } from "react";
import type { Confidence, RunOptions } from "../api/types";
import { duration, num, plural } from "../lib/format";
import { edges as edgesOf } from "../lib/links";
import { findState, gradeOf, layers as layersOf, type RunView as RunModel } from "../lib/runModel";
import { AnswerBody } from "./Answer";
import { FieldLog } from "./FieldLog";
import { FindDrawer } from "./FindDrawer";
import { Section } from "./Section";

const CONFIDENCE_LEVEL: Record<Confidence, number> = { low: 1, medium: 2, high: 3 };
const STATUS_TEXT = { queued: "Waiting", running: "Digging", done: "Answered", error: "Failed" } as const;

export interface RunViewProps {
  view: RunModel; // foldRun(events, status)
  question: string;
  model: string;
  options: RunOptions;
  elapsedText: string; // "0:47" while running, "1m 03s" when done
  selected: string | null; // the find open in the drawer
  onSelect: (id: string | null) => void;
  crumbs?: ReactNode; // breadcrumb nav above the question
  headingLevel?: 1 | 2; // the question's heading level (2 when embedded in a page)
  sinceLast?: number; // seconds since the last event, while running
  slowNote?: string | null; // e.g. the free-tier latency note
  failure?: ReactNode; // shown when the run failed (message + actions)
  notice?: ReactNode; // e.g. a lost-connection message
  aside?: ReactNode; // extra content under the report (e.g. replay controls)
}

export function RunView({
  view,
  question,
  model,
  options,
  elapsedText,
  selected,
  onSelect,
  crumbs,
  headingLevel = 1,
  sinceLast,
  slowNote,
  failure,
  notice,
  aside,
}: RunViewProps) {
  const [highlight, setHighlight] = useState<string | null>(null);
  const running = view.status === "running" || view.status === "queued";
  const layers = useMemo(() => layersOf(view), [view]);
  // lines only between finds that are on show (irrelevant ones start collapsed)
  const edges = useMemo(
    () => edgesOf([...view.finds.values()].filter((f) => findState(view, f.id) !== "dropped")),
    [view],
  );
  const stateOf = useCallback((fid: string) => findState(view, fid), [view]);
  const gradeFor = useCallback((fid: string) => gradeOf(view, fid), [view]);

  const answer = view.answer;
  const done = view.status === "done" && answer !== null;
  const counts = {
    found: view.finds.size,
    kept: [...view.finds.keys()].filter((f) => stateOf(f) === "kept" || stateOf(f) === "cited").length,
    cited: view.cited.size,
  };
  const emptyText = (key: string) => {
    if (running) return "Nothing yet";
    if (options.offline && (key === "prs" || key === "issues")) return "Not searched (offline)";
    return "Nothing found at this depth";
  };
  const selectedFind = selected ? view.finds.get(selected) : undefined;
  const Heading = headingLevel === 1 ? "h1" : "h2";

  const log = <FieldLog timeline={view.timeline} running={running} sinceLast={sinceLast} slowNote={slowNote} />;

  return (
    <div className="run">
      {crumbs}
      <Heading className="run__q">{question}</Heading>
      <p className="run__meta">
        <span className={`run__status is-${view.status}`}>
          {running && <span className="live-dot" aria-hidden />}
          {STATUS_TEXT[view.status]}
        </span>
        <span className="mono">{elapsedText}</span>
        <span className="mono muted">{model}</span>
        {!options.follow_links && <span className="muted">links not followed</span>}
        {!options.verify && <span className="muted">claims not checked</span>}
        {options.offline && <span className="muted">offline</span>}
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
            onSelect={onSelect}
            emptyText={emptyText}
          />
        </div>

        <div className="run__report">
          {view.status === "queued" && <p className="note">Waiting for the previous question to finish.</p>}
          {view.status === "error" && failure}
          {notice}

          {done && answer && (
            <article className="answer" aria-label="Answer">
              <AnswerBody
                markdown={answer.answer}
                finds={view.finds}
                accession={view.accession}
                highlight={highlight}
                onHover={setHighlight}
                onSelect={onSelect}
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
              <section aria-label="Field log">
                <p className="eyebrow run__log-title">Field log</p>
                {log}
              </section>
            )
          )}
          {aside}
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
          onSelect={onSelect}
          onClose={() => onSelect(null)}
        />
      )}
    </div>
  );
}

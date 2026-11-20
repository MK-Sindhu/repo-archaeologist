/** /examples (gallery) and /examples/:slug (one recording replayed in full). */

import { ArrowRight, Pause, Play, RotateCcw } from "lucide-react";
import { useMemo, useState } from "react";
import type { RunOptions } from "../../api/types";
import { RunView } from "../../components/RunView";
import { useFolded } from "../../components/site/Replay";
import { clock, duration, shortDate } from "../../lib/format";
import { useReplay } from "../../lib/replay";
import { Link, paths } from "../../router";
import { useRecording, useShowcase } from "./data";
import { ExampleRow } from "./Landing";
import { SiteNotFound } from "./SiteNotFound";

export function Examples() {
  const showcase = useShowcase();
  return (
    <main id="content" className="site">
      <header className="page-head container">
        <p className="eyebrow">Examples</p>
        <h1 className="page-head__title">Real runs, replayed</h1>
        <p className="page-head__lead">
          Each of these is a real investigation of an open-source repository, recorded once and replayed here in
          your browser. Nothing on these pages calls a model. The bars show how deep each answer's citations go:
          code, commits, pull requests, issues.
        </p>
      </header>
      <section className="container lp-section" aria-label="Recorded runs">
        {showcase.summaries.length ? (
          <ul className="ex-list">
            {showcase.summaries.map((s) => (
              <ExampleRow key={s.slug} s={s} />
            ))}
          </ul>
        ) : (
          <p className="muted">{showcase.ready ? "No recordings yet." : "Loading the recordings…"}</p>
        )}
        <p className="lp-cta-line">
          <Link to={paths.app()} className="lp-more">
            Ask about a repository of your own <ArrowRight size={15} aria-hidden />
          </Link>
        </p>
      </section>
    </main>
  );
}

const SPEEDS: { label: string; value: number }[] = [
  { label: "1×", value: 1 },
  { label: "4×", value: 4 },
  { label: "Instant", value: Infinity },
];

const OPTIONS_FALLBACK: RunOptions = { follow_links: true, verify: true, offline: false };

export function Example({ slug }: { slug: string }) {
  const { recording, missing } = useRecording(slug);
  const [speed, setSpeed] = useState(1);
  const [selected, setSelected] = useState<string | null>(null);
  const events = useMemo(() => recording?.run.events ?? [], [recording]);
  const replay = useReplay(events, { speed, loop: false });
  const view = useFolded(events, replay.count);

  if (missing) return <SiteNotFound />;
  if (!recording) return <main id="content" className="site container" aria-busy="true" />;

  const run = recording.run;
  const finished = replay.done;
  const elapsed = finished ? duration(run.elapsed_s ?? view.answer?.latency_s ?? view.lastAt) : clock(view.lastAt);

  return (
    <main id="content" className="site container example">
      <nav className="crumbs" aria-label="Breadcrumb">
        <Link to={paths.examples()}>Examples</Link>
      </nav>
      <p className="example__context">
        Recorded {shortDate(recording.recorded_at)} on{" "}
        <a href={recording.repo.web_url} className="mono">
          {recording.repo.slug}
        </a>{" "}
        · {recording.repo.language}
        {recording.repo.commits !== null && <> · {recording.repo.commits.toLocaleString("en-US")} commits of history</>}
      </p>
      <div ref={replay.ref}>
        <RunView
          view={view}
          question={run.question}
          model={run.model}
          options={run.options ?? OPTIONS_FALLBACK}
          elapsedText={elapsed}
          selected={selected}
          onSelect={setSelected}
          aside={
            <div className="replay-bar" role="group" aria-label="Replay">
              <button
                type="button"
                className="btn btn-secondary btn-small"
                onClick={replay.playing ? replay.pause : replay.play}
              >
                {replay.playing ? <Pause size={14} aria-hidden /> : <Play size={14} aria-hidden />}
                {replay.playing ? "Pause" : finished ? "Replay" : "Play"}
              </button>
              <button type="button" className="btn btn-secondary btn-small" onClick={replay.restart}>
                <RotateCcw size={14} aria-hidden /> Restart
              </button>
              <span className="replay-bar__speeds" role="radiogroup" aria-label="Speed">
                {SPEEDS.map((s) => (
                  <button
                    key={s.label}
                    type="button"
                    role="radio"
                    aria-checked={speed === s.value}
                    className={speed === s.value ? "rp-seg is-on" : "rp-seg"}
                    onClick={() => {
                      setSpeed(s.value);
                      if (!Number.isFinite(s.value)) replay.finish();
                    }}
                  >
                    {s.label}
                  </button>
                ))}
              </span>
              <span className="replay-bar__count mono muted">
                {replay.count}/{events.length} events
              </span>
            </div>
          }
        />
      </div>
      <aside className="example__cta">
        <p>This replay is a recording; no model was called to show it. Ask about a repository of your own:</p>
        <Link to={paths.app()} className="btn btn-primary">
          Ask your own
        </Link>
      </aside>
    </main>
  );
}

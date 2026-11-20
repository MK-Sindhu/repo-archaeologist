/** Replays of recorded investigations for the public site: the hero's looping section, the
 *  transport controls, and a small depth glyph. Server-render safe (the pre-rendered frame is
 *  the finished one; playback starts in effects). */

import { Pause, Play, RotateCcw } from "lucide-react";
import { type CSSProperties, useCallback, useMemo } from "react";
import type { RunEvent, ShowcaseRecording, StepEvent } from "../../api/types";
import { clock } from "../../lib/format";
import { edges as edgesOf } from "../../lib/links";
import { type Replay, useReplay } from "../../lib/replay";
import { findState, foldRun, gradeOf, layers as layersOf, type RunView } from "../../lib/runModel";
import type { RecordingSummary } from "../../pages/site/showcase";
import { citedOnly } from "../../pages/site/showcase";
import { Link, paths } from "../../router";
import { Section } from "../Section";
import { stepName } from "./steps";

/** Fold the first `count` events; the full set folds as finished. */
export function useFolded(events: readonly RunEvent[], count: number): RunView {
  return useMemo(
    () => foldRun(events.slice(0, count), count >= events.length ? "done" : "running"),
    [events, count],
  );
}

/** The section (strata, find tags, Harris lines) for a folded run. */
export function SectionOf({
  view,
  label,
  highlight,
  onHover,
  onSelect,
  selected,
  staircase,
}: {
  view: RunView;
  label: string;
  highlight?: string | null;
  onHover?: (id: string | null) => void;
  onSelect?: (id: string) => void;
  selected?: string | null;
  staircase?: boolean;
}) {
  const layers = useMemo(() => layersOf(view), [view]);
  const edges = useMemo(
    () => edgesOf([...view.finds.values()].filter((f) => findState(view, f.id) !== "dropped")),
    [view],
  );
  const stateOf = useCallback((id: string) => findState(view, id), [view]);
  const gradeFor = useCallback((id: string) => gradeOf(view, id), [view]);
  const finished = view.status === "done";
  return (
    <Section
      label={label}
      layers={layers}
      stateOf={stateOf}
      gradeOf={gradeFor}
      accession={view.accession}
      edges={edges}
      cited={view.cited}
      highlight={highlight}
      selected={selected}
      onHover={onHover}
      onSelect={onSelect}
      emptyText={() => (finished ? "Nothing at this depth" : "Nothing yet")}
      staircase={staircase}
    />
  );
}

export function lastStep(view: RunView): StepEvent | null {
  for (let i = view.timeline.length - 1; i >= 0; i--) {
    const item = view.timeline[i];
    if (item.type === "step") return item;
  }
  return null;
}

/** Play/pause + restart for an auto-playing replay (WCAG 2.2.2: moving content can be paused). */
export function MiniControls({ replay, label }: { replay: Replay; label: string }) {
  return (
    <span className="replay-mini">
      <button
        type="button"
        className="replay-mini__btn"
        onClick={replay.playing ? replay.pause : replay.play}
        aria-label={replay.playing ? `Pause the ${label}` : `Play the ${label}`}
      >
        {replay.playing ? <Pause size={13} aria-hidden /> : <Play size={13} aria-hidden />}
        {replay.playing ? "Pause" : "Play"}
      </button>
      <button type="button" className="replay-mini__btn" onClick={replay.restart} aria-label={`Restart the ${label}`}>
        <RotateCcw size={13} aria-hidden />
        Restart
      </button>
    </span>
  );
}

/** The hero: a real recording drawing itself into the strata, looping. Only the evidence the
 *  answer cites is shown (the chain behind it); the full run is one click away. */
export function HeroReplay({ recording }: { recording: ShowcaseRecording }) {
  const events = useMemo(() => citedOnly(recording.run.events), [recording]);
  const replay = useReplay(events, { loop: true, holdMs: 6500 });
  const view = useFolded(events, replay.count);
  const step = lastStep(view);
  return (
    <figure ref={replay.ref} className={`hero-replay is-${replay.phase}`} id="replay">
      <div className="hero-replay__frame">
        <SectionOf view={view} label={`Evidence by depth, replayed from ${recording.repo.slug}`} staircase />
      </div>
      <div className="hero-replay__ticker" aria-hidden>
        {step ? (
          <>
            <span className="mono hero-replay__at">{clock(step.at)}</span>
            <span className="hero-replay__step">{stepName(step)}</span>
          </>
        ) : (
          <span className="hero-replay__step muted">Starting</span>
        )}
      </div>
      <figcaption className="hero-replay__caption">
        <span>
          A real run on <span className="mono">{recording.repo.slug}</span>, showing the evidence its answer
          cites: “{recording.run.question}”
        </span>
        <span className="hero-replay__actions">
          <MiniControls replay={replay} label="replay" />
          <Link to={paths.example(recording.slug)} className="hero-replay__full">
            Watch it in full
          </Link>
        </span>
      </figcaption>
    </figure>
  );
}

/** Four bands, filled down to the deepest layer the answer cited from. */
export function DepthGlyph({ summary }: { summary: RecordingSummary }) {
  const depth = summary.layers.issues ? 4 : summary.layers.prs ? 3 : summary.layers.commits ? 2 : 1;
  const names = ["the code", "commits", "pull requests", "issues"];
  return (
    <span className="depth" role="img" aria-label={`Cited evidence down to ${names[depth - 1]}`}>
      {[0, 1, 2, 3].map((i) => (
        <i key={i} className={i < depth ? "on" : undefined} style={{ "--i": i } as CSSProperties} />
      ))}
    </span>
  );
}

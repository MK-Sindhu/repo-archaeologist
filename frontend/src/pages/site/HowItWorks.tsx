/** /how-it-works: every step of a real recorded run, stepped through as you scroll. The
 *  section beside the text shows what had been dug up after that step. */

import { ArrowRight } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { StepEvent } from "../../api/types";
import { SectionOf, useFolded } from "../../components/site/Replay";
import { EXPLAIN } from "../../components/site/steps";
import { clock, duration, plural, refLabel } from "../../lib/format";
import { Link, paths } from "../../router";
import { useRecording, useShowcase } from "./data";
import { heroSlug, keptOnly, type Walkthrough, walkthrough } from "./showcase";

const SOURCE: Record<string, string> = {
  code: "code",
  docs: "docs",
  commits: "commit messages",
  prs: "pull requests",
};

function refs(ids: readonly string[]): string {
  return ids.map(refLabel).join(", ");
}

/** What this step did in the recorded run, in a sentence or two. */
function inThisRun(step: Walkthrough): string {
  const ev: StepEvent = step.events[0];
  switch (step.node) {
    case "analyze": {
      const a = ev.analysis;
      if (!a) return ev.summary;
      const targets = a.targets.map((t) => t.symbol ?? t.path).filter(Boolean);
      const searches = a.queries.map((q) => `${SOURCE[q.source] ?? q.source} “${q.query}”`).join("; ");
      return `A ${a.question_type} question${targets.length ? ` about ${targets.join(", ")}` : ""}. Searches: ${searches}.`;
    }
    case "retrieve": {
      const parts = step.events.map((e) => `${SOURCE[e.source ?? ""] ?? e.source}: ${plural(e.evidence?.length ?? 0, "hit")}`);
      return `${parts.join(" · ")}.`;
    }
    case "trace_history": {
      const n = ev.evidence?.length ?? 0;
      return n ? `${plural(n, "commit")} changed the code in question.` : ev.summary;
    }
    case "grade": {
      const graded = ev.grades?.length ?? 0;
      const kept = ev.kept?.length ?? 0;
      const next = ev.frontier?.length ? ` Next, follow ${refs(ev.frontier)}.` : " Nothing left worth following.";
      return `Graded ${plural(graded, "new find")}; ${kept} kept so far.${next}`;
    }
    case "follow": {
      const got = (ev.evidence ?? []).map((f) => f.id);
      return got.length ? `Hop ${ev.hops ?? 1}: fetched ${refs(got)}.` : ev.summary;
    }
    case "synthesize":
      return ev.draft
        ? `Drafted an answer citing ${plural(ev.draft.cited_ids.length, "find")}, with ${ev.draft.confidence} confidence.`
        : ev.summary;
    case "verify":
      return `${ev.summary[0].toUpperCase()}${ev.summary.slice(1)}.`;
    case "finalize": {
      const a = ev.answer;
      if (!a) return ev.summary;
      return `${plural(a.citations.length, "source")} cited after ${plural(a.hops, "hop")}${a.latency_s ? `, ${duration(a.latency_s)} in all` : ""}.`;
    }
  }
}

export function HowItWorks() {
  const showcase = useShowcase();
  const { recording, missing } = useRecording(heroSlug(showcase.slugs));
  const events = useMemo(() => (recording ? keptOnly(recording.run.events) : []), [recording]);
  const steps = useMemo(() => walkthrough(recording?.run.events ?? []), [recording]);
  const [active, setActive] = useState<number | null>(null); // null: the finished run
  const items = useRef<(HTMLLIElement | null)[]>([]);

  useEffect(() => {
    if (!steps.length || typeof IntersectionObserver === "undefined") return;
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.isIntersecting) setActive(Number((e.target as HTMLElement).dataset.index));
        }
      },
      { rootMargin: "-42% 0px -48% 0px" },
    );
    items.current.forEach((el) => el && io.observe(el));
    return () => io.disconnect();
  }, [steps]);

  const upTo = active === null ? events.length : (steps[active]?.upTo ?? events.length);
  const view = useFolded(events, upTo);
  const seen = new Set<string>();

  return (
    <main id="content" className="site">
      <header className="page-head container">
        <p className="eyebrow">How it works</p>
        <h1 className="page-head__title">One question, followed to the bottom</h1>
        {recording ? (
          <p className="page-head__lead">
            This is a real run on <span className="mono">{recording.repo.slug}</span>: “{recording.run.question}”
            Scroll through it step by step. The section shows what had been dug up after each step; the evidence
            the run set aside is left out to keep it readable.
          </p>
        ) : (
          <p className="page-head__lead">{missing ? "The example run could not be loaded." : "Loading the example run…"}</p>
        )}
      </header>

      {recording && (
        <div className="walk container">
          <div className="walk__visual">
            <div className="walk__sticky">
              <SectionOf view={view} label="Evidence by depth after the current step" />
              <p className="walk__where muted" aria-live="polite">
                {active === null ? "The finished run" : `After step ${active + 1} of ${steps.length}`}
              </p>
            </div>
          </div>
          <ol className="walk__steps">
            {steps.map((step, i) => {
              const first = !seen.has(step.node);
              seen.add(step.node);
              const ex = EXPLAIN[step.node];
              const title = step.node === "follow" ? `${ex.title} · hop ${step.events[0].hops ?? 1}` : ex.title;
              return (
                <li
                  key={step.key}
                  ref={(el) => {
                    items.current[i] = el;
                  }}
                  data-index={i}
                  className={active === i ? "walk__step is-active" : "walk__step"}
                >
                  <span className="walk__at mono">{clock(step.events[0].at)}</span>
                  <h2 className="walk__title">{title}</h2>
                  {first && <p className="walk__what">{ex.what}</p>}
                  <p className="walk__run">
                    <span className="eyebrow">In this run</span> {inThisRun(step)}
                  </p>
                </li>
              );
            })}
          </ol>
        </div>
      )}

      <section className="lp-section container prose-block" aria-labelledby="hw-modes">
        <h2 id="hw-modes" className="lp-h2">
          Why, or how
        </h2>
        <p>
          Questions about why (why is this here, when did it start, who wanted it) dig through history as above.
          Questions about what the code does, where something happens or how it works are answered from the code
          and docs as they are now: the answer explains instead of digging, and skips links it doesn't need.
        </p>
      </section>

      <section className="lp-section container prose-block" aria-labelledby="hw-thin">
        <h2 id="hw-thin" className="lp-h2">
          Repositories with little history
        </h2>
        <p>
          A new repository, or one imported in a single commit, has nothing to dig through. The repository page says
          so, and answers come from the code and docs as they are now. It won't invent a motive that no commit,
          pull request or issue records.
        </p>
      </section>

      <section className="lp-section container prose-block" aria-labelledby="hw-not">
        <h2 id="hw-not" className="lp-h2">
          What it doesn't do
        </h2>
        <ul className="plain-list">
          <li>It doesn't guess. When nothing says why, the answer says the evidence doesn't establish it.</li>
          <li>It doesn't write anything: no code changes, no comments on GitHub. It only reads.</li>
          <li>
            The hosted version doesn't open private repositories. Run it yourself and it opens any clone on your
            disk.
          </li>
        </ul>
        <p className="lp-cta-line">
          <Link to={paths.examples()} className="lp-more">
            See more real runs <ArrowRight size={15} aria-hidden />
          </Link>
        </p>
      </section>
    </main>
  );
}

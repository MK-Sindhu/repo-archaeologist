/** The landing page. The hero's replay is the one moving thing; everything else is quiet. */

import { ArrowRight } from "lucide-react";
import { DepthGlyph, HeroReplay } from "../../components/site/Replay";
import { GITHUB_URL } from "../../components/site/SiteChrome";
import { duration, plural } from "../../lib/format";
import { Link, paths } from "../../router";
import { useRecording, useShowcase } from "./data";
import { heroSlug, type RecordingSummary, totals } from "./showcase";

const ASKS = [
  {
    who: "Getting started in a codebase",
    question: "What does this module do, and where does a request enter it?",
    gets: "An explanation of the code as it is now, citing the files and functions it read.",
  },
  {
    who: "Reviewing a change",
    question: "Why is this check here?",
    gets: "The commit that added it and the discussion that asked for it.",
  },
  {
    who: "Refactoring",
    question: "Is this workaround still needed?",
    gets: "The issue it worked around, so you can see whether that problem still exists.",
  },
  {
    who: "Debugging",
    question: "When did this behaviour start, and why?",
    gets: "The change that introduced it, and the pull request that explains the intent.",
  },
];

const STEPS = [
  { title: "Plan", text: "Reads the question and names the code it is about." },
  { title: "Search", text: "Code, docs, commit messages and pull requests, at once." },
  { title: "Follow the links", text: "Commit → pull request → issue, one hop at a time." },
  { title: "Write and check", text: "Every claim cites a find, and every claim is checked." },
];

export function ExampleRow({ s }: { s: RecordingSummary }) {
  return (
    <li className="ex-row">
      <Link to={paths.example(s.slug)} className="ex-row__link">
        <span className="ex-row__repo">
          <DepthGlyph summary={s} />
          <span className="mono">{s.repo.slug}</span>
          <span className="muted">{s.repo.language}</span>
        </span>
        <span className="ex-row__q">{s.question}</span>
        <span className="ex-row__facts">
          {plural(s.hops, "hop")} · {plural(s.cited, "source")} cited
          {s.claims && <> · {s.claims.supported}/{s.claims.checked} claims supported</>}
          {s.seconds !== null && <> · {duration(s.seconds)}</>}
        </span>
        <ArrowRight className="ex-row__arrow" size={18} aria-hidden />
      </Link>
    </li>
  );
}

export function Landing() {
  const showcase = useShowcase();
  const hero = useRecording(heroSlug(showcase.slugs));
  const t = totals(showcase.summaries);

  return (
    <main id="content" className="site">
      <section className="lp-hero container" aria-labelledby="lp-title">
        <div className="lp-hero__copy">
          <h1 id="lp-title" className="display lp-hero__title">
            Every line of code was written for a reason.
          </h1>
          <p className="lp-hero__sub">
            Ask why a piece of code is the way it is. Repo Archaeologist follows it to the commit that changed
            it, the pull request behind that commit and the issue behind the pull request, and cites each one.
          </p>
          <div className="lp-hero__ctas">
            <Link to={paths.app()} className="btn btn-primary">
              Try it on a repository
            </Link>
            <a href="#replay" className="btn btn-secondary">
              Watch a real investigation
            </a>
          </div>
        </div>
        <div className="lp-hero__visual">
          {hero.recording ? (
            <HeroReplay recording={hero.recording} />
          ) : (
            <div className="hero-replay hero-replay--empty" id="replay" aria-busy={!hero.missing}>
              {hero.missing && <p className="muted">The example run could not be loaded.</p>}
            </div>
          )}
        </div>
      </section>

      <section className="lp-problem" aria-label="The problem">
        <div className="container lp-problem__inner">
          <p className="lp-problem__line">
            <code>git blame</code> tells you which commit. It rarely tells you why.
          </p>
          <p className="lp-problem__line lp-problem__line--deep">
            The reason is usually a hop or two away: in the pull request that commit came from, or the issue
            that pull request fixed.
          </p>
        </div>
      </section>

      <section className="lp-section container" aria-labelledby="lp-how">
        <div className="lp-section__head">
          <h2 id="lp-how" className="lp-h2">
            How it digs
          </h2>
          <Link to={paths.how()} className="lp-more">
            Every step on a real run <ArrowRight size={15} aria-hidden />
          </Link>
        </div>
        <ol className="lp-steps">
          {STEPS.map((s, i) => (
            <li key={s.title} className="lp-step">
              <span className="lp-step__n mono" aria-hidden>
                {String(i + 1).padStart(2, "0")}
              </span>
              <h3 className="lp-step__title">{s.title}</h3>
              <p className="lp-step__text">{s.text}</p>
            </li>
          ))}
        </ol>
      </section>

      <section className="lp-section container" aria-labelledby="lp-asks">
        <h2 id="lp-asks" className="lp-h2">
          What people ask it
        </h2>
        <dl className="lp-asks">
          {ASKS.map((a) => (
            <div key={a.who} className="lp-ask">
              <dt className="eyebrow lp-ask__who">{a.who}</dt>
              <dd className="lp-ask__q">“{a.question}”</dd>
              <dd className="lp-ask__gets">{a.gets}</dd>
            </div>
          ))}
        </dl>
      </section>

      {showcase.summaries.length > 0 && (
        <section className="lp-section container" aria-labelledby="lp-examples">
          <div className="lp-section__head">
            <h2 id="lp-examples" className="lp-h2">
              Real runs, replayed
            </h2>
            <Link to={paths.examples()} className="lp-more">
              All examples <ArrowRight size={15} aria-hidden />
            </Link>
          </div>
          <ul className="ex-list">
            {showcase.summaries.slice(0, 3).map((s) => (
              <ExampleRow key={s.slug} s={s} />
            ))}
          </ul>
        </section>
      )}

      <section className="lp-section container" aria-labelledby="lp-trust">
        <h2 id="lp-trust" className="lp-h2">
          Cited, checked, and yours to run
        </h2>
        <div className="lp-trust">
          <div className="lp-trust__item">
            <h3>Every claim cites a find</h3>
            <p>
              Answers point at the commit, pull request, issue or line of code behind each sentence, and a second
              pass checks every claim against what it cites.
              {t.claimsChecked > 0 && (
                <>
                  {" "}
                  In the examples here, {t.claimsSupported} of {plural(t.claimsChecked, "claim")} were supported by
                  their evidence.
                </>
              )}
            </p>
          </div>
          <div className="lp-trust__item">
            <h3>The source is public</h3>
            <p>
              The agent, the website and the evaluation are on <a href={GITHUB_URL}>GitHub</a>. Read how it decides
              what to follow before you trust it.
            </p>
          </div>
          <div className="lp-trust__item">
            <h3>Run it yourself</h3>
            <p>
              One script on your machine, or one Docker image on a server. A Render blueprint and a Vercel setup
              are included. <Link to={paths.selfHost()}>How to deploy it</Link>.
            </p>
          </div>
          <div className="lp-trust__item">
            <h3>Free to start</h3>
            <p>It runs on Gemini's free tier, slowly, or on Claude. Your API key stays on your server.</p>
          </div>
        </div>
      </section>

      <section className="lp-final" aria-labelledby="lp-final">
        <div className="container lp-final__inner">
          <h2 id="lp-final" className="display lp-final__title">
            Pick a repository. Ask why.
          </h2>
          <div className="lp-hero__ctas">
            <Link to={paths.app()} className="btn btn-primary">
              Open the app
            </Link>
            <Link to={paths.selfHost()} className="btn btn-secondary">
              Run it yourself
            </Link>
          </div>
        </div>
      </section>
    </main>
  );
}

/** /faq: plain answers, as native disclosure widgets (work without JavaScript). */

import type { ReactNode } from "react";
import { Link, paths } from "../../router";

const QA: { q: string; a: ReactNode }[] = [
  {
    q: "Where does my code go?",
    a: (
      <p>
        To the server running Repo Archaeologist. It clones the repository there (with full history, because the
        history is the point), builds a search index of it on the same server, and sends the pieces relevant to your
        question to the language model. Nothing is written back to GitHub. Run it yourself and the server is yours.
      </p>
    ),
  },
  {
    q: "Where do the API keys live?",
    a: (
      <p>
        In the server's environment (<code>.env</code> on your machine, the host's settings in a deployment). They
        never reach the browser. A deployed server can require an access code, so strangers can't spend your quota.
      </p>
    ),
  },
  {
    q: "Can it read private repositories?",
    a: (
      <p>
        Not on a hosted server yet: it clones from GitHub without credentials. Run it on your own machine and it opens
        any clone on your disk, private or not.
      </p>
    ),
  },
  {
    q: "Which languages work?",
    a: (
      <p>
        Any repository can be searched and its history traced. It also understands functions and classes (finding
        the exact one a question is about, listing them on the map, and following each one's history) in Python,
        TypeScript, JavaScript, Go, Rust, Java, C#, Ruby, PHP, C and C++. Other languages are searched in blocks of
        lines instead.
      </p>
    ),
  },
  {
    q: "Which models does it use, and what does it cost?",
    a: (
      <p>
        Claude or Gemini, with your own key. Gemini has a free tier that is enough to try it: slow, and capped per
        day. A question takes roughly 4–10 model calls. On Claude you pay your provider's usual rates.
      </p>
    ),
  },
  {
    q: "Why does a question take a few minutes?",
    a: (
      <p>
        On Gemini's free tier each model call takes about 20–40 seconds, and an investigation makes several: a plan,
        grading, a draft and a check of every claim, plus a call for each round of links it follows. The page shows
        each step as it happens.
      </p>
    ),
  },
  {
    q: "How do I know an answer is right?",
    a: (
      <p>
        Every claim cites a find you can open: the commit, pull request, issue or lines of code it rests on. A
        separate pass checks each claim against what it cites and lists the ones it couldn't support. Citations to
        anything that was never retrieved are removed and reported. When nothing says why, the answer says so instead
        of guessing.
      </p>
    ),
  },
  {
    q: "What if the repository has almost no history?",
    a: (
      <p>
        Then there's little to dig through, and the repository page says so. You can still ask what the code does,
        where things happen and how they work; those answers come from the code and docs as they are now.
      </p>
    ),
  },
  {
    q: "Is it a replacement for git blame?",
    a: (
      <p>
        It starts where blame stops. Blame names the last commit that touched a line; Repo Archaeologist follows that
        commit to its pull request and the issue behind it, and reads the discussion, which is usually where the reason
        is written down.
      </p>
    ),
  },
];

export function Faq() {
  return (
    <main id="content" className="site">
      <header className="page-head container">
        <p className="eyebrow">FAQ</p>
        <h1 className="page-head__title">Questions and answers</h1>
      </header>
      <div className="container doc">
        <div className="faq">
          {QA.map((item) => (
            <details key={item.q} className="faq__item">
              <summary className="faq__q">{item.q}</summary>
              <div className="faq__a">{item.a}</div>
            </details>
          ))}
        </div>
        <p className="lp-cta-line">
          Still curious? <Link to={paths.how()}>See how an investigation works</Link>, or{" "}
          <Link to={paths.selfHost()}>run it yourself</Link>.
        </p>
      </div>
    </main>
  );
}

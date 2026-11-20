/** Detail panel for one find: full text, metadata, its grade and reason, links to the finds
 *  it points at, and a link to GitHub. */

import { ArrowUpRight, X } from "lucide-react";
import { useEffect, useRef } from "react";
import type { Find, Grade } from "../api/types";
import { shortDate } from "../lib/format";
import { resolveRef } from "../lib/links";
import { accessionLabel, type FindState } from "../lib/runModel";
import { KIND_ICON, KIND_LABEL, shortId } from "./FindTag";

const SOURCE_LABEL: Record<string, string> = {
  "search:code": "Code search",
  "search:docs": "Docs search",
  "search:commits": "Commit search",
  "search:prs": "Pull request search",
  history: "Git history of the code",
  fetch: "Followed a link",
  git: "Git",
};

const STATE_LABEL: Record<FindState, string> = {
  retrieved: "Not graded yet",
  kept: "Relevant",
  cited: "Cited in the answer",
  dropped: "Not relevant",
};

interface Props {
  find: Find;
  number: number | undefined;
  state: FindState;
  grade: Grade | undefined;
  finds: ReadonlyMap<string, Find>;
  accession: ReadonlyMap<string, number>;
  onSelect: (id: string) => void;
  onClose: () => void;
}

export function FindDrawer({ find, number, state, grade, finds, accession, onSelect, onClose }: Props) {
  const close = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    close.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [find.id, onClose]);

  const Icon = KIND_ICON[find.kind];
  const ids = new Set(finds.keys());
  const links = find.refs.map((ref) => ({ ref, target: resolveRef(ref, ids) }));
  const text = find.text ?? find.excerpt;
  const codeLike = find.kind === "code" || find.kind === "commit";

  return (
    <div className="drawer" role="presentation" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <aside className="drawer__panel" role="dialog" aria-modal="true" aria-labelledby="drawer-title">
        <header className="drawer__head">
          <p className={`drawer__no mono${state === "cited" ? " is-cited" : ""}`}>
            {accessionLabel(number)}
            <span className="drawer__kind">
              <Icon size={13} aria-hidden /> {KIND_LABEL[find.kind]}
            </span>
          </p>
          <button ref={close} type="button" className="btn-icon" onClick={onClose} aria-label="Close">
            <X size={18} />
          </button>
        </header>
        <h2 id="drawer-title" className="drawer__title">
          {find.title}
        </h2>
        <dl className="drawer__meta">
          <div>
            <dt>Status</dt>
            <dd>
              {STATE_LABEL[state]}
              {grade && ` · relevance ${grade.score} of 3`}
            </dd>
          </div>
          {grade?.reason && (
            <div>
              <dt>Why</dt>
              <dd>{grade.reason}</dd>
            </div>
          )}
          <div>
            <dt>Ref</dt>
            <dd className="mono">{shortId(find)}</dd>
          </div>
          {find.date && (
            <div>
              <dt>Date</dt>
              <dd>{shortDate(find.date)}</dd>
            </div>
          )}
          {find.meta.author && (
            <div>
              <dt>Author</dt>
              <dd>{find.meta.author}</dd>
            </div>
          )}
          <div>
            <dt>Found by</dt>
            <dd>{SOURCE_LABEL[find.source] ?? find.source}</dd>
          </div>
        </dl>
        {find.partial && (
          <p className="note">Known only from search results; its discussion was not fetched.</p>
        )}
        {find.meta.shallow_boundary && (
          <p className="note">
            The clone's history stops here, so this commit may look like it added everything.
          </p>
        )}
        {links.length > 0 && (
          <div className="drawer__links">
            <p className="eyebrow">Links to</p>
            <ul>
              {links.map(({ ref, target }) => (
                <li key={ref}>
                  {target ? (
                    <button type="button" className="drawer__ref" onClick={() => onSelect(target)}>
                      <span className="mono">{accessionLabel(accession.get(target))}</span>{" "}
                      {finds.get(target)?.title}
                    </button>
                  ) : (
                    <span className="mono muted">{ref}</span>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
        {text && <pre className={codeLike ? "drawer__text is-code" : "drawer__text"}>{text}</pre>}
        {find.url && (
          <a className="btn btn-secondary drawer__gh" href={find.url} target="_blank" rel="noreferrer">
            Open on GitHub <ArrowUpRight size={16} aria-hidden />
          </a>
        )}
      </aside>
    </div>
  );
}

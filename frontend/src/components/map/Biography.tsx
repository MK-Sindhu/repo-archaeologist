/** A symbol's (or file's) biography: the commits that changed it, newest first, each with the
 *  pull requests and issues it names. Commits are drawn as find tags numbered from the oldest
 *  (F-01 is where it began). */

import { ArrowUpRight } from "lucide-react";
import type { CSSProperties } from "react";
import type { Find, HistoryResponse } from "../../api/types";
import { refLabel } from "../../lib/format";
import { resolveRef } from "../../lib/links";
import { FindTag } from "../FindTag";

type Ready = Extract<HistoryResponse, { status: "ready" }>;

const LINKED = /^(pr|issue|gh):\d+$/;

export function Biography({
  history,
  subject,
  thin,
  resolving,
  onResolve,
}: {
  history: Ready;
  subject: string; // "backoff" or the file name
  thin: boolean;
  resolving: boolean;
  onResolve: () => void;
}) {
  const { commits, prs } = history;
  const linkIds = new Set(prs.map((p) => p.id));
  const anyLinks = commits.some((c) => c.refs.some((r) => LINKED.test(r)));

  if (!commits.length) {
    return (
      <p className="muted bio__empty">
        {thin
          ? `${subject} arrived with the repository's first commit; there is no older history to read.`
          : `No changes to ${subject} were found in the scanned history.`}
      </p>
    );
  }

  return (
    <div className="bio">
      <ol className="bio__list">
        {commits.map((c, i) => {
          const links = c.refs
            .filter((r) => LINKED.test(r))
            .map((r) => {
              const id = resolveRef(r, linkIds);
              return { ref: r, find: id ? prs.find((p) => p.id === id) : undefined };
            });
          return (
            <li key={c.id} className="bio__row">
              <div className="bio__body">
                <TagLink find={c} number={commits.length - i} band="var(--stratum-1)" />
                {links.length > 0 && (
                  <ul className="bio__links" aria-label="Linked pull requests and issues">
                    {links.map(({ ref, find }) => (
                      <li key={ref} className="bio__linked">
                        {find ? (
                          <TagLink find={find} number={undefined} band={find.kind === "issue" ? "var(--stratum-3)" : "var(--stratum-2)"} />
                        ) : (
                          <span className="bio__ref mono">{refLabel(ref)}</span>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </li>
          );
        })}
      </ol>
      <div className="bio__foot">
        {history.resolved ? (
          <p className="muted">Pull requests were looked up on GitHub.</p>
        ) : (
          <>
            <button type="button" className="btn btn-secondary btn-small" onClick={onResolve} disabled={resolving}>
              {resolving ? "Looking up…" : "Look up pull requests on GitHub"}
            </button>
            <p className="muted bio__hint">
              {anyLinks
                ? "Links above come from the commit messages. GitHub can also tell which pull request each commit belongs to (this uses GitHub API requests)."
                : "The commit messages name no pull requests. GitHub can tell which pull request each commit belongs to (this uses GitHub API requests)."}
            </p>
          </>
        )}
      </div>
    </div>
  );
}

function TagLink({ find, number, band }: { find: Find; number: number | undefined; band: string }) {
  const style = { "--band": band, width: "100%" } as CSSProperties;
  const tag = <FindTag find={find} number={number} state="kept" grade={null} style={style} />;
  if (!find.url) return tag;
  return (
    <a className="bio__tag" href={find.url} target="_blank" rel="noreferrer">
      {tag}
      <ArrowUpRight size={13} className="bio__out" aria-hidden />
    </a>
  );
}

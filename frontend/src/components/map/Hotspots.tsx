/** The most-changed files: where the repository's history is busiest. */

import { Flag } from "lucide-react";
import type { Survey } from "../../api/types";
import { num, plural } from "../../lib/format";
import { dayMonthYear } from "../../lib/survey";

export function Hotspots({
  survey,
  selected,
  onOpenFile,
}: {
  survey: Survey;
  selected: string | null;
  onOpenFile: (path: string) => void;
}) {
  const spots = survey.hotspots.filter((h) => h.changes > 1).slice(0, 8);
  if (!spots.length) return null;
  return (
    <section className="hot" aria-labelledby="hot-title">
      <h3 id="hot-title" className="eyebrow">
        <Flag size={11} strokeWidth={2.5} aria-hidden className="hot__flag" /> Hotspots
      </h3>
      <p className="hot__intro muted">The files changed most often. Their history has the most to tell.</p>
      <ol className="hot__list">
        {spots.map((h) => {
          const slash = h.path.lastIndexOf("/");
          return (
            <li key={h.path}>
              <button
                type="button"
                className={selected === h.path ? "hot__row is-selected" : "hot__row"}
                onClick={() => onOpenFile(h.path)}
              >
                <span className="hot__path mono">
                  {slash >= 0 && <span className="hot__dir">{h.path.slice(0, slash + 1)}</span>}
                  {h.path.slice(slash + 1)}
                </span>
                <span className="hot__meta">
                  {plural(h.changes, "change")}
                  {h.recent_changes > 0 && <> · {num(h.recent_changes)} this year</>} · last {dayMonthYear(h.last_change)}
                </span>
              </button>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

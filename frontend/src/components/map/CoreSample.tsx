/** The core sample: the repository's history as one column of time — commits per month (or
 *  quarter, or year), the first and last commit, the contributors, and the languages. */

import type { Survey } from "../../api/types";
import { num, plural } from "../../lib/format";
import { binMonths, dayMonthYear, formatBytes, monthLabel } from "../../lib/survey";

export function CoreSample({ survey }: { survey: Survey }) {
  const { totals } = survey;
  const thin = totals.commits <= 3;
  const bins = binMonths(survey.months, 72);
  const max = Math.max(1, ...bins.map((b) => b.commits));
  const busiest = bins.reduce((a, b) => (b.commits > a.commits ? b : a), bins[0] ?? { label: "", start: "", commits: 0 });
  const years = yearsBetween(totals.first_commit, totals.last_commit);
  const langBytes = survey.languages.reduce((s, l) => s + l.bytes, 0);
  const binWord = bins.length === survey.months.length ? "month" : /Q\d/.test(bins[0]?.label ?? "") ? "quarter" : "year";

  return (
    <section className="core" aria-labelledby="core-title">
      <h3 id="core-title" className="eyebrow">
        History
      </h3>
      {thin ? (
        <p className="core__thin">
          {totals.commits === 0
            ? "No commits recorded."
            : `${plural(totals.commits, "commit")}${totals.commits === 1 ? " on " : ", the last on "}${dayMonthYear(totals.last_commit)}`}
          {survey.authors[0] && <span className="muted"> · by {survey.authors.map((a) => a.name).join(", ")}</span>}
        </p>
      ) : (
        <>
          <p className="core__facts">
            <strong>{plural(totals.commits, "commit")}</strong>
            {years !== null && <span> over {years < 1 ? "less than a year" : plural(Math.round(years), "year")}</span>}
            <span className="muted"> · {plural(totals.authors, "author")}</span>
          </p>
          <div
            className="core__bars"
            role="img"
            aria-label={`Commits per ${binWord} from ${label(bins[0]?.label)} to ${label(bins[bins.length - 1]?.label)}; busiest: ${label(busiest.label)} with ${plural(busiest.commits, "commit")}.`}
          >
            {bins.map((b) => (
              <span
                key={b.label}
                className={b.commits === 0 ? "core__bar is-empty" : "core__bar"}
                style={{ height: `${Math.max(b.commits === 0 ? 0 : 6, (b.commits / max) * 100)}%` }}
                title={`${label(b.label)}: ${plural(b.commits, "commit")}`}
              />
            ))}
          </div>
          <p className="core__axis mono">
            <span>{dayMonthYear(totals.first_commit)}</span>
            <span>{dayMonthYear(totals.last_commit)}</span>
          </p>
          {survey.truncated && (
            <p className="core__note muted">
              Only the latest {num(survey.months.reduce((s, m) => s + m.commits, 0))} commits were scanned for
              this map; older changes are not counted.
            </p>
          )}
        </>
      )}

      {survey.authors.length > 0 && !thin && (
        <div className="core__people">
          <p className="core__sub">Most commits</p>
          <ol className="people">
            {survey.authors.slice(0, 6).map((a) => (
              <li key={a.name}>
                <span className="people__name">{a.name}</span>
                <span className="people__bar" aria-hidden>
                  <span style={{ width: `${(a.commits / survey.authors[0].commits) * 100}%` }} />
                </span>
                <span className="people__n mono">{num(a.commits)}</span>
              </li>
            ))}
          </ol>
        </div>
      )}

      {survey.languages.length > 0 && (
        <div className="core__langs">
          <p className="core__sub">Languages</p>
          <div className="langs__bar" aria-hidden>
            {survey.languages.slice(0, 5).map((l, i) => (
              <span key={l.name} className={`langs__seg lang-${i}`} style={{ flexGrow: l.bytes }} />
            ))}
          </div>
          <ul className="langs__list">
            {survey.languages.slice(0, 5).map((l, i) => (
              <li key={l.name}>
                <span className={`langs__swatch lang-${i}`} aria-hidden />
                {l.name}
                <span className="muted mono">
                  {" "}
                  {pct(l.bytes, langBytes)} · {formatBytes(l.bytes)}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

function label(l: string | undefined): string {
  if (!l) return "";
  return /^\d{4}-\d{2}$/.test(l) ? monthLabel(l) : l;
}

function pct(part: number, whole: number): string {
  if (whole <= 0) return "0%";
  const p = (part / whole) * 100;
  return p < 1 ? "<1%" : `${Math.round(p)}%`;
}

function yearsBetween(a: string | null, b: string | null): number | null {
  if (!a || !b) return null;
  const ms = Date.parse(b) - Date.parse(a);
  return Number.isNaN(ms) ? null : ms / (365.25 * 86400e3);
}

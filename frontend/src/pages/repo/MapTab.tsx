/** Map tab: a newcomer's survey of the repository. The site plan (what's where, what's old,
 *  what keeps changing), its history as a core sample, the hotspots, and a panel per file with
 *  its functions, code and biographies. No LLM involved. Loaded lazily with the tab. */

import { Flag } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../../api/client";
import type { RepoSummary, Survey } from "../../api/types";
import { CoreSample } from "../../components/map/CoreSample";
import { FilePanel } from "../../components/map/FilePanel";
import { FileTable } from "../../components/map/FileTable";
import { Hotspots } from "../../components/map/Hotspots";
import { type FillMode, SitePlan } from "../../components/map/SitePlan";
import { plural } from "../../lib/format";
import {
  activityBucketStarts,
  ageBucketStarts,
  ageRange,
  buildTree,
  crumbPaths,
  dayMonthYear,
  findNode,
  formatBytes,
  monthYearOf,
} from "../../lib/survey";
import type { RepoView } from "./urlState";

const cache = new Map<string, Survey>(); // repo id -> survey (checked against the repo's head)
const HOT_TILES = 10; // hotspots marked on the plan

interface Props {
  repo: RepoSummary;
  view: RepoView;
  setView: (next: Partial<RepoView>) => void;
  onAskWhy: (question: string) => void;
}

export default function MapTab({ repo, view, setView, onAskWhy }: Props) {
  const cached = cache.get(repo.id);
  const [survey, setSurvey] = useState<Survey | null>(
    cached && (!repo.head || cached.head.startsWith(repo.head)) ? cached : null,
  );
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<FillMode>("age");
  const [layout, setLayout] = useState<"plan" | "table">("plan");

  useEffect(() => {
    if (survey) return;
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = () =>
      api
        .survey(repo.id)
        .then((s) => {
          if (!live) return;
          if (s.status === "pending") {
            setPending(true);
            timer = setTimeout(load, 3000);
            return;
          }
          cache.set(repo.id, s);
          setPending(false);
          setSurvey(s);
        })
        .catch((e) => live && setError(e instanceof Error ? e.message : String(e)));
    load();
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [repo.id, survey]);

  const hot = useMemo(
    () => new Set(survey?.hotspots.filter((h) => h.changes > 1).slice(0, HOT_TILES).map((h) => h.path) ?? []),
    [survey],
  );
  const tree = useMemo(() => (survey ? buildTree(survey.files, hot) : null), [survey, hot]);
  const range = useMemo(() => (survey ? ageRange(survey.files) : null), [survey]);
  const maxChanges = useMemo(() => Math.max(0, ...(survey?.files.map((f) => f[2]) ?? [0])), [survey]);
  const dirNode = tree ? (findNode(tree, view.dir) ?? tree) : null;
  const shownDir = dirNode && dirNode.isDir ? dirNode : tree;

  const openFile = useCallback((path: string) => setView({ file: path, symbol: null }), [setView]);
  const openDir = useCallback((path: string) => setView({ dir: path }), [setView]);
  const closeFile = useCallback(() => setView({ file: null }), [setView]);

  if (error) return <p className="error-text map__msg">{error}</p>;
  if (!survey || !tree || !shownDir) {
    return (
      <p className="muted map__msg" role="status">
        <span className="live-dot" aria-hidden /> {pending ? "Surveying the repository; this finishes with the indexing." : "Loading the map…"}
      </p>
    );
  }

  const thin = survey.totals.commits <= 3;
  const unknownDir = view.dir && dirNode !== shownDir;

  return (
    <div className="map">
      <div className="map__main">
        <div className="map__bar">
          <nav className="map__crumbs" aria-label="Folder">
            {crumbPaths(shownDir.path).map((p, i, all) => {
              const name = p ? p.split("/").pop() : repo.name;
              const last = i === all.length - 1;
              return (
                <span key={p || "root"} className="map__crumb">
                  {last ? (
                    <span aria-current="location" className="map__here">
                      {name}/
                    </span>
                  ) : (
                    <button type="button" className="map__up" onClick={() => openDir(p)}>
                      {name}/
                    </button>
                  )}
                </span>
              );
            })}
            <span className="map__size muted">
              {plural(shownDir.files, "file")} · {formatBytes(shownDir.bytes)}
            </span>
          </nav>
          <div className="map__controls">
            <Segmented
              label="Colour by"
              value={mode}
              options={[
                ["age", "Age"],
                ["activity", "Activity"],
              ]}
              onChange={(v) => setMode(v as FillMode)}
              disabled={layout === "table"}
            />
            <Segmented
              label="Show as"
              value={layout}
              options={[
                ["plan", "Plan"],
                ["table", "Table"],
              ]}
              onChange={(v) => setLayout(v as "plan" | "table")}
            />
          </div>
        </div>
        {unknownDir && <p className="note">The folder {view.dir} isn't in this repository any more; showing the top.</p>}

        {layout === "plan" ? (
          <>
            <SitePlan
              root={shownDir}
              mode={mode}
              range={range}
              maxChanges={maxChanges}
              selected={view.file}
              onOpenDir={openDir}
              onOpenFile={openFile}
            />
            <Legend mode={mode} range={range} maxChanges={maxChanges} thin={thin} hasHot={hot.size > 0} />
          </>
        ) : (
          <FileTable root={shownDir} selected={view.file} onOpenFile={openFile} />
        )}
      </div>

      <aside className="map__side" aria-label="Repository survey">
        <CoreSample survey={survey} />
        <Hotspots survey={survey} selected={view.file} onOpenFile={openFile} />
      </aside>

      {view.file && (
        <FilePanel
          repoId={repo.id}
          path={view.file}
          symbol={view.symbol}
          webUrl={repo.web_url}
          head={repo.head}
          thin={thin}
          onSymbol={(s) => setView({ symbol: s })}
          onClose={closeFile}
          onAskWhy={onAskWhy}
        />
      )}
    </div>
  );
}

function Segmented({
  label,
  value,
  options,
  onChange,
  disabled,
}: {
  label: string;
  value: string;
  options: [string, string][];
  onChange: (v: string) => void;
  disabled?: boolean;
}) {
  return (
    <div className="seg" role="group" aria-label={label}>
      <span className="seg__label">{label}</span>
      {options.map(([v, text]) => (
        <button
          key={v}
          type="button"
          className={v === value ? "seg__btn is-on" : "seg__btn"}
          aria-pressed={v === value}
          onClick={() => onChange(v)}
          disabled={disabled}
        >
          {text}
        </button>
      ))}
    </div>
  );
}

function Legend({
  mode,
  range,
  maxChanges,
  thin,
  hasHot,
}: {
  mode: FillMode;
  range: ReturnType<typeof ageRange>;
  maxChanges: number;
  thin: boolean;
  hasHot: boolean;
}) {
  let steps: { cls: string; text: string }[] = [];
  let caption = "";
  if (mode === "age") {
    caption = "Last changed";
    if (thin || !range || range.newest === range.oldest) {
      steps = [{ cls: "fill-a0", text: range ? `All in ${monthYearOf(range.newest)}` : "No recorded changes" }];
    } else {
      const starts = ageBucketStarts(range);
      // months collide when the whole history spans a few months: use days then
      const when = range.newest - range.oldest < 200 * 86400 ? (t: number) => dayMonthYear(Math.round(t)) : monthYearOf;
      steps = starts.map((t, i) => ({
        cls: `fill-a${i}`,
        text: i === 0 ? `since ${when(starts[1] ?? t)}` : i === starts.length - 1 ? `before ${when(t)}` : `from ${when(starts[i + 1])}`,
      }));
    }
  } else {
    caption = "Times changed";
    const starts = activityBucketStarts(maxChanges);
    steps = starts
      .map((n, i) => ({ cls: `fill-x${i}`, text: i === 0 ? "never" : `${n}+` }))
      .filter((s, i, all) => i === 0 || s.text !== all[i - 1].text);
  }
  return (
    <div className="legend" aria-label="Legend">
      <span className="legend__caption">{caption}</span>
      <ul className="legend__steps">
        {steps.map((s) => (
          <li key={s.cls}>
            <span className={`legend__swatch ${s.cls}`} aria-hidden />
            {s.text}
          </li>
        ))}
        {mode === "age" && !thin && (
          <li>
            <span className="legend__swatch fill-none" aria-hidden />
            no recorded change
          </li>
        )}
      </ul>
      {hasHot && (
        <span className="legend__hot">
          <span className="legend__swatch is-hot" aria-hidden>
            <Flag size={9} strokeWidth={2.5} />
          </span>
          hotspot: changed most often
        </span>
      )}
      <span className="legend__size muted">Area = size in bytes</span>
    </div>
  );
}

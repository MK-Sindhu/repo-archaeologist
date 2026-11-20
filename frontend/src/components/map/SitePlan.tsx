/** The site plan: a squarified treemap of the current folder, two levels deep, sized by bytes
 *  and filled by age (when each file last changed) or activity (how often it changed).
 *  Hotspots get a flag-pink outline and a flag marker. Tiles are buttons with a roving
 *  tabindex: arrow keys move between them, Enter opens a folder or a file. */

import { Flag, Folder } from "lucide-react";
import { type CSSProperties, type KeyboardEvent, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { plural } from "../../lib/format";
import {
  activityBucket,
  type AgeRange,
  ageBucket,
  formatBytes,
  monthYearOf,
  type TreeNode,
} from "../../lib/survey";
import { layoutMap, type MapTile } from "../../lib/treemap";

export type FillMode = "age" | "activity";

interface Props {
  root: TreeNode; // the folder on show
  mode: FillMode;
  range: AgeRange | null;
  maxChanges: number;
  selected: string | null; // open file
  onOpenDir: (path: string) => void;
  onOpenFile: (path: string) => void;
}

export function SitePlan({ root, mode, range, maxChanges, selected, onOpenDir, onOpenFile }: Props) {
  const wrap = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const measure = () => setWidth(Math.floor(el.clientWidth));
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const height = width < 600 ? Math.round(width * 1.15) : Math.round(Math.min(640, Math.max(380, width * 0.6)));
  const tiles = useMemo(
    () => (width > 0 ? layoutMap(root, width, height, { minArea: width < 600 ? 160 : 260 }) : []),
    [root, width, height],
  );
  const interactive = useMemo(() => tiles.filter((t) => isInteractive(t, root.path)), [tiles, root.path]);
  const [focusKey, setFocusKey] = useState<string | null>(null);
  const refs = useRef(new Map<string, HTMLButtonElement>());
  useEffect(() => setFocusKey(null), [root.path]);
  const activeKey = focusKey && interactive.some((t) => t.key === focusKey) ? focusKey : interactive[0]?.key;

  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const keys = ["ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp", "Home", "End"];
    if (!keys.includes(e.key) || !interactive.length) return;
    e.preventDefault();
    const i = Math.max(0, interactive.findIndex((t) => t.key === activeKey));
    const next =
      e.key === "Home"
        ? 0
        : e.key === "End"
          ? interactive.length - 1
          : e.key === "ArrowRight" || e.key === "ArrowDown"
            ? Math.min(interactive.length - 1, i + 1)
            : Math.max(0, i - 1);
    const key = interactive[next].key;
    setFocusKey(key);
    refs.current.get(key)?.focus();
  };

  const fill = (n: TreeNode | null): string => {
    if (!n) return "fill-none";
    if (mode === "activity") return `fill-x${activityBucket(n.changes, maxChanges)}`;
    const b = ageBucket(n.lastEpoch, range);
    return b === null ? "fill-none" : `fill-a${b}`;
  };

  return (
    <div ref={wrap} className="plan-wrap">
      {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions */}
      <div
        className="plan"
        style={{ height }}
        role="group"
        aria-label={`Site plan of ${root.path || "the repository"}: ${plural(root.files, "file")}, ${formatBytes(root.bytes)}`}
        onKeyDown={onKey}
      >
        {tiles.map((t) => {
          const pos: CSSProperties = { left: t.x, top: t.y, width: t.w, height: t.h };
          if (t.w < 1 || t.h < 1) return null;
          const tab = t.key === activeKey ? 0 : -1;
          const setRef = (el: HTMLButtonElement | null) => {
            if (el) refs.current.set(t.key, el);
            else refs.current.delete(t.key);
          };
          const onFocus = () => setFocusKey(t.key);
          if (t.kind === "frame" && t.node) {
            const n = t.node;
            return (
              <div key={t.key} className={`tile tile--frame${n.hot ? " has-hot" : ""}`} style={pos}>
                <button
                  ref={setRef}
                  type="button"
                  className="tile__frame-label"
                  tabIndex={tab}
                  onFocus={onFocus}
                  onClick={() => onOpenDir(n.path)}
                  aria-label={describe(n, "folder")}
                  title={describe(n, "folder")}
                >
                  {t.label ?? n.name}/
                </button>
              </div>
            );
          }
          if (t.kind === "more") {
            const label = `${plural(t.more?.count ?? 0, "small item")}, ${formatBytes(t.more?.bytes ?? 0)}`;
            const canZoom = t.parent !== root.path;
            const body = t.w >= 54 && t.h >= 18 ? <span className="tile__name">{`+${t.more?.count ?? 0}`}</span> : null;
            return canZoom ? (
              <button
                key={t.key}
                ref={setRef}
                type="button"
                className="tile tile--more"
                style={pos}
                tabIndex={tab}
                onFocus={onFocus}
                onClick={() => onOpenDir(t.parent)}
                aria-label={`${label} in ${t.parent}`}
                title={`${label}: open ${t.parent}/ to see them`}
              >
                {body}
              </button>
            ) : (
              <div key={t.key} className="tile tile--more" style={pos} title={`${label} (listed in the table view)`}>
                {body}
              </div>
            );
          }
          const n = t.node!;
          const isDir = t.kind === "dir";
          const showName = t.w >= 40 && t.h >= 16;
          const showSize = t.w >= 64 && t.h >= 36;
          const fileHot = n.hot && !isDir;
          const showFlag = fileHot && t.w >= 22 && t.h >= 18;
          return (
            <button
              key={t.key}
              ref={setRef}
              type="button"
              className={[
                "tile",
                isDir ? "tile--dir" : "tile--file",
                fill(n),
                fileHot && "is-hot",
                isDir && n.hot && "has-hot",
                selected === n.path && "is-selected",
                t.depth === 1 && "is-inner",
              ]
                .filter(Boolean)
                .join(" ")}
              style={pos}
              tabIndex={tab}
              onFocus={onFocus}
              onClick={() => (isDir ? onOpenDir(n.path) : onOpenFile(n.path))}
              aria-label={describe(n, isDir ? "folder" : "file")}
              title={describe(n, isDir ? "folder" : "file")}
            >
              {showName && (
                <span className="tile__name">
                  {isDir && <Folder className="tile__icon" size={10} strokeWidth={2.25} aria-hidden />}
                  {n.name}
                  {isDir && "/"}
                </span>
              )}
              {showSize && (
                <span className="tile__size">
                  {isDir ? `${plural(n.files, "file")} · ${formatBytes(n.bytes)}` : formatBytes(n.bytes)}
                </span>
              )}
              {showFlag && <Flag className="tile__flag" size={10} strokeWidth={2.5} aria-hidden />}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function isInteractive(t: MapTile, rootPath: string): boolean {
  if (t.w < 1 || t.h < 1) return false;
  if (t.kind === "more") return t.parent !== rootPath;
  return true;
}

function describe(n: TreeNode, what: "file" | "folder"): string {
  const parts = [
    `${n.path || n.name} (${what})`,
    formatBytes(n.bytes),
    what === "folder" ? plural(n.files, "file") : null,
    plural(n.changes, "change"),
    n.lastEpoch !== null ? `last changed ${monthYearOf(n.lastEpoch)}` : "no recorded change",
    n.hot ? (what === "folder" ? "contains a hotspot" : "hotspot") : null,
  ];
  return parts.filter(Boolean).join(", ");
}

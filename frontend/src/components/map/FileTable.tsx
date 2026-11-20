/** The site plan as a sortable table: the same files, for keyboard, screen-reader and small
 *  screen use. */

import { Flag } from "lucide-react";
import { useMemo, useState } from "react";
import { num, plural } from "../../lib/format";
import { filesUnder, formatBytes, monthYearOf, type TreeNode } from "../../lib/survey";

type Key = "path" | "bytes" | "changes" | "last";
const LIMIT = 300;

export function FileTable({
  root,
  selected,
  onOpenFile,
}: {
  root: TreeNode;
  selected: string | null;
  onOpenFile: (path: string) => void;
}) {
  const [sort, setSort] = useState<{ key: Key; desc: boolean }>({ key: "changes", desc: true });
  const [all, setAll] = useState(false);
  const rows = useMemo(() => {
    const files = filesUnder(root);
    const value = (f: TreeNode): string | number => {
      switch (sort.key) {
        case "path":
          return f.path;
        case "bytes":
          return f.bytes;
        case "changes":
          return f.changes;
        case "last":
          return f.lastEpoch ?? -1;
      }
    };
    return files.sort((a, b) => {
      const va = value(a);
      const vb = value(b);
      const c = typeof va === "string" ? va.localeCompare(vb as string) : va - (vb as number);
      return (sort.desc ? -c : c) || a.path.localeCompare(b.path);
    });
  }, [root, sort]);
  const shown = all ? rows : rows.slice(0, LIMIT);

  const header = (key: Key, label: string, numeric = false) => {
    const active = sort.key === key;
    return (
      <th
        scope="col"
        className={numeric ? "is-num" : undefined}
        aria-sort={active ? (sort.desc ? "descending" : "ascending") : "none"}
      >
        <button
          type="button"
          className="files__sort"
          onClick={() => setSort((s) => ({ key, desc: s.key === key ? !s.desc : key !== "path" }))}
        >
          {label}
          <span aria-hidden className="files__arrow">
            {active ? (sort.desc ? "↓" : "↑") : ""}
          </span>
        </button>
      </th>
    );
  };

  return (
    <div className="files">
      <table className="files__table">
        <caption className="visually-hidden">
          Files in {root.path || "the repository"}, sorted by {sort.key}
        </caption>
        <thead>
          <tr>
            {header("path", "File")}
            {header("bytes", "Size", true)}
            {header("changes", "Changes", true)}
            {header("last", "Last change", true)}
          </tr>
        </thead>
        <tbody>
          {shown.map((f) => (
            <tr key={f.path} className={selected === f.path ? "is-selected" : undefined}>
              <td className="files__path">
                <button type="button" className="files__open mono" onClick={() => onOpenFile(f.path)}>
                  {root.path ? f.path.slice(root.path.length + 1) : f.path}
                </button>
                {f.hot && (
                  <span className="files__hot" title="Hotspot: among the most-changed files">
                    <Flag size={11} strokeWidth={2.5} aria-hidden />
                    <span className="visually-hidden">hotspot</span>
                  </span>
                )}
              </td>
              <td className="is-num mono">{formatBytes(f.bytes)}</td>
              <td className="is-num mono">{num(f.changes)}</td>
              <td className="is-num">{f.lastEpoch !== null ? monthYearOf(f.lastEpoch) : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length > LIMIT && !all && (
        <button type="button" className="btn btn-secondary btn-small files__more" onClick={() => setAll(true)}>
          Show all {plural(rows.length, "file")}
        </button>
      )}
    </div>
  );
}

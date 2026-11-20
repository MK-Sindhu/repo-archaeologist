/** The repository page's view state, kept in the URL so it can be linked and survives
 *  reloads: ?tab=ask|map|guide&dir=<folder>&file=<path>&symbol=<qualified name>. */

import { useCallback, useEffect, useState } from "react";

export type RepoTab = "ask" | "map" | "guide";
export const TABS: readonly RepoTab[] = ["ask", "map", "guide"];

export interface RepoView {
  tab: RepoTab;
  dir: string; // the folder the site plan shows ("" = the repository root)
  file: string | null; // the file open in the panel
  symbol: string | null; // the symbol selected in that file
}

export function parseRepoView(search: string): RepoView {
  const q = new URLSearchParams(search);
  const tab = q.get("tab");
  const file = q.get("file") || null;
  return {
    tab: tab === "map" || tab === "guide" ? tab : "ask",
    dir: (q.get("dir") ?? "").replace(/^\/+|\/+$/g, ""),
    file,
    symbol: file ? q.get("symbol") || null : null,
  };
}

/** Search string for a view, keeping unrelated parameters (e.g. ?mock=1). */
export function serializeRepoView(view: RepoView, current = ""): string {
  const q = new URLSearchParams(current);
  for (const key of ["tab", "dir", "file", "symbol"]) q.delete(key);
  if (view.tab !== "ask") q.set("tab", view.tab);
  if (view.tab === "map") {
    if (view.dir) q.set("dir", view.dir);
    if (view.file) q.set("file", view.file);
    if (view.file && view.symbol) q.set("symbol", view.symbol);
  }
  const s = q.toString();
  return s ? `?${s}` : "";
}

/** View state synced to the URL with replaceState (no history entries per click). */
export function useRepoView(): [RepoView, (next: Partial<RepoView>) => void] {
  const [view, setView] = useState<RepoView>(() => parseRepoView(window.location.search));
  useEffect(() => {
    const sync = () => setView(parseRepoView(window.location.search));
    window.addEventListener("popstate", sync);
    return () => window.removeEventListener("popstate", sync);
  }, []);
  const update = useCallback((next: Partial<RepoView>) => {
    setView((prev) => {
      const merged: RepoView = { ...prev, ...next };
      if (!merged.file) merged.symbol = null;
      const search = serializeRepoView(merged, window.location.search);
      window.history.replaceState(null, "", window.location.pathname + search);
      return merged;
    });
  }, []);
  return [view, update];
}

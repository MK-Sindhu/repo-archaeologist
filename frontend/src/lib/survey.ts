/** Repository survey helpers for the Map tab: the folder tree built from the flat file list,
 *  colour buckets for the site plan (age and activity), time binning for the core sample, and
 *  small formatters. Pure functions; no DOM. */

import type { Survey, SurveyFile } from "../api/types";

export interface TreeNode {
  name: string; // last path segment ("" for the root)
  path: string; // "src/click" ("" for the root)
  isDir: boolean;
  bytes: number; // file size, or the sum over the folder
  changes: number; // commits that changed the file; for folders the sum
  lastEpoch: number | null; // last change (unix seconds); for folders the most recent
  files: number; // files in the subtree (1 for a file)
  hot: boolean; // a hotspot file, or a folder containing one
  children: TreeNode[]; // folders only, biggest first
}

/** Nested tree from the survey's flat file list. Folders sort before nothing in particular:
 *  children are ordered by size, biggest first. */
export function buildTree(files: readonly SurveyFile[], hotspots: ReadonlySet<string> = new Set()): TreeNode {
  const root: TreeNode = dir("", "");
  const dirs = new Map<string, TreeNode>([["", root]]);
  for (const [path, bytes, changes, lastEpoch] of files) {
    const parts = path.split("/").filter(Boolean);
    if (parts.length === 0) continue;
    let parent = root;
    for (let i = 0; i < parts.length - 1; i++) {
      const p = parts.slice(0, i + 1).join("/");
      let d = dirs.get(p);
      if (!d) {
        d = dir(parts[i], p);
        dirs.set(p, d);
        parent.children.push(d);
      }
      parent = d;
    }
    parent.children.push({
      name: parts[parts.length - 1],
      path: parts.join("/"),
      isDir: false,
      bytes: Math.max(0, bytes),
      changes: Math.max(0, changes),
      lastEpoch,
      files: 1,
      hot: hotspots.has(path),
      children: [],
    });
  }
  total(root);
  return root;
}

function dir(name: string, path: string): TreeNode {
  return { name, path, isDir: true, bytes: 0, changes: 0, lastEpoch: null, files: 0, hot: false, children: [] };
}

function total(node: TreeNode): void {
  if (!node.isDir) return;
  let bytes = 0;
  let changes = 0;
  let files = 0;
  let last: number | null = null;
  let hot = false;
  for (const c of node.children) {
    total(c);
    bytes += c.bytes;
    changes += c.changes;
    files += c.files;
    if (c.lastEpoch !== null && (last === null || c.lastEpoch > last)) last = c.lastEpoch;
    hot ||= c.hot;
  }
  node.children.sort((a, b) => b.bytes - a.bytes || a.name.localeCompare(b.name));
  Object.assign(node, { bytes, changes, files, lastEpoch: last, hot });
}

/** The folder at `path` ("" = root), or null. */
export function findNode(root: TreeNode, path: string): TreeNode | null {
  if (!path) return root;
  let node: TreeNode | undefined = root;
  for (const part of path.split("/")) {
    node = node?.children.find((c) => c.name === part);
    if (!node) return null;
  }
  return node;
}

/** Every file under a node, depth first. */
export function filesUnder(node: TreeNode): TreeNode[] {
  if (!node.isDir) return [node];
  return node.children.flatMap(filesUnder);
}

/** Folder path parts for a breadcrumb: "src/click" -> ["", "src", "src/click"]. */
export function crumbPaths(path: string): string[] {
  const parts = path.split("/").filter(Boolean);
  return ["", ...parts.map((_, i) => parts.slice(0, i + 1).join("/"))];
}

// -- colour buckets ---------------------------------------------------------------------------

export const BUCKETS = 5;

export interface AgeRange {
  newest: number; // unix seconds
  oldest: number;
}

/** The span of last-change times across the files (null when nothing has a date). */
export function ageRange(files: readonly SurveyFile[]): AgeRange | null {
  let newest = -Infinity;
  let oldest = Infinity;
  for (const f of files) {
    if (f[3] === null) continue;
    newest = Math.max(newest, f[3]);
    oldest = Math.min(oldest, f[3]);
  }
  return Number.isFinite(newest) ? { newest, oldest } : null;
}

/** 0 = changed most recently (the surface) … 4 = untouched the longest (deep); null when the
 *  file has no recorded change. Linear in time across the repository's own range. */
export function ageBucket(lastEpoch: number | null, range: AgeRange | null): number | null {
  if (lastEpoch === null || !range) return null;
  const span = range.newest - range.oldest;
  if (span <= 0) return 0;
  const t = (range.newest - lastEpoch) / span;
  return Math.min(BUCKETS - 1, Math.max(0, Math.floor(t * BUCKETS)));
}

/** 0 = never changed … 4 = among the most changed, on a log scale up to `max`. */
export function activityBucket(changes: number, max: number): number {
  if (changes <= 0 || max <= 0) return 0;
  if (max <= 1) return BUCKETS - 1;
  const t = Math.log1p(changes) / Math.log1p(max);
  return Math.min(BUCKETS - 1, Math.max(1, Math.ceil(t * (BUCKETS - 1))));
}

/** The time each age bucket starts at, newest first (for the legend). */
export function ageBucketStarts(range: AgeRange): number[] {
  const span = range.newest - range.oldest;
  return Array.from({ length: BUCKETS }, (_, i) => range.newest - (span * i) / BUCKETS);
}

/** Lower bound of each activity bucket (for the legend): [0, 1, …, max]. */
export function activityBucketStarts(max: number): number[] {
  if (max <= 1) return [0, 1, 1, 1, 1];
  return Array.from({ length: BUCKETS }, (_, i) =>
    i === 0 ? 0 : Math.max(1, Math.round(Math.expm1(((i - 1) / (BUCKETS - 1)) * Math.log1p(max))) + 1),
  );
}

// -- core sample --------------------------------------------------------------------------------

export interface Bin {
  label: string; // "2024-02", "2024 Q1" or "2024"
  start: string; // first month in the bin, "2024-01"
  commits: number;
}

/** Monthly counts grouped so there are at most `maxBins` bars: months, then quarters, then
 *  years. Input months are oldest first. */
export function binMonths(months: Survey["months"], maxBins = 72): Bin[] {
  if (months.length <= maxBins) return months.map((m) => ({ label: m.month, start: m.month, commits: m.commits }));
  const quarterly = months.length / 3 <= maxBins;
  const bins = new Map<string, Bin>();
  for (const m of months) {
    const [y, mo] = m.month.split("-");
    const label = quarterly ? `${y} Q${Math.floor((Number(mo) - 1) / 3) + 1}` : y;
    const bin = bins.get(label);
    if (bin) bin.commits += m.commits;
    else bins.set(label, { label, start: m.month, commits: m.commits });
  }
  return [...bins.values()];
}

// -- formatting ------------------------------------------------------------------------------

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const kb = bytes / 1024;
  if (kb < 1024) return `${kb < 10 ? kb.toFixed(1) : Math.round(kb)} KB`;
  const mb = kb / 1024;
  return `${mb < 10 ? mb.toFixed(1) : Math.round(mb)} MB`;
}

/** "20 Nov 2026" from unix seconds or an ISO string. */
export function dayMonthYear(value: number | string | null): string {
  if (value === null) return "";
  const d = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
}

/** "Nov 2026" from unix seconds. */
export function monthYearOf(epoch: number): string {
  return new Date(epoch * 1000).toLocaleDateString("en-US", { month: "short", year: "numeric", timeZone: "UTC" });
}

/** "2024-02" -> "Feb 2024" */
export function monthLabel(month: string): string {
  const [y, m] = month.split("-").map(Number);
  if (!y || !m) return month;
  return new Date(Date.UTC(y, m - 1, 1)).toLocaleDateString("en-US", { month: "short", year: "numeric", timeZone: "UTC" });
}

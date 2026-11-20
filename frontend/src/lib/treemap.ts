/** Squarified treemap layout (Bruls, Huizing & van Wijk) and the site-plan tile layout built
 *  on it: the current folder two levels deep, tiny tiles merged into "N small files", and a
 *  cap on the number of tiles. Pure functions; no DOM. */

import type { TreeNode } from "./survey";

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface Placed<T> extends Rect {
  item: T;
}

/** Lay out items (value > 0) in `rect`, area proportional to value, rows chosen to keep the
 *  tiles as square as possible. Items keep their relative order (sort them biggest first for
 *  the best result). Zero and negative values are left out. */
export function squarify<T>(items: readonly { value: number; item: T }[], rect: Rect): Placed<T>[] {
  const list = items.filter((i) => i.value > 0);
  const sum = list.reduce((s, i) => s + i.value, 0);
  if (!list.length || rect.w <= 0 || rect.h <= 0 || sum <= 0) return [];
  const scale = (rect.w * rect.h) / sum;
  const areas = list.map((i) => i.value * scale);
  const out: Placed<T>[] = [];
  let free: Rect = { ...rect };
  let i = 0;
  while (i < list.length) {
    const side = Math.min(free.w, free.h);
    if (side <= 0) break;
    let row = [areas[i]];
    let j = i + 1;
    while (j < list.length && worst([...row, areas[j]], side) <= worst(row, side)) {
      row = [...row, areas[j]];
      j++;
    }
    free = placeRow(row, list.slice(i, j).map((x) => x.item), free, out);
    i = j;
  }
  return out;
}

/** The worst aspect ratio in a row laid along a side of length `side`. */
function worst(row: number[], side: number): number {
  const s = row.reduce((a, b) => a + b, 0);
  const max = Math.max(...row);
  const min = Math.min(...row);
  const s2 = s * s;
  const w2 = side * side;
  return Math.max((w2 * max) / s2, s2 / (w2 * min));
}

function placeRow<T>(row: number[], items: T[], free: Rect, out: Placed<T>[]): Rect {
  const s = row.reduce((a, b) => a + b, 0);
  if (free.w >= free.h) {
    // a column on the left, as wide as the row's total area needs
    const w = Math.min(free.w, s / free.h);
    let y = free.y;
    row.forEach((a, k) => {
      const h = k === row.length - 1 ? free.y + free.h - y : a / w;
      out.push({ x: free.x, y, w, h, item: items[k] });
      y += h;
    });
    return { x: free.x + w, y: free.y, w: free.w - w, h: free.h };
  }
  const h = Math.min(free.h, s / free.w);
  let x = free.x;
  row.forEach((a, k) => {
    const w = k === row.length - 1 ? free.x + free.w - x : a / h;
    out.push({ x, y: free.y, w, h, item: items[k] });
    x += w;
  });
  return { x: free.x, y: free.y + h, w: free.w, h: free.h - h };
}

// -- the site plan ---------------------------------------------------------------------------

export type MapTileKind = "frame" | "dir" | "file" | "more";

export interface MapTile extends Rect {
  key: string;
  kind: MapTileKind; // frame: a folder drawn with its contents inside; dir: a folder leaf
  node: TreeNode | null; // null for "more"
  depth: 0 | 1; // 0: a child of the current folder; 1: a grandchild inside a frame
  parent: string; // the folder this tile sits in
  more?: { count: number; bytes: number }; // merged small files/folders
  label?: string; // frames: the folder path below the current one ("src/click" for a chain)
}

export interface MapOptions {
  minArea?: number; // px²; smaller tiles merge into "N small files"
  maxTiles?: number;
  gap?: number; // px between tiles
  frameHeader?: number; // px for a frame's label strip
  minFrame?: { w: number; h: number }; // smallest folder drawn with its contents
}

const DEFAULTS: Required<MapOptions> = {
  minArea: 24,
  maxTiles: 1500,
  gap: 2,
  frameHeader: 18,
  minFrame: { w: 72, h: 52 },
};

/** Tiles for `root`'s children (depth 0) and, inside folders big enough, their children
 *  (depth 1). Children too small to see are merged into one "more" tile per folder. The tile
 *  count never exceeds `maxTiles`: once the budget runs out, folders are drawn as plain
 *  leaves. */
export function layoutMap(root: TreeNode, width: number, height: number, options: MapOptions = {}): MapTile[] {
  const o = { ...DEFAULTS, ...options };
  const tiles: MapTile[] = [];
  const top = place(root, { x: 0, y: 0, w: width, h: height }, o);
  let budget = o.maxTiles - top.length;
  for (const t of top) {
    const node = t.item.node;
    const rect = inset(t, o.gap / 2);
    const canFrame =
      node !== null &&
      node.isDir &&
      node.children.length > 0 &&
      rect.w >= o.minFrame.w &&
      rect.h >= o.minFrame.h;
    if (canFrame) {
      // a folder holding just one folder ("src" -> "src/click") is drawn as that folder
      let shown = node;
      while (shown.children.length === 1 && shown.children[0].isDir && shown.children[0].children.length > 0) {
        shown = shown.children[0];
      }
      const inner: Rect = {
        x: rect.x + o.gap,
        y: rect.y + o.frameHeader,
        w: rect.w - 2 * o.gap,
        h: rect.h - o.frameHeader - o.gap,
      };
      const kids = place(shown, inner, o);
      if (kids.length <= budget) {
        budget -= kids.length;
        const frame = tile({ node: shown }, rect, "frame", 0, root.path);
        frame.label = root.path ? shown.path.slice(root.path.length + 1) : shown.path;
        tiles.push(frame);
        for (const k of kids) tiles.push(tile(k.item, inset(k, o.gap / 2), kindOf(k.item), 1, shown.path));
        continue;
      }
    }
    tiles.push(tile(t.item, rect, kindOf(t.item), 0, root.path));
  }
  return tiles;
}

interface Entry {
  node: TreeNode | null;
  more?: { count: number; bytes: number };
}

/** Squarify a folder's children into `rect`, merging the ones under `minArea`. */
function place(dir: TreeNode, rect: Rect, o: Required<MapOptions>): Placed<Entry>[] {
  const kids = dir.children.filter((c) => c.bytes > 0);
  const total = kids.reduce((s, c) => s + c.bytes, 0);
  if (!kids.length || total <= 0 || rect.w <= 0 || rect.h <= 0) return [];
  const area = rect.w * rect.h;
  const big: { value: number; item: Entry }[] = [];
  const small: TreeNode[] = [];
  for (const c of kids) {
    if ((c.bytes / total) * area < o.minArea) small.push(c);
    else big.push({ value: c.bytes, item: { node: c } });
  }
  if (small.length === 1) {
    big.push({ value: small[0].bytes, item: { node: small[0] } });
  } else if (small.length > 1) {
    const bytes = small.reduce((s, c) => s + c.bytes, 0);
    big.push({ value: bytes, item: { node: null, more: { count: small.length, bytes } } });
  }
  return squarify(big, rect);
}

function kindOf(e: Entry): MapTileKind {
  if (!e.node) return "more";
  return e.node.isDir ? "dir" : "file";
}

function tile(e: Entry, r: Rect, kind: MapTileKind, depth: 0 | 1, parent: string): MapTile {
  return {
    key: e.node ? `${kind}:${e.node.path}` : `more:${parent}:${depth}`,
    kind,
    node: e.node,
    depth,
    parent,
    more: e.more,
    x: r.x,
    y: r.y,
    w: r.w,
    h: r.h,
  };
}

function inset(r: Rect, by: number): Rect {
  const w = Math.max(0, r.w - 2 * by);
  const h = Math.max(0, r.h - 2 * by);
  return { x: r.x + by, y: r.y + by, w, h };
}

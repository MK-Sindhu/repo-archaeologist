import { describe, expect, it } from "vitest";
import type { SurveyFile } from "../api/types";
import { buildTree } from "./survey";
import { layoutMap, type Rect, squarify } from "./treemap";

const overlap = (a: Rect, b: Rect) =>
  Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x) > 1e-6 &&
  Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y) > 1e-6;

describe("squarify", () => {
  const rect = { x: 0, y: 0, w: 600, h: 400 };
  const values = [6, 6, 4, 3, 2, 2, 1];
  const tiles = squarify(values.map((v, i) => ({ value: v, item: i })), rect);

  it("gives every item an area proportional to its value and fills the rect", () => {
    const total = values.reduce((a, b) => a + b, 0);
    expect(tiles).toHaveLength(values.length);
    for (const t of tiles) expect(t.w * t.h).toBeCloseTo((values[t.item] / total) * 600 * 400, 4);
    const area = tiles.reduce((s, t) => s + t.w * t.h, 0);
    expect(area).toBeCloseTo(600 * 400, 4);
  });

  it("keeps tiles inside the rect without overlaps", () => {
    for (const t of tiles) {
      expect(t.x).toBeGreaterThanOrEqual(-1e-6);
      expect(t.y).toBeGreaterThanOrEqual(-1e-6);
      expect(t.x + t.w).toBeLessThanOrEqual(600 + 1e-6);
      expect(t.y + t.h).toBeLessThanOrEqual(400 + 1e-6);
    }
    for (let i = 0; i < tiles.length; i++)
      for (let j = i + 1; j < tiles.length; j++) expect(overlap(tiles[i], tiles[j])).toBe(false);
  });

  it("keeps aspect ratios reasonable", () => {
    for (const t of tiles) expect(Math.max(t.w / t.h, t.h / t.w)).toBeLessThan(4);
  });

  it("ignores empty input and zero values", () => {
    expect(squarify([], rect)).toEqual([]);
    expect(squarify([{ value: 0, item: 1 }], rect)).toEqual([]);
    expect(squarify([{ value: 1, item: 1 }], { x: 0, y: 0, w: 0, h: 10 })).toEqual([]);
  });
});

describe("layoutMap", () => {
  const files: SurveyFile[] = [
    ["src/a.py", 4000, 5, 100],
    ["src/b.py", 3000, 2, 90],
    ["src/c.py", 2000, 1, 80],
    ["docs/index.md", 1500, 1, 70],
    ["README.md", 500, 3, 60],
  ];
  const root = buildTree(files);

  it("draws big folders as frames with their files inside", () => {
    const tiles = layoutMap(root, 800, 500);
    const frame = tiles.find((t) => t.kind === "frame" && t.node?.path === "src");
    expect(frame).toBeTruthy();
    const inside = tiles.filter((t) => t.depth === 1 && t.parent === "src");
    expect(inside.map((t) => t.node?.name).sort()).toEqual(["a.py", "b.py", "c.py"]);
    for (const t of inside) {
      expect(t.x).toBeGreaterThanOrEqual(frame!.x - 1e-6);
      expect(t.y).toBeGreaterThanOrEqual(frame!.y - 1e-6);
      expect(t.x + t.w).toBeLessThanOrEqual(frame!.x + frame!.w + 1e-6);
      expect(t.y + t.h).toBeLessThanOrEqual(frame!.y + frame!.h + 1e-6);
    }
  });

  it("merges tiles too small to see into one 'more' tile per folder", () => {
    const many: SurveyFile[] = [["big.py", 1_000_000, 1, 1]];
    for (let i = 0; i < 300; i++) many.push([`tiny/t${i}.py`, 10, 0, null]);
    for (let i = 0; i < 40; i++) many.push([`x${i}.txt`, 5, 0, null]);
    const tiles = layoutMap(buildTree(many), 400, 300, { minArea: 24 });
    const more = tiles.filter((t) => t.kind === "more");
    expect(more.length).toBeGreaterThanOrEqual(1);
    const merged = more.reduce((s, t) => s + (t.more?.count ?? 0), 0);
    expect(merged).toBeGreaterThan(1);
    expect(tiles.length).toBeLessThan(60);
  });

  it("draws a folder holding one folder as that folder (src -> src/click)", () => {
    const chain: SurveyFile[] = [
      ["src/click/core.py", 5000, 3, 1],
      ["src/click/types.py", 4000, 1, 1],
      ["README.md", 800, 1, 1],
    ];
    const tiles = layoutMap(buildTree(chain), 800, 500);
    const frame = tiles.find((t) => t.kind === "frame")!;
    expect(frame.node?.path).toBe("src/click");
    expect(frame.label).toBe("src/click");
    expect(tiles.filter((t) => t.parent === "src/click").map((t) => t.node?.name).sort()).toEqual(["core.py", "types.py"]);
  });

  it("never exceeds the tile cap", () => {
    const many: SurveyFile[] = [];
    for (let d = 0; d < 30; d++) for (let f = 0; f < 80; f++) many.push([`d${d}/f${f}.py`, 1000 + f, 1, 1]);
    const tiles = layoutMap(buildTree(many), 1600, 1200, { maxTiles: 500, minArea: 1 });
    expect(tiles.length).toBeLessThanOrEqual(500);
    // folders that did not fit the budget are drawn as plain folder tiles
    expect(tiles.some((t) => t.kind === "dir")).toBe(true);
  });
});

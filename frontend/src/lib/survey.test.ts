import { describe, expect, it } from "vitest";
import type { SurveyFile } from "../api/types";
import {
  activityBucket,
  ageBucket,
  ageRange,
  binMonths,
  buildTree,
  crumbPaths,
  filesUnder,
  findNode,
  formatBytes,
} from "./survey";

const FILES: SurveyFile[] = [
  ["src/pkg/retry.py", 1286, 3, 300],
  ["src/pkg/util.py", 200, 1, 100],
  ["README.md", 244, 2, 400],
  ["empty.txt", 0, 0, null],
];

describe("buildTree", () => {
  const root = buildTree(FILES, new Set(["src/pkg/retry.py"]));

  it("nests files into folders with totals", () => {
    expect(root.bytes).toBe(1286 + 200 + 244);
    expect(root.files).toBe(4);
    const src = findNode(root, "src")!;
    expect(src.isDir).toBe(true);
    expect(src.bytes).toBe(1486);
    expect(src.changes).toBe(4);
    expect(src.lastEpoch).toBe(300);
    expect(findNode(root, "src/pkg")!.children.map((c) => c.name)).toEqual(["retry.py", "util.py"]);
  });

  it("marks hotspots and the folders that hold them", () => {
    expect(findNode(root, "src/pkg/retry.py")!.hot).toBe(true);
    expect(findNode(root, "src")!.hot).toBe(true);
    expect(findNode(root, "README.md")!.hot).toBe(false);
  });

  it("finds nodes, lists files and builds breadcrumbs", () => {
    expect(findNode(root, "nope")).toBeNull();
    expect(filesUnder(root).map((f) => f.path).sort()).toEqual(
      ["README.md", "empty.txt", "src/pkg/retry.py", "src/pkg/util.py"].sort(),
    );
    expect(crumbPaths("src/pkg")).toEqual(["", "src", "src/pkg"]);
    expect(crumbPaths("")).toEqual([""]);
  });
});

describe("buckets", () => {
  it("maps recent changes to the surface and old ones deep", () => {
    const range = ageRange(FILES)!;
    expect(range).toEqual({ newest: 400, oldest: 100 });
    expect(ageBucket(400, range)).toBe(0);
    expect(ageBucket(100, range)).toBe(4);
    expect(ageBucket(250, range)).toBe(2);
    expect(ageBucket(null, range)).toBeNull();
    expect(ageBucket(5, { newest: 5, oldest: 5 })).toBe(0);
  });

  it("scales activity on a log scale", () => {
    expect(activityBucket(0, 400)).toBe(0);
    expect(activityBucket(1, 400)).toBe(1);
    expect(activityBucket(400, 400)).toBe(4);
    expect(activityBucket(20, 400)).toBeGreaterThan(1);
    expect(activityBucket(1, 1)).toBe(4);
  });
});

describe("binMonths", () => {
  const months = (n: number) =>
    Array.from({ length: n }, (_, i) => {
      const y = 2010 + Math.floor(i / 12);
      return { month: `${y}-${String((i % 12) + 1).padStart(2, "0")}`, commits: 1 };
    });

  it("keeps months when they fit", () => {
    expect(binMonths(months(4))).toHaveLength(4);
  });

  it("groups into quarters, then years, keeping the totals", () => {
    const q = binMonths(months(120), 72);
    expect(q[0].label).toBe("2010 Q1");
    expect(q.reduce((s, b) => s + b.commits, 0)).toBe(120);
    const y = binMonths(months(240), 72);
    expect(y[0].label).toBe("2010");
    expect(y).toHaveLength(20);
    expect(y[0].commits).toBe(12);
  });
});

describe("formatBytes", () => {
  it("formats sizes", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(1536)).toBe("1.5 KB");
    expect(formatBytes(3 * 1024 * 1024)).toBe("3.0 MB");
  });
});

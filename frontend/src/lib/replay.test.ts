import { describe, expect, it } from "vitest";
import type { RunEvent } from "../api/types";
import { replayDelays, replayLength } from "./replay";

const step = (at: number): RunEvent => ({ type: "step", node: "retrieve", at, summary: "" });

const EVENTS: RunEvent[] = [
  { type: "status", status: "running", at: 0 },
  step(13.7), // a 13.7 s planning call
  step(13.7), // parallel searches land together...
  step(13.8),
  step(15.0),
  step(44.0), // a 29 s model call
  { type: "log", level: "warning", message: "retrying", at: 44.5 },
  { type: "status", status: "done", at: 62.6 },
];

describe("replayDelays", () => {
  it("compresses long gaps, spaces out bursts, and shows status events at once", () => {
    expect(replayDelays(EVENTS)).toEqual([0, 900, 240, 240, 300, 900, 240, 0]);
  });

  it("divides by speed and is instant at Infinity", () => {
    expect(replayDelays(EVENTS, { speed: 4 })).toEqual([0, 225, 60, 60, 75, 225, 60, 0]);
    expect(replayDelays(EVENTS, { speed: Infinity }).every((d) => d === 0)).toBe(true);
  });

  it("treats a missing or backwards `at` as no gap", () => {
    const odd: RunEvent[] = [step(5), step(3), { type: "status", status: "running", at: 0 }, step(5.2)];
    expect(replayDelays(odd, { minGapMs: 100 })).toEqual([0, 100, 0, 100]);
  });

  it("keeps a real 60-second run to a watchable length", () => {
    const ms = replayLength(replayDelays(EVENTS));
    expect(ms).toBeGreaterThan(2000);
    expect(ms).toBeLessThan(EVENTS.length * 900);
  });
});

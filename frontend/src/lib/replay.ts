/** Replaying a recorded investigation on the public site, with no API calls.
 *
 *  `replayDelays` turns the real gaps between events (seconds, from each event's `at`) into a
 *  watchable pace: compressed, clamped and divided by the chosen speed. `useReplay` steps a
 *  count through the events on that schedule: it starts from the finished frame (what the
 *  pre-rendered HTML shows), clears it and draws the dig, pauses while off-screen, can loop
 *  with a pause on the finished frame, and never autoplays under reduced motion.
 *
 *  Server-render safe: nothing touches window or document outside effects. */

import { useCallback, useEffect, useMemo, useState } from "react";
import type { RunEvent } from "../api/types";

export interface ScheduleOptions {
  speed?: number; // 1 = normal, 4 = fast, Infinity = instant
  scale?: number; // real gap × scale ...
  minGapMs?: number; // ... clamped to [minGapMs, maxGapMs]
  maxGapMs?: number;
}

function atOf(ev: RunEvent): number | null {
  return "at" in ev && typeof ev.at === "number" && Number.isFinite(ev.at) ? ev.at : null;
}

/** Delay (ms) to wait before showing each event, after the previous one. The first event and
 *  status events come at once; every other gap is the real gap × scale, clamped to
 *  [minGapMs, maxGapMs] (so a burst of parallel searches still lands one by one, and a
 *  30-second model call takes under a second), then divided by speed. */
export function replayDelays(
  events: readonly RunEvent[],
  { speed = 1, scale = 0.25, minGapMs = 240, maxGapMs = 900 }: ScheduleOptions = {},
): number[] {
  let prev = 0;
  return events.map((ev, i) => {
    const at = atOf(ev);
    const gapMs = at === null ? 0 : Math.max(0, at - prev) * 1000;
    if (at !== null) prev = Math.max(prev, at);
    if (i === 0 || ev.type === "status" || !Number.isFinite(speed) || speed <= 0) return 0;
    return Math.round(Math.min(maxGapMs, Math.max(minGapMs, gapMs * scale)) / speed);
  });
}

/** Total replay length in ms. */
export function replayLength(delays: readonly number[]): number {
  return delays.reduce((sum, d) => sum + d, 0);
}

export type ReplayPhase = "idle" | "clearing" | "playing" | "holding";

export interface ReplayOptions {
  speed?: number; // see ScheduleOptions.speed
  loop?: boolean; // start again after holding the finished frame
  autoplay?: boolean; // start when first visible (never under reduced motion)
  holdMs?: number; // how long the finished frame stays before a loop restarts
  clearMs?: number; // fade-out of the old frame before drawing starts
}

export interface Replay {
  count: number; // events shown: events.slice(0, count)
  phase: ReplayPhase;
  playing: boolean;
  done: boolean; // every event is shown
  play: () => void;
  pause: () => void;
  restart: () => void;
  finish: () => void;
  /** Attach to the element whose visibility gates playback (paused while off-screen). */
  ref: (el: Element | null) => void;
}

export function useReplay(events: readonly RunEvent[], options: ReplayOptions = {}): Replay {
  const { speed = 1, loop = false, autoplay = true, holdMs = 5000, clearMs = 320 } = options;
  const total = events.length;
  const delays = useMemo(() => replayDelays(events, { speed }), [events, speed]);

  // the finished frame first: it is what the server rendered and what reduced motion keeps
  const [count, setCount] = useState(total);
  const [phase, setPhase] = useState<ReplayPhase>("idle");
  const [playing, setPlaying] = useState(false);
  const [visible, setVisible] = useState(true);
  const [el, setEl] = useState<Element | null>(null);

  // a new recording: back to its finished frame, autoplaying again if allowed
  useEffect(() => {
    setCount(total);
    setPhase("idle");
    setPlaying(false);
    if (!autoplay) return;
    const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
    if (reduced) return;
    setPhase("clearing");
    setPlaying(true);
  }, [events, total, autoplay]);

  // pause while off-screen
  useEffect(() => {
    if (!el || typeof IntersectionObserver === "undefined") return;
    const io = new IntersectionObserver(([entry]) => setVisible(entry.isIntersecting), {
      threshold: 0.15,
    });
    io.observe(el);
    return () => io.disconnect();
  }, [el]);

  // one timer at a time drives the machine
  useEffect(() => {
    if (!playing || !visible) return;
    let wait: number;
    let step: () => void;
    if (phase === "clearing") {
      wait = clearMs;
      step = () => {
        setCount(0);
        setPhase("playing");
      };
    } else if (phase === "playing") {
      if (count >= total) {
        wait = 0;
        step = () => setPhase("holding");
      } else {
        wait = delays[count] ?? 0;
        step = () => setCount((c) => Math.min(total, c + 1));
      }
    } else if (phase === "holding") {
      if (!loop) {
        wait = 0;
        step = () => {
          setPlaying(false);
          setPhase("idle");
        };
      } else {
        wait = holdMs;
        step = () => setPhase("clearing");
      }
    } else {
      return;
    }
    const timer = setTimeout(step, wait);
    return () => clearTimeout(timer);
  }, [playing, visible, phase, count, total, delays, loop, holdMs, clearMs]);

  const restart = useCallback(() => {
    setPhase("clearing");
    setPlaying(true);
  }, []);
  const play = useCallback(() => {
    if (count >= total) {
      restart();
      return;
    }
    setPhase("playing");
    setPlaying(true);
  }, [count, total, restart]);
  const pause = useCallback(() => setPlaying(false), []);
  const finish = useCallback(() => {
    setPlaying(false);
    setPhase("idle");
    setCount(total);
  }, [total]);

  return { count, phase, playing, done: count >= total, play, pause, restart, finish, ref: setEl };
}

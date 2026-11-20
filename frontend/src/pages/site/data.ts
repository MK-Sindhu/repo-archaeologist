/** Showcase data for the public pages: recordings in /showcase/*.json.
 *
 *  Pre-rendering primes this cache synchronously (primeSiteData) and embeds what each page
 *  used as <script id="site-data" type="application/json">, which the client reads once at
 *  import time, so the first client render matches the pre-rendered HTML. Anything missing is
 *  fetched in effects. Server-render safe. */

import { useEffect, useState } from "react";
import type { ShowcaseIndex, ShowcaseRecording } from "../../api/types";
import { orderSlugs, type RecordingSummary, summarize } from "./showcase";

export interface SiteData {
  index: ShowcaseIndex | null;
  summaries: Record<string, RecordingSummary>;
  recordings: Record<string, ShowcaseRecording>;
}

const cache: SiteData = { index: null, summaries: {}, recordings: {} };

export function primeSiteData(data: Partial<SiteData>): void {
  if (data.index) cache.index = data.index;
  Object.assign(cache.summaries, data.summaries ?? {});
  for (const [slug, rec] of Object.entries(data.recordings ?? {})) {
    cache.recordings[slug] = rec;
    cache.summaries[slug] ??= summarize(rec);
  }
}

/** What a pre-rendered page embeds: the index, every summary, and the given recordings. */
export function embeddable(slugs: readonly string[]): SiteData {
  const recordings: SiteData["recordings"] = {};
  for (const s of slugs) if (cache.recordings[s]) recordings[s] = cache.recordings[s];
  return { index: cache.index, summaries: { ...cache.summaries }, recordings };
}

if (typeof document !== "undefined") {
  const el = document.getElementById("site-data");
  if (el?.textContent) {
    try {
      primeSiteData(JSON.parse(el.textContent) as SiteData);
    } catch {
      /* ignore: pages fetch what they need */
    }
  }
}

const BASE = "/showcase";
const inflight = new Map<string, Promise<unknown>>();

function getJson<T>(path: string): Promise<T> {
  let p = inflight.get(path) as Promise<T> | undefined;
  if (!p) {
    p = fetch(`${BASE}/${path}`).then((r) => {
      if (!r.ok) throw new Error(`${r.status}`);
      return r.json() as Promise<T>;
    });
    inflight.set(path, p);
    p.catch(() => inflight.delete(path));
  }
  return p;
}

async function loadIndex(): Promise<ShowcaseIndex> {
  if (cache.index) return cache.index;
  const index = await getJson<ShowcaseIndex>("index.json");
  cache.index = index;
  return index;
}

async function loadRecording(slug: string): Promise<ShowcaseRecording> {
  const hit = cache.recordings[slug];
  if (hit) return hit;
  const rec = await getJson<ShowcaseRecording>(`${encodeURIComponent(slug)}.json`);
  primeSiteData({ recordings: { [slug]: rec } });
  return rec;
}

export function slugsOf(index: ShowcaseIndex | null): string[] {
  return orderSlugs((index?.recordings ?? []).map((r) => r.slug));
}

export interface ShowcaseState {
  slugs: string[];
  summaries: RecordingSummary[];
  ready: boolean;
}

function snapshot(): ShowcaseState {
  const slugs = slugsOf(cache.index);
  const summaries = slugs.map((s) => cache.summaries[s]).filter(Boolean);
  return { slugs, summaries, ready: !!cache.index && summaries.length === slugs.length };
}

/** Every recording's summary (fetches the index and any recording it hasn't seen). */
export function useShowcase(): ShowcaseState {
  const [state, setState] = useState<ShowcaseState>(snapshot);
  useEffect(() => {
    let live = true;
    if (state.ready) return;
    loadIndex()
      .then((index) => Promise.allSettled(slugsOf(index).map(loadRecording)))
      .then(() => live && setState(snapshot()))
      .catch(() => live && setState({ ...snapshot(), ready: true }));
    return () => {
      live = false;
    };
  }, [state.ready]);
  return state;
}

export interface RecordingState {
  recording: ShowcaseRecording | null;
  missing: boolean;
}

/** One recording by slug (null slug: the hero's). */
export function useRecording(slug: string | null): RecordingState {
  const pick = (): string | null => slug ?? slugsOf(cache.index)[0] ?? null;
  const [state, setState] = useState<RecordingState>(() => {
    const s = pick();
    return { recording: s ? (cache.recordings[s] ?? null) : null, missing: false };
  });
  useEffect(() => {
    let live = true;
    const want = slug ?? null;
    (want ? Promise.resolve(want) : loadIndex().then((i) => slugsOf(i)[0] ?? null))
      .then((s) => {
        if (!s) throw new Error("no recordings");
        return loadRecording(s);
      })
      .then((rec) => live && setState({ recording: rec, missing: false }))
      .catch(() => live && setState({ recording: null, missing: true }));
    return () => {
      live = false;
    };
  }, [slug]);
  return state;
}

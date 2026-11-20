import { useEffect, useState } from "react";
import { api } from "./api/client";
import type { Status } from "./api/types";

/** Current time, ticking every `ms` while `active`. */
export function useNow(active: boolean, ms = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [active, ms]);
  return now;
}

let statusCache: Promise<Status> | null = null;

/** GET /api/status, shared across components; null until loaded or if it fails. */
export function useStatus(): Status | null {
  const [status, setStatus] = useState<Status | null>(null);
  useEffect(() => {
    let live = true;
    statusCache ??= api.status().catch((e) => {
      statusCache = null;
      throw e;
    });
    statusCache.then((s) => live && setStatus(s)).catch(() => undefined);
    return () => {
      live = false;
    };
  }, []);
  return status;
}

export function usePrefersReducedMotion(): boolean {
  const query = "(prefers-reduced-motion: reduce)";
  const [reduced, setReduced] = useState(() => window.matchMedia?.(query).matches ?? false);
  useEffect(() => {
    const mq = window.matchMedia?.(query);
    if (!mq) return;
    const on = () => setReduced(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return reduced;
}

export function useDocumentTitle(title: string): void {
  useEffect(() => {
    document.title = title ? `${title} · Archaeologist` : "Archaeologist";
  }, [title]);
}

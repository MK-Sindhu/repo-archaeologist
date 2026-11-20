import { useEffect, useState } from "react";
import { api, isNotApi, isUnreachable } from "./api/client";
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

export type ServerState =
  | { phase: "loading"; status: null }
  | { phase: "ok"; status: Status }
  | { phase: "unreachable"; status: null } // down or waking up; retrying
  | { phase: "failed"; status: null } // answered with an error; carry on without status
  | { phase: "notApi"; status: null; message: string }; // wrong/missing API address (setup)

/** GET /api/status. While the server cannot be reached (network error, 502/503/504: free
 *  hosting plans pause idle servers) it retries every `retryMs` until it answers. */
export function useServerStatus(retryMs = 5000): ServerState {
  const [state, setState] = useState<ServerState>({ phase: "loading", status: null });
  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const attempt = () => {
      api
        .status()
        .then((status) => live && setState({ phase: "ok", status }))
        .catch((err) => {
          if (!live) return;
          if (isUnreachable(err)) {
            setState({ phase: "unreachable", status: null });
            timer = setTimeout(attempt, retryMs);
          } else if (isNotApi(err)) {
            setState({ phase: "notApi", status: null, message: err.message });
          } else {
            setState({ phase: "failed", status: null });
          }
        });
    };
    attempt();
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [retryMs]);
  return state;
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

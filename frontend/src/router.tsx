/** A tiny history-API router: three routes, real links, no dependency. */

import { type AnchorHTMLAttributes, type MouseEvent, useEffect, useState } from "react";

export type Route =
  | { name: "home" }
  | { name: "repo"; id: string }
  | { name: "run"; id: string }
  | { name: "notFound" };

export function parseRoute(pathname: string): Route {
  if (pathname === "/" || pathname === "") return { name: "home" };
  const m = /^\/(repo|run)\/([^/]+)\/?$/.exec(pathname);
  if (m) {
    let id: string;
    try {
      id = decodeURIComponent(m[2]);
    } catch {
      return { name: "notFound" };
    }
    return m[1] === "repo" ? { name: "repo", id } : { name: "run", id };
  }
  return { name: "notFound" };
}

export const paths = {
  home: () => "/",
  repo: (id: string) => `/repo/${encodeURIComponent(id)}`,
  run: (id: string) => `/run/${encodeURIComponent(id)}`,
};

export function navigate(to: string, { replace = false } = {}): void {
  if (to === window.location.pathname) return;
  if (replace) window.history.replaceState(null, "", to);
  else window.history.pushState(null, "", to);
  window.dispatchEvent(new PopStateEvent("popstate"));
  window.scrollTo({ top: 0 });
}

export function useRoute(): Route {
  const [route, setRoute] = useState(() => parseRoute(window.location.pathname));
  useEffect(() => {
    const onPop = () => setRoute(parseRoute(window.location.pathname));
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  return route;
}

type LinkProps = AnchorHTMLAttributes<HTMLAnchorElement> & { to: string };

export function Link({ to, onClick, ...rest }: LinkProps) {
  const handle = (e: MouseEvent<HTMLAnchorElement>) => {
    onClick?.(e);
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    navigate(to);
  };
  return <a href={to} onClick={handle} {...rest} />;
}

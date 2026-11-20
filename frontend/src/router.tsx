/** A tiny history-API router: real links, no dependency, safe to render on the server.
 *
 *  Public site (pre-rendered, no API):  /  /how-it-works  /examples  /examples/:slug
 *                                       /self-host  /faq
 *  App (behind the access code):        /app  /app/repo/:id  /app/run/:id
 *  Legacy app URLs /repo/:id and /run/:id still parse (legacy: true) and are redirected. */

import { type AnchorHTMLAttributes, type MouseEvent, useEffect, useState } from "react";

export type SiteRoute =
  | { name: "landing" }
  | { name: "how" }
  | { name: "examples" }
  | { name: "example"; slug: string }
  | { name: "selfHost" }
  | { name: "faq" }
  | { name: "siteNotFound" };

export type AppRoute =
  | { name: "appHome" }
  | { name: "repo"; id: string; legacy?: boolean }
  | { name: "run"; id: string; legacy?: boolean }
  | { name: "notFound" };

export type Route = SiteRoute | AppRoute;

const SITE_NAMES = new Set<Route["name"]>(["landing", "how", "examples", "example", "selfHost", "faq", "siteNotFound"]);

export function isSiteRoute(route: Route): route is SiteRoute {
  return SITE_NAMES.has(route.name);
}

function decode(part: string): string | null {
  try {
    return decodeURIComponent(part);
  } catch {
    return null;
  }
}

export function parseRoute(pathname: string): Route {
  const path = pathname.length > 1 ? pathname.replace(/\/+$/, "") : pathname || "/";
  switch (path) {
    case "/":
      return { name: "landing" };
    case "/how-it-works":
      return { name: "how" };
    case "/examples":
      return { name: "examples" };
    case "/self-host":
      return { name: "selfHost" };
    case "/faq":
      return { name: "faq" };
    case "/app":
      return { name: "appHome" };
  }
  const example = /^\/examples\/([^/]+)$/.exec(path);
  if (example) {
    const slug = decode(example[1]);
    return slug ? { name: "example", slug } : { name: "siteNotFound" };
  }
  const app = /^(\/app)?\/(repo|run)\/([^/]+)$/.exec(path);
  if (app) {
    const id = decode(app[3]);
    if (!id) return { name: "notFound" };
    const legacy = !app[1];
    return app[2] === "repo" ? { name: "repo", id, legacy } : { name: "run", id, legacy };
  }
  return path.startsWith("/app/") ? { name: "notFound" } : { name: "siteNotFound" };
}

export const paths = {
  landing: () => "/",
  how: () => "/how-it-works",
  examples: () => "/examples",
  example: (slug: string) => `/examples/${encodeURIComponent(slug)}`,
  selfHost: () => "/self-host",
  faq: () => "/faq",
  app: () => "/app",
  /** @deprecated kept for older call sites: the app's home is /app */
  home: () => "/app",
  repo: (id: string) => `/app/repo/${encodeURIComponent(id)}`,
  run: (id: string) => `/app/run/${encodeURIComponent(id)}`,
};

const isBrowser = typeof window !== "undefined";

export function navigate(to: string, { replace = false } = {}): void {
  if (!isBrowser) return;
  const current = window.location.pathname + window.location.search;
  if (to === window.location.pathname || to === current) return;
  if (replace) window.history.replaceState(null, "", to);
  else window.history.pushState(null, "", to);
  window.dispatchEvent(new PopStateEvent("popstate"));
  window.scrollTo({ top: 0 });
}

/** The current route. On the server (pre-rendering) it is `initial`, as it is on the first
 *  client render when given, so server and client agree. */
export function useRoute(initial?: Route): Route {
  const [route, setRoute] = useState<Route>(
    () => initial ?? (isBrowser ? parseRoute(window.location.pathname) : { name: "landing" }),
  );
  useEffect(() => {
    const sync = () => setRoute(parseRoute(window.location.pathname));
    sync();
    window.addEventListener("popstate", sync);
    return () => window.removeEventListener("popstate", sync);
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

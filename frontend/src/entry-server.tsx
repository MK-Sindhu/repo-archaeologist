/** Server entry for pre-rendering the public pages (scripts/prerender.mjs builds this with
 *  `vite build --ssr` and calls it once per route). Never shipped to browsers. */

import { renderToString } from "react-dom/server";
import { App } from "./App";
import type { ShowcaseIndex, ShowcaseRecording } from "./api/types";
import { embeddable, primeSiteData, type SiteData, slugsOf } from "./pages/site/data";
import { type PageMeta, siteMeta } from "./pages/site/meta";
import { heroSlug, summarize } from "./pages/site/showcase";
import { isSiteRoute, parseRoute, paths, type SiteRoute } from "./router";

export function prime(index: ShowcaseIndex, recordings: Record<string, ShowcaseRecording>): void {
  primeSiteData({ index, recordings });
}

/** Every public URL to pre-render (the 404 page is rendered separately). */
export function publicRoutes(index: ShowcaseIndex): string[] {
  return [
    paths.landing(),
    paths.how(),
    paths.examples(),
    ...slugsOf(index).map(paths.example),
    paths.selfHost(),
    paths.faq(),
  ];
}

export interface Rendered {
  html: string;
  meta: PageMeta;
  data: SiteData;
}

/** Render one public URL ("/404" renders the not-found page). */
export function render(url: string, index: ShowcaseIndex, recordings: Record<string, ShowcaseRecording>): Rendered {
  const parsed = url === "/404" ? ({ name: "siteNotFound" } as const) : parseRoute(url);
  if (!isSiteRoute(parsed)) throw new Error(`${url} is not a public page`);
  const route: SiteRoute = parsed;
  const hero = heroSlug(slugsOf(index));
  let needed: string[] = [];
  if (route.name === "landing" || route.name === "how") needed = hero ? [hero] : [];
  if (route.name === "example") needed = [route.slug];
  const rec = route.name === "example" ? recordings[route.slug] : undefined;
  const meta = siteMeta(route, rec ? summarize(rec) : null);
  const html = renderToString(<App initialRoute={route} />);
  return { html, meta, data: embeddable(needed) };
}

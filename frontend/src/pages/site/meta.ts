/** Title, description and URL path of each public page, for <head> (pre-rendering) and
 *  document.title (client navigation). */

import { paths, type SiteRoute } from "../../router";
import type { RecordingSummary } from "./showcase";

export const PRODUCT = "Repo Archaeologist";

export interface PageMeta {
  title: string;
  description: string;
  path: string;
}

const DEFAULT_DESCRIPTION =
  "Ask why code is the way it is. Repo Archaeologist follows the code to the commit that changed it, the pull request behind it and the issue behind that, and cites every claim.";

export function siteMeta(route: SiteRoute, summary?: RecordingSummary | null): PageMeta {
  switch (route.name) {
    case "landing":
      return {
        title: `${PRODUCT}: why is this code like this?`,
        description: DEFAULT_DESCRIPTION,
        path: paths.landing(),
      };
    case "how":
      return {
        title: `How it works · ${PRODUCT}`,
        description:
          "Every step of an investigation on a real run: plan, search four sources at once, trace the function's history, grade the evidence, follow commit → pull request → issue, write the answer and check every claim.",
        path: paths.how(),
      };
    case "examples":
      return {
        title: `Examples · ${PRODUCT}`,
        description:
          "Real investigations on open-source repositories, recorded once and replayed in your browser: the question, the evidence at every depth, and the cited answer.",
        path: paths.examples(),
      };
    case "example":
      return {
        title: summary ? `${summary.question} · ${PRODUCT}` : `Example · ${PRODUCT}`,
        description: summary
          ? `A real investigation of ${summary.repo.slug}, replayed: ${summary.cited} sources cited, ${summary.hops} hop${summary.hops === 1 ? "" : "s"} of links followed.`
          : DEFAULT_DESCRIPTION,
        path: paths.example(route.slug),
      };
    case "selfHost":
      return {
        title: `Run it yourself · ${PRODUCT}`,
        description:
          "Run Repo Archaeologist on your machine with one script, or deploy it: a Docker image for the API, a Render blueprint, and the website on Vercel. Every setting explained.",
        path: paths.selfHost(),
      };
    case "faq":
      return {
        title: `Questions and answers · ${PRODUCT}`,
        description:
          "Where your code and keys go, which repositories and languages work, what it costs, how citations are checked, and why free-tier runs take a few minutes.",
        path: paths.faq(),
      };
    case "siteNotFound":
      return { title: `Page not found · ${PRODUCT}`, description: DEFAULT_DESCRIPTION, path: "/404" };
  }
}

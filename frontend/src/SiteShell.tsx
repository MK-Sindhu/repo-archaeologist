/** The public site (pre-rendered): marketing header and footer around the public pages. No
 *  API calls, no access code. Everything here renders on the server too: window, document
 *  and storage are read only inside effects. */

import { useEffect } from "react";
import { SiteFooter, SiteHeader, useCloseMenuOnNavigate } from "./components/site/SiteChrome";
import { useShowcase } from "./pages/site/data";
import { Example, Examples } from "./pages/site/Examples";
import { Faq } from "./pages/site/Faq";
import { HowItWorks } from "./pages/site/HowItWorks";
import { Landing } from "./pages/site/Landing";
import { siteMeta } from "./pages/site/meta";
import { SelfHost } from "./pages/site/SelfHost";
import { SiteNotFound } from "./pages/site/SiteNotFound";
import type { SiteRoute } from "./router";

function Page({ route }: { route: SiteRoute }) {
  switch (route.name) {
    case "landing":
      return <Landing />;
    case "how":
      return <HowItWorks />;
    case "examples":
      return <Examples />;
    case "example":
      return <Example key={route.slug} slug={route.slug} />;
    case "selfHost":
      return <SelfHost />;
    case "faq":
      return <Faq />;
    case "siteNotFound":
      return <SiteNotFound />;
  }
}

export default function SiteShell({ route }: { route: SiteRoute }) {
  const showcase = useShowcase();
  const key = route.name === "example" ? `example:${route.slug}` : route.name;
  const summary =
    route.name === "example" ? (showcase.summaries.find((s) => s.slug === route.slug) ?? null) : null;
  const title = siteMeta(route, summary).title;
  useEffect(() => {
    document.title = title;
  }, [title]);
  useCloseMenuOnNavigate(key);

  return (
    <div className="site-shell">
      <a className="skip" href="#content">
        Skip to content
      </a>
      <SiteHeader current={route.name} />
      <Page route={route} />
      <SiteFooter />
    </div>
  );
}

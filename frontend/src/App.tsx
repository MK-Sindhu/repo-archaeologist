/** Routes the public site and the app to their own shells. The public site renders on the
 *  server (pre-rendering) and on the client; the app shell is loaded only for /app/*. */

import { lazy, Suspense } from "react";
import SiteShell from "./SiteShell";
import { isSiteRoute, type Route, useRoute } from "./router";

const AppShell = lazy(() => import("./AppShell"));

export function App({ initialRoute }: { initialRoute?: Route }) {
  const route = useRoute(initialRoute);
  if (isSiteRoute(route)) return <SiteShell route={route} />;
  return (
    <Suspense fallback={<main className="container" aria-busy="true" />}>
      <AppShell route={route} />
    </Suspense>
  );
}

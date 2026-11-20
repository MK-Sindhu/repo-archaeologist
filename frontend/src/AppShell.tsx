/** The app behind the access code (/app/*): server status, banners, the access gate, and
 *  the app pages. Loaded lazily, so the public pages never download it. */

import { useEffect, useState, useSyncExternalStore } from "react";
import { access } from "./api/access";
import { onUnauthorized } from "./api/client";
import { apiHost } from "./api/config";
import { AccessGate } from "./components/AccessGate";
import { Header, ServerBanner, SetupBanner } from "./components/Header";
import { useServerStatus } from "./hooks";
import { Home } from "./pages/Home";
import { NotFound } from "./pages/NotFound";
import { Repo } from "./pages/Repo";
import { Run } from "./pages/Run";
import { type AppRoute, navigate, paths } from "./router";

export default function AppShell({ route }: { route: AppRoute }) {
  const server = useServerStatus();
  const status = server.status;
  const code = useSyncExternalStore(access.subscribe, access.get);
  const [refused, setRefused] = useState<string | null>(null); // a request got 401
  const [epoch, setEpoch] = useState(0); // remounts the page after unlocking, so it refetches
  useEffect(() => onUnauthorized((detail) => setRefused(detail)), []);

  // old links (/repo/:id, /run/:id) move to /app/...
  useEffect(() => {
    if ((route.name === "repo" || route.name === "run") && route.legacy) {
      const to = route.name === "repo" ? paths.repo(route.id) : paths.run(route.id);
      navigate(to + window.location.search, { replace: true });
    }
  }, [route]);

  const locked = refused !== null || (!!status?.access_required && !code);
  const waiting = server.phase === "loading" || server.phase === "unreachable";

  return (
    <>
      <a className="skip" href="#content">
        Skip to content
      </a>
      <Header
        status={status}
        onLock={status?.access_required && code && !locked ? () => access.clear() : undefined}
      />
      {server.phase === "unreachable" && <ServerBanner host={apiHost()} />}
      {server.phase === "notApi" && <SetupBanner message={server.message} />}
      <div id="content" tabIndex={-1} className="content">
        {waiting ? (
          <main className="container" aria-busy="true" />
        ) : locked ? (
          <AccessGate
            notice={refused}
            onUnlocked={(value) => {
              access.set(value);
              setRefused(null);
              setEpoch((e) => e + 1);
            }}
          />
        ) : (
          <div key={epoch} className="page">
            {route.name === "appHome" && <Home status={status} />}
            {route.name === "repo" && <Repo key={route.id} id={route.id} />}
            {route.name === "run" && <Run key={route.id} id={route.id} status={status} />}
            {route.name === "notFound" && <NotFound />}
          </div>
        )}
      </div>
    </>
  );
}

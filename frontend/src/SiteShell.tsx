/** The public site (pre-rendered): marketing header and footer around the public pages. No
 *  API calls, no access code. Everything here must render on the server: read window,
 *  document or storage only inside effects.
 *
 *  PLACEHOLDER (phase 0): the marketing builder replaces this file and adds pages/site/*. */

import { Wordmark } from "./components/Mark";
import { Link, paths, type SiteRoute } from "./router";

export default function SiteShell({ route }: { route: SiteRoute }) {
  return (
    <>
      <header className="header">
        <div className="container header__inner">
          <Link to={paths.landing()} className="wordmark">
            <Wordmark />
          </Link>
          <Link to={paths.app()} className="btn btn-primary btn-small">
            Open the app
          </Link>
        </div>
      </header>
      <main id="content" className="container">
        <p className="muted">Public page: {route.name}</p>
      </main>
    </>
  );
}

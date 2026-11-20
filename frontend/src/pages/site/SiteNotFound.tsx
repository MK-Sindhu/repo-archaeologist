import { Link, paths } from "../../router";

export function SiteNotFound() {
  return (
    <main id="content" className="site container page-head">
      <p className="eyebrow">404</p>
      <h1 className="page-head__title">Nothing at this depth</h1>
      <p className="page-head__lead">This page doesn't exist. It may have moved, or the link has a typo.</p>
      <p className="lp-hero__ctas">
        <Link to={paths.landing()} className="btn btn-secondary">
          Go to the home page
        </Link>
        <Link to={paths.examples()} className="btn btn-secondary">
          See the examples
        </Link>
      </p>
    </main>
  );
}

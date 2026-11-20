/** Header and footer of the public site. Server-render safe: the theme icon is chosen after
 *  mount (the inline head script has already applied the theme). */

import { Moon, Sun } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, paths, type SiteRoute } from "../../router";
import { currentTheme, setTheme, type Theme } from "../../theme";
import { Wordmark } from "../Mark";

export const GITHUB_URL = "https://github.com/MK-Sindhu/repo-archaeologist";

const NAV: { name: SiteRoute["name"]; label: string; to: string }[] = [
  { name: "how", label: "How it works", to: paths.how() },
  { name: "examples", label: "Examples", to: paths.examples() },
  { name: "selfHost", label: "Self-host", to: paths.selfHost() },
  { name: "faq", label: "FAQ", to: paths.faq() },
];

function isCurrent(item: SiteRoute["name"], current: SiteRoute["name"]): boolean {
  return item === current || (item === "examples" && current === "example");
}

function ThemeToggle() {
  const [theme, setThemeState] = useState<Theme | null>(null);
  useEffect(() => setThemeState(currentTheme()), []);
  const next: Theme = theme === "dark" ? "light" : "dark";
  return (
    <button
      type="button"
      className="btn-icon"
      onClick={() => {
        setTheme(next);
        setThemeState(next);
      }}
      aria-label={`Switch to ${next} theme`}
      title={`Switch to ${next} theme`}
    >
      {theme === "dark" ? <Sun size={17} aria-hidden /> : <Moon size={17} aria-hidden />}
    </button>
  );
}

export function SiteHeader({ current }: { current: SiteRoute["name"] }) {
  const links = NAV.map((item) => (
    <Link
      key={item.name}
      to={item.to}
      className="site-nav__link"
      aria-current={isCurrent(item.name, current) ? "page" : undefined}
    >
      {item.label}
    </Link>
  ));
  return (
    <header className="header site-header">
      <div className="container header__inner">
        <Link to={paths.landing()} className="wordmark" aria-label="Repo Archaeologist home">
          <Wordmark />
        </Link>
        <nav className="site-nav" aria-label="Site">
          {links}
          <a className="site-nav__link" href={GITHUB_URL}>
            GitHub
          </a>
        </nav>
        <div className="site-header__end">
          <ThemeToggle />
          <Link to={paths.app()} className="btn btn-primary btn-small site-header__cta">
            Open the app
          </Link>
          <details className="site-menu">
            <summary className="btn btn-secondary btn-small">Menu</summary>
            <nav className="site-menu__panel" aria-label="Site (menu)">
              {links}
              <a className="site-nav__link" href={GITHUB_URL}>
                GitHub
              </a>
            </nav>
          </details>
        </div>
      </div>
    </header>
  );
}

export function SiteFooter() {
  return (
    <footer className="site-footer">
      <div className="container site-footer__inner">
        <div className="site-footer__brand">
          <span className="wordmark">
            <Wordmark />
          </span>
          <p className="muted">Answers why code is the way it is, from its commits, pull requests and issues.</p>
        </div>
        <nav className="site-footer__links" aria-label="Footer">
          <Link to={paths.how()}>How it works</Link>
          <Link to={paths.examples()}>Examples</Link>
          <Link to={paths.selfHost()}>Self-host</Link>
          <Link to={paths.faq()}>FAQ</Link>
          <a href={GITHUB_URL}>Source on GitHub</a>
          <Link to={paths.app()}>Open the app</Link>
        </nav>
      </div>
    </footer>
  );
}

/** Closes the mobile menu after navigating (details stays open otherwise). */
export function useCloseMenuOnNavigate(key: string): void {
  useEffect(() => {
    document.querySelectorAll<HTMLDetailsElement>("details.site-menu[open]").forEach((d) => {
      d.open = false;
    });
  }, [key]);
}

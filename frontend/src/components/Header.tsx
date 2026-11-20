import { Lock, Moon, Sun } from "lucide-react";
import { useState } from "react";
import type { Status } from "../api/types";
import { PRODUCT } from "../hooks";
import { Link, paths } from "../router";
import { Wordmark } from "./Mark";
import { currentTheme, setTheme, type Theme } from "../theme";

export { Mark } from "./Mark";

const GITHUB_TEXT: Record<Status["github"], string> = {
  gh: "GitHub: signed in",
  token: "GitHub: token",
  none: "GitHub: not signed in (60 requests/hour)",
};

export function Header({ status, onLock }: { status: Status | null; onLock?: () => void }) {
  const [theme, setThemeState] = useState<Theme>(() => currentTheme());
  const next: Theme = theme === "dark" ? "light" : "dark";
  return (
    <>
      <header className="header">
        <div className="container header__inner">
          <Link to={paths.app()} className="wordmark" aria-label={`${PRODUCT}: your repositories`}>
            <Wordmark />
          </Link>
          <div className="header__status">
            {status && (
              <>
                <span className="mono header__model" title={`Grading with ${status.grader_model}`}>
                  {status.model}
                </span>
                <span className="header__gh">{GITHUB_TEXT[status.github]}</span>
              </>
            )}
            {onLock && (
              <button type="button" className="header__lock" onClick={onLock} title="Forget the access code on this browser">
                <Lock size={13} aria-hidden /> Lock
              </button>
            )}
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
              {theme === "dark" ? <Sun size={17} /> : <Moon size={17} />}
            </button>
          </div>
        </div>
      </header>
      {status && !status.llm_ready && (
        <div className="banner" role="alert">
          <div className="container banner__inner">
            <strong>Add an API key to ask questions.</strong>{" "}
            <span>{status.llm_hint ?? "Set GEMINI_API_KEY or ANTHROPIC_API_KEY in .env and restart."}</span>
          </div>
        </div>
      )}
    </>
  );
}

/** The website reached something that is not its API: a deployment setting to fix. */
export function SetupBanner({ message }: { message: string }) {
  return (
    <div className="banner" role="alert">
      <div className="container banner__inner">
        <span>{message}</span>
      </div>
    </div>
  );
}

/** The API did not answer: probably a free-plan server waking up. Retries run in the app. */
export function ServerBanner({ host }: { host: string | null }) {
  return (
    <div className="banner banner--waiting" role="status" aria-live="polite">
      <div className="container banner__inner">
        <span className="live-dot" aria-hidden />
        <span>
          Can't reach the server{host ? <> at <span className="mono">{host}</span></> : null}. Free hosting
          plans pause idle servers; it can take up to a minute to wake. Retrying…
        </span>
      </div>
    </div>
  );
}

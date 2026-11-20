import { Moon, Sun } from "lucide-react";
import { useState } from "react";
import type { Status } from "../api/types";
import { Link, paths } from "../router";
import { currentTheme, setTheme, type Theme } from "../theme";

/** Four bands, deepest flagged: the product mark. */
export function Mark({ size = 16 }: { size?: number }) {
  return (
    <svg className="mark" width={size} height={size} viewBox="0 0 16 16" aria-hidden focusable="false">
      <rect y="0" width="16" height="4" fill="var(--stratum-1)" />
      <rect y="4" width="16" height="4" fill="var(--stratum-2)" />
      <rect y="8" width="16" height="4" fill="var(--stratum-3)" />
      <rect y="12" width="16" height="4" fill="var(--gley-deep)" />
      <circle cx="11" cy="14" r="1.6" fill="var(--flag)" />
    </svg>
  );
}

const GITHUB_TEXT: Record<Status["github"], string> = {
  gh: "GitHub: signed in",
  token: "GitHub: token",
  none: "GitHub: not signed in (60 requests/hour)",
};

export function Header({ status }: { status: Status | null }) {
  const [theme, setThemeState] = useState<Theme>(() => currentTheme());
  const next: Theme = theme === "dark" ? "light" : "dark";
  return (
    <>
      <header className="header">
        <div className="container header__inner">
          <Link to={paths.home()} className="wordmark" aria-label="Archaeologist home">
            <Mark />
            <span>Archaeologist</span>
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

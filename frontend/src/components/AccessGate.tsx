/** Shown in place of the page when the API needs an access code the browser doesn't have
 *  (or the stored one was refused). */

import { type FormEvent, useState } from "react";
import { ApiError, checkAccess, isUnreachable } from "../api/client";
import { useDocumentTitle } from "../hooks";

interface Props {
  notice: string | null; // why the gate appeared, e.g. the server's 401 detail
  onUnlocked: (code: string) => void;
}

export function AccessGate({ notice, onUnlocked }: Props) {
  useDocumentTitle("Private");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(notice);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const value = code.trim();
    if (!value) {
      setError("Enter the access code.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await checkAccess(value);
      onUnlocked(value);
    } catch (err) {
      setBusy(false);
      if (err instanceof ApiError && err.status === 401) setError(err.detail ?? "That code is not right.");
      else if (isUnreachable(err)) setError("Can't reach the server right now. Try again in a moment.");
      else setError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <main className="container gate">
      <h1 className="display gate__title">This site is private</h1>
      <p className="gate__text">Enter the access code to open repositories and ask questions.</p>
      <form className="open gate__form" onSubmit={submit} noValidate>
        <label htmlFor="access-code" className="open__label">
          Access code
        </label>
        <div className="open__row">
          <input
            id="access-code"
            type="password"
            className="field"
            value={code}
            onChange={(e) => setCode(e.target.value)}
            autoComplete="current-password"
            autoFocus
            disabled={busy}
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? "access-error" : undefined}
          />
          <button type="submit" className="btn btn-primary" disabled={busy}>
            Unlock
          </button>
        </div>
        {error && (
          <p id="access-error" className="error-text" role="alert">
            {error}
          </p>
        )}
      </form>
    </main>
  );
}

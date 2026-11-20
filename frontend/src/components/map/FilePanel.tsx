/** The file panel: a file's functions and classes, its code, and the biography of the selected
 *  symbol (or of the whole file), with "Ask why" handing a question to the Ask tab. */

import { ArrowUpRight, MessageCircleQuestion, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../../api/client";
import type { CodeSlice, HistoryResponse, OutlineResponse, OutlineSymbol } from "../../api/types";
import { plural } from "../../lib/format";
import { Biography } from "./Biography";

type Outline = Extract<OutlineResponse, { status: "ready" }>;
type History = Extract<HistoryResponse, { status: "ready" }>;

const FILE_HEAD = 160; // lines shown when no symbol is selected
const KIND_TEXT: Record<OutlineSymbol["kind"], string> = {
  class: "class",
  function: "function",
  method: "method",
  interface: "interface",
  type: "type",
  enum: "enum",
  module: "module",
  other: "symbol",
};

interface Props {
  repoId: string;
  path: string;
  symbol: string | null;
  webUrl: string | null;
  head: string | null;
  thin: boolean;
  onSymbol: (qualified: string | null) => void;
  onClose: () => void;
  onAskWhy: (question: string) => void;
}

/** Poll an endpoint that may answer {status: "pending"} while the repository is being indexed. */
function usePending<T extends { status: string }>(load: (() => Promise<T>) | null, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!load) return;
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    setData(null);
    setError(null);
    const run = () =>
      load()
        .then((d) => {
          if (!live) return;
          setData(d);
          if (d.status === "pending") timer = setTimeout(run, 3000);
        })
        .catch((e) => live && setError(e instanceof Error ? e.message : String(e)));
    run();
    return () => {
      live = false;
      clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return { data, error };
}

export function FilePanel({ repoId, path, symbol, webUrl, head, thin, onSymbol, onClose, onAskWhy }: Props) {
  const close = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    close.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [path, onClose]);

  const outline = usePending(() => api.outline(repoId, path), [repoId, path]);
  const ready = outline.data?.status === "ready" ? (outline.data as Outline) : null;
  const sym = ready?.symbols.find((s) => s.qualified === symbol) ?? null;
  const symbolKnown = !symbol || !ready || !!sym; // stale ?symbol= in the URL

  const [code, setCode] = useState<CodeSlice | null>(null);
  const [codeError, setCodeError] = useState<string | null>(null);
  useEffect(() => {
    if (!ready && outline.data?.status !== "ready" && !outline.error) return;
    let live = true;
    setCode(null);
    setCodeError(null);
    api
      .code(repoId, path, sym?.start ?? 1, sym ? sym.end : FILE_HEAD)
      .then((c) => live && setCode(c))
      .catch((e) => live && setCodeError(e instanceof Error ? e.message : String(e)));
    return () => {
      live = false;
    };
  }, [repoId, path, sym?.start, sym?.end, ready, outline.error]);

  const [resolve, setResolve] = useState(false);
  useEffect(() => setResolve(false), [path, symbol]);
  const history = usePending(
    ready || outline.error
      ? () => api.history(repoId, { path, symbol: sym?.qualified ?? null, start: sym?.start, end: sym?.end, resolve })
      : null,
    [repoId, path, sym?.qualified, resolve, !!ready, !!outline.error],
  );
  const fresh = history.data?.status === "ready" ? (history.data as History) : null;
  const last = useRef<{ key: string; hist: History } | null>(null);
  const histKey = `${path}\u0000${sym?.qualified ?? ""}`;
  if (fresh) last.current = { key: histKey, hist: fresh };
  const hist = fresh ?? (resolve && last.current?.key === histKey ? last.current.hist : null);

  const fileName = path.split("/").pop() ?? path;
  const subject = sym ? sym.qualified : fileName;
  const ghUrl = webUrl && head ? `${webUrl}/blob/${head}/${path}${sym ? `#L${sym.start}-L${sym.end}` : ""}` : null;
  const pending = outline.data?.status === "pending";

  return (
    <div className="drawer" role="presentation" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <aside className="drawer__panel fpanel" role="dialog" aria-modal="true" aria-labelledby="fpanel-title">
        <header className="drawer__head">
          <p className="drawer__no mono">
            File
            {ready?.language && <span className="drawer__kind">{ready.language}</span>}
          </p>
          <button ref={close} type="button" className="btn-icon" onClick={onClose} aria-label="Close">
            <X size={18} />
          </button>
        </header>
        <h2 id="fpanel-title" className="fpanel__title mono">
          {path.includes("/") && <span className="fpanel__dir">{path.slice(0, path.lastIndexOf("/") + 1)}</span>}
          {fileName}
        </h2>
        {ghUrl && (
          <a className="fpanel__gh" href={ghUrl} target="_blank" rel="noreferrer">
            Open on GitHub <ArrowUpRight size={13} aria-hidden />
          </a>
        )}

        {pending && <p className="note">The repository is being indexed; this panel fills in when that finishes.</p>}
        {outline.error && <p className="error-text">{outline.error}</p>}

        {ready && (
          <section className="fpanel__section" aria-labelledby="outline-title">
            <h3 id="outline-title" className="eyebrow">
              {ready.supported ? `Functions and classes · ${ready.symbols.length}` : "Functions and classes"}
            </h3>
            {!ready.supported ? (
              <p className="muted fpanel__muted">No outline for this language yet. The code and its history are below.</p>
            ) : ready.symbols.length === 0 ? (
              <p className="muted fpanel__muted">This file defines no functions or classes.</p>
            ) : (
              <ul className="outline">
                <li>
                  <button
                    type="button"
                    className={!sym ? "outline__item is-selected" : "outline__item"}
                    aria-pressed={!sym}
                    onClick={() => onSymbol(null)}
                  >
                    <span className="outline__kind">file</span>
                    <span className="outline__name">Whole file</span>
                  </button>
                </li>
                {ready.symbols.map((s) => (
                  <li key={`${s.qualified}:${s.start}`}>
                    <button
                      type="button"
                      className={[
                        "outline__item",
                        s.qualified === sym?.qualified && "is-selected",
                        s.kind === "method" && "is-member",
                      ]
                        .filter(Boolean)
                        .join(" ")}
                      aria-pressed={s.qualified === sym?.qualified}
                      onClick={() => onSymbol(s.qualified)}
                    >
                      <span className="outline__kind">{KIND_TEXT[s.kind]}</span>
                      <span className="outline__name mono">{s.kind === "method" ? s.name : s.qualified}</span>
                      <span className="outline__lines mono">
                        {s.start === s.end ? `L${s.start}` : `L${s.start}–${s.end}`}
                      </span>
                      {s.doc && <span className="outline__doc">{s.doc}</span>}
                    </button>
                  </li>
                ))}
              </ul>
            )}
            {!symbolKnown && <p className="note">{symbol} is no longer in this file.</p>}
          </section>
        )}

        {sym && (
          <div className="fpanel__ask">
            <button
              type="button"
              className="btn btn-secondary btn-small"
              onClick={() => onAskWhy(`Why does \`${sym.qualified}\` look the way it does?`)}
            >
              <MessageCircleQuestion size={14} aria-hidden /> Ask why
            </button>
            <span className="muted">Starts a question about {sym.qualified} in the Ask tab.</span>
          </div>
        )}

        {(ready || outline.error) && (
          <section className="fpanel__section" aria-labelledby="code-title">
            <h3 id="code-title" className="eyebrow">
              {sym ? `Code · ${sym.qualified}` : "Code"}
            </h3>
            {codeError && <p className="error-text">{codeError}</p>}
            {code && <CodeView code={code} />}
            {code && !sym && code.end < code.total_lines && (
              <p className="muted fpanel__muted">
                Lines 1–{code.end} of {plural(code.total_lines, "line")}.
              </p>
            )}
          </section>
        )}

        {(ready || outline.error) && (
          <section className="fpanel__section" aria-labelledby="bio-title">
            <h3 id="bio-title" className="eyebrow">
              History of {subject}
            </h3>
            {history.error && <p className="error-text">{history.error}</p>}
            {!hist && !history.error && <p className="muted fpanel__muted">Reading the history…</p>}
            {hist && (
              <Biography
                history={hist}
                subject={subject}
                thin={thin}
                resolving={resolve && !fresh && !history.error}
                onResolve={() => setResolve(true)}
              />
            )}
          </section>
        )}
      </aside>
    </div>
  );
}

function CodeView({ code }: { code: CodeSlice }) {
  const lines = code.text.split("\n");
  const width = String(code.start + lines.length - 1).length;
  return (
    <pre className="code" tabIndex={0} aria-label={`Lines ${code.start} to ${code.end} of ${code.path}`}>
      <code>
        {lines.map((line, i) => (
          <span key={i} className="code__line">
            <span className="code__no" aria-hidden>
              {String(code.start + i).padStart(width, " ")}
            </span>
            {line || " "}
            {"\n"}
          </span>
        ))}
      </code>
    </pre>
  );
}

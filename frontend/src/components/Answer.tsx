/** The answer as a field report: markdown in Literata with every citation turned into a
 *  find-tag chip that links to the evidence in the section. */

import { type ReactNode, useLayoutEffect, useMemo, useRef, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Find } from "../api/types";
import { findIdFromHref, linkCitations } from "../lib/citations";
import { accessionLabel } from "../lib/runModel";
import { KIND_ICON, monthYear } from "./FindTag";

interface Props {
  markdown: string;
  finds: ReadonlyMap<string, Find>;
  accession: ReadonlyMap<string, number>;
  highlight: string | null;
  onHover: (id: string | null) => void;
  onSelect: (id: string) => void;
}

interface Pop {
  id: string;
  rect: DOMRect;
}

export function AnswerBody({ markdown, finds, accession, highlight, onHover, onSelect }: Props) {
  const [pop, setPop] = useState<Pop | null>(null);
  const linked = useMemo(
    () => linkCitations(markdown, (id) => (finds.has(id) ? accessionLabel(accession.get(id)) : undefined)),
    [markdown, finds, accession],
  );

  const show = (id: string, el: HTMLElement) => {
    onHover(id);
    setPop({ id, rect: el.getBoundingClientRect() });
  };
  const hide = () => {
    onHover(null);
    setPop(null);
  };

  const components = useMemo(
    () => ({
      a: ({ href, children }: { href?: string; children?: ReactNode }) => {
        const id = findIdFromHref(href);
        if (!id) {
          return (
            <a href={href} target="_blank" rel="noreferrer">
              {children}
            </a>
          );
        }
        const find = finds.get(id);
        // a real inline link (not a button, which is an atomic box the line may break after)
        return (
          <a
            href={href}
            className={highlight === id ? "chip is-highlighted" : "chip"}
            aria-label={`${accessionLabel(accession.get(id))}: ${find?.title ?? id}. Show evidence`}
            onMouseEnter={(e) => show(id, e.currentTarget)}
            onMouseLeave={hide}
            onFocus={(e) => show(id, e.currentTarget)}
            onBlur={hide}
            onClick={(e) => {
              e.preventDefault();
              onSelect(id);
            }}
          >
            {children}
          </a>
        );
      },
    }),
    [finds, accession, highlight, onSelect],
  );

  return (
    <div className="answer__body prose">
      <Markdown remarkPlugins={[remarkGfm]} components={components}>
        {linked}
      </Markdown>
      {pop && finds.get(pop.id) && <ChipPopover find={finds.get(pop.id)!} rect={pop.rect} />}
    </div>
  );
}

function ChipPopover({ find, rect }: { find: Find; rect: DOMRect }) {
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const w = el.offsetWidth;
    const h = el.offsetHeight;
    const margin = 12;
    const left = Math.min(Math.max(margin, rect.left), window.innerWidth - w - margin);
    const below = rect.bottom + 8;
    const top = below + h > window.innerHeight - margin ? rect.top - h - 8 : below;
    setPos({ left, top });
  }, [rect]);
  const Icon = KIND_ICON[find.kind];
  return (
    <div
      ref={ref}
      className="chip-pop"
      role="tooltip"
      style={pos ? { left: pos.left, top: pos.top } : { left: -9999, top: 0 }}
    >
      <p className="chip-pop__head">
        <Icon size={13} aria-hidden />
        <span>{find.title}</span>
      </p>
      {find.date && <p className="chip-pop__date mono">{monthYear(find.date)}</p>}
      {find.excerpt && <p className="chip-pop__excerpt">{find.excerpt.slice(0, 280)}</p>}
    </div>
  );
}

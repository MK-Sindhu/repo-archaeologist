/** SVG connectors between finds in different strata (a Harris matrix drawn over the section).
 *  Positions are measured from the DOM and re-measured whenever the section resizes, finds are
 *  added or removed, or an arrival animation ends. */

import { useLayoutEffect, useRef, useState } from "react";
import type { Edge } from "../lib/links";

interface PathSpec {
  key: string;
  d: string;
  cited: boolean;
  active: boolean;
}

interface Props {
  edges: Edge[];
  cited: ReadonlySet<string>;
  highlight?: string | null;
}

const R = 7; // elbow radius

function elbow(x1: number, y1: number, x2: number, y2: number): string {
  // turn just below the source tag, inside its layer's padding; labels mask the drop
  const ym = Math.round(Math.min(y1 + 12, (y1 + y2) / 2));
  const dx = x2 - x1;
  if (Math.abs(dx) < 2 * R || y2 - y1 < 2 * R) return `M${x1} ${y1} L${x2} ${y2}`;
  const s = Math.sign(dx);
  return [
    `M${x1} ${y1}`,
    `V${ym - R}`,
    `Q${x1} ${ym} ${x1 + s * R} ${ym}`,
    `H${x2 - s * R}`,
    `Q${x2} ${ym} ${x2} ${ym + R}`,
    `V${y2}`,
  ].join(" ");
}

function cssEscape(value: string): string {
  return typeof CSS !== "undefined" && CSS.escape ? CSS.escape(value) : value.replace(/["\\]/g, "\\$&");
}

/** Render as the last child of the positioned element that contains the finds; that parent
 *  is the coordinate space (a ref on the parent would not be attached yet when this child's
 *  layout effect runs). */
export function HarrisLines({ edges, cited, highlight }: Props) {
  const svg = useRef<SVGSVGElement>(null);
  const [paths, setPaths] = useState<PathSpec[]>([]);
  const [size, setSize] = useState({ w: 0, h: 0 });

  useLayoutEffect(() => {
    const root = svg.current?.parentElement;
    if (!root) return;
    let frame = 0;
    let lastKey = "";

    const measure = () => {
      frame = 0;
      const box = root.getBoundingClientRect();
      const find = (id: string) =>
        root.querySelector<HTMLElement>(`[data-find-id="${cssEscape(id)}"]`);
      // spread several connectors leaving or entering one tag across its width
      const outs = new Map<string, string[]>();
      const ins = new Map<string, string[]>();
      for (const e of edges) {
        outs.set(e.from, [...(outs.get(e.from) ?? []), e.to]);
        ins.set(e.to, [...(ins.get(e.to) ?? []), e.from]);
      }
      const next: PathSpec[] = [];
      for (const e of edges) {
        const a = find(e.from);
        const b = find(e.to);
        if (!a || !b || !a.offsetParent || !b.offsetParent) continue;
        const ra = a.getBoundingClientRect();
        const rb = b.getBoundingClientRect();
        const ao = outs.get(e.from)!;
        const bi = ins.get(e.to)!;
        const x1 = Math.round(ra.left - box.left + (ra.width * (ao.indexOf(e.to) + 1)) / (ao.length + 1));
        const y1 = Math.round(ra.bottom - box.top);
        const x2 = Math.round(rb.left - box.left + (rb.width * (bi.indexOf(e.from) + 1)) / (bi.length + 1));
        const y2 = Math.round(rb.top - box.top);
        if (y2 <= y1) continue;
        next.push({
          key: `${e.from}>${e.to}`,
          d: elbow(x1, y1, x2, y2),
          cited: cited.has(e.from) && cited.has(e.to),
          active: highlight === e.from || highlight === e.to,
        });
      }
      const key = `${box.width}x${box.height}|${next.map((p) => p.key + p.d + p.cited + p.active).join(";")}`;
      if (key === lastKey) return;
      lastKey = key;
      setSize({ w: Math.round(box.width), h: Math.round(box.height) });
      setPaths(next);
    };
    const schedule = () => {
      if (!frame) frame = requestAnimationFrame(measure);
    };

    measure();
    const ro = new ResizeObserver(schedule);
    ro.observe(root);
    const strata = root.querySelector(".strata");
    const mo = new MutationObserver(schedule);
    if (strata) mo.observe(strata, { childList: true, subtree: true });
    root.addEventListener("animationend", schedule);
    root.addEventListener("transitionend", schedule);
    window.addEventListener("resize", schedule);
    document.fonts?.ready.then(schedule).catch(() => undefined);
    return () => {
      if (frame) cancelAnimationFrame(frame);
      ro.disconnect();
      mo.disconnect();
      root.removeEventListener("animationend", schedule);
      root.removeEventListener("transitionend", schedule);
      window.removeEventListener("resize", schedule);
    };
  }, [edges, cited, highlight]);

  return (
    <svg ref={svg} className="harris" width={size.w} height={size.h} aria-hidden focusable="false">
      {paths.map((p) => (
        <path
          key={p.key}
          d={p.d}
          pathLength={1}
          className={[p.cited && "is-cited", p.active && "is-active"].filter(Boolean).join(" ") || undefined}
        />
      ))}
    </svg>
  );
}

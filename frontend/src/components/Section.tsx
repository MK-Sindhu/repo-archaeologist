/** The stratigraphic section: evidence laid out in layers, present at the top, the original
 *  need at the bottom, with Harris-matrix lines between linked finds. Used live on the run
 *  page and as the illustrated example on the home page. */

import { type CSSProperties, useState } from "react";
import type { Edge } from "../lib/links";
import type { FindState, LayerView } from "../lib/runModel";
import { FindTag } from "./FindTag";
import { HarrisLines } from "./HarrisLines";

interface Props {
  layers: LayerView[];
  stateOf: (id: string) => FindState;
  gradeOf: (id: string) => number | null;
  accession: ReadonlyMap<string, number>;
  edges: Edge[];
  cited: ReadonlySet<string>;
  highlight?: string | null;
  selected?: string | null;
  onHover?: (id: string | null) => void;
  onSelect?: (id: string) => void;
  /** text for a layer with nothing in it */
  emptyText?: (layerKey: LayerView["key"]) => string;
  intro?: boolean; // one-time draw-in (home page)
  monoIds?: ReadonlySet<string>;
  staircase?: boolean; // offset each layer's tags further right (home page)
  label: string;
}

export function Section({
  layers,
  stateOf,
  gradeOf,
  accession,
  edges,
  cited,
  highlight,
  selected,
  onHover,
  onSelect,
  emptyText,
  intro,
  monoIds,
  staircase,
  label,
}: Props) {
  const [open, setOpen] = useState<Record<string, boolean>>({});

  return (
    <section className={intro ? "section section--intro" : "section"} aria-label={label}>
      <div className="section__body">
        <ol className="strata">
          {layers.map((layer, i) => {
            const expanded = open[layer.key] ?? false;
            const visible = expanded ? [...layer.shown, ...layer.dropped] : layer.shown;
            const empty = !layer.shown.length && !layer.dropped.length;
            return (
              <li
                key={layer.key}
                className={`stratum stratum--${i}`}
                style={{ "--i": i } as CSSProperties}
              >
                <h3 className="stratum__label eyebrow">{layer.label}</h3>
                <div className="stratum__finds">
                  {visible.map((f, j) => (
                    <FindTag
                      key={f.id}
                      find={f}
                      number={accession.get(f.id)}
                      state={stateOf(f.id)}
                      grade={gradeOf(f.id)}
                      highlighted={highlight === f.id}
                      selected={selected === f.id}
                      mono={monoIds?.has(f.id)}
                      onHover={onHover}
                      onSelect={onSelect}
                      style={staircase ? ({ marginLeft: `${i * 9 + j * 2}%` } as CSSProperties) : undefined}
                    />
                  ))}
                  {empty && emptyText && <p className="stratum__empty">{emptyText(layer.key)}</p>}
                  {layer.dropped.length > 0 && (
                    <button
                      type="button"
                      className="stratum__more"
                      aria-expanded={expanded}
                      onClick={() => setOpen((o) => ({ ...o, [layer.key]: !expanded }))}
                    >
                      {expanded ? "Hide not relevant" : `+${layer.dropped.length} not relevant`}
                    </button>
                  )}
                </div>
              </li>
            );
          })}
        </ol>
        <HarrisLines edges={edges} cited={cited} highlight={highlight} />
      </div>
    </section>
  );
}

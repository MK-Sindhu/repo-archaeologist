/** Edges of the Harris matrix: which finds link to which, across strata.
 *
 * A find's `refs` name other evidence by canonical ref. A ref matches a find when:
 * - the ids are equal;
 * - numbered refs: `gh:N` matches `pr:N` or `issue:N`, and `pr:N` / `issue:N` fall back to
 *   the other kind (GitHub shares one number space, so "Fixes #7" may name a PR);
 * - commits: one sha is a prefix of the other and the shorter has at least 7 characters.
 * Only links between different strata become edges; each edge runs from the shallower find
 * to the deeper one, and each pair appears once. As in a Harris matrix, redundant relations
 * are left out: a → c is dropped when a chain a → b → … → c already connects them.
 */

import type { Find } from "../api/types";
import { layerIndex } from "./runModel";

export interface Edge {
  from: string; // shallower find id
  to: string; // deeper find id
}

const NUMBERED = /^(pr|issue|gh):(\d+)$/;
const COMMIT = /^commit:([0-9a-fA-F]+)$/;

export function resolveRef(ref: string, ids: ReadonlySet<string>): string | null {
  if (ids.has(ref)) return ref;
  const num = NUMBERED.exec(ref);
  if (num) {
    const [, kind, n] = num;
    const order = kind === "pr" ? ["pr", "issue"] : kind === "issue" ? ["issue", "pr"] : ["pr", "issue"];
    for (const k of order) if (ids.has(`${k}:${n}`)) return `${k}:${n}`;
    return null;
  }
  const commit = COMMIT.exec(ref);
  if (commit) {
    const sha = commit[1].toLowerCase();
    for (const id of ids) {
      const m = COMMIT.exec(id);
      if (!m) continue;
      const other = m[1].toLowerCase();
      const shorter = Math.min(sha.length, other.length);
      if (shorter >= 7 && (other.startsWith(sha) || sha.startsWith(other))) return id;
    }
  }
  return null;
}

export function edges(finds: Iterable<Find>, { reduce = true } = {}): Edge[] {
  const list = [...finds];
  const ids = new Set(list.map((f) => f.id));
  const kindOf = new Map(list.map((f) => [f.id, f.kind]));
  const seen = new Set<string>();
  const out: Edge[] = [];
  for (const f of list) {
    for (const ref of f.refs) {
      const target = resolveRef(ref, ids);
      if (!target || target === f.id) continue;
      const la = layerIndex(f.kind);
      const lb = layerIndex(kindOf.get(target)!);
      if (la === lb) continue;
      const [from, to] = la < lb ? [f.id, target] : [target, f.id];
      const key = `${from}\u0000${to}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({ from, to });
    }
  }
  return reduce ? transitiveReduction(out) : out;
}

/** Drop every edge u → v that is implied by a longer path from u to v (edges form a DAG:
 *  they always point to a deeper stratum). */
export function transitiveReduction(list: Edge[]): Edge[] {
  const next = new Map<string, string[]>();
  for (const e of list) next.set(e.from, [...(next.get(e.from) ?? []), e.to]);
  const implied = (u: string, v: string) => {
    const stack = (next.get(u) ?? []).filter((c) => c !== v);
    const seen = new Set(stack);
    while (stack.length) {
      const n = stack.pop()!;
      if (n === v) return true;
      for (const c of next.get(n) ?? []) {
        if (!seen.has(c)) {
          seen.add(c);
          stack.push(c);
        }
      }
    }
    return false;
  };
  return list.filter((e) => !implied(e.from, e.to));
}

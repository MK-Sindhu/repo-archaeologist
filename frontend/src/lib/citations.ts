/** Inline citations in answer markdown.
 *
 * The model cites evidence as `[<canonical ref>]`, e.g. `[commit:c6d98fc…]`, `[pr:12]`,
 * `[code:src/pkg/retry.py#L8-L13]`, sometimes several in one bracket (`[pr:12, issue:7]`).
 * `linkCitations` rewrites every known citation into a markdown link `[F-03](#find:<id>)`
 * that the renderer turns into a find-tag chip. Code spans and fenced blocks are left alone,
 * as are real markdown links and anything that is not a citation.
 */

const REF = String.raw`(?:commit:[0-9a-fA-F]{7,40}|(?:pr|issue|gh):\d+|code:[^\s\],;]+|doc:[^\s\],;]+)`;
const REF_RE = new RegExp(`^${REF}$`);
// a bracket group not part of a link/image: not preceded by "]" or "!", not followed by "(" or "["
const GROUP_RE = /(?<![\]!\\])\[([^[\]\n]+)\](?![([])/g;
const SEGMENT_RE = /(```[\s\S]*?(?:```|$)|`[^`\n]*`)/g;

export const FIND_HREF = "#find:";

export function isRef(text: string): boolean {
  return REF_RE.test(text.trim());
}

/** Ids in one bracket group, or null when the group is not purely citations. */
export function parseGroup(inner: string): string[] | null {
  const parts = inner.split(/\s*[,;]\s*/).map((p) => p.trim()).filter(Boolean);
  if (!parts.length || !parts.every(isRef)) return null;
  return parts;
}

/** Rewrite citations to `[label](#find:<id>)` links. `label(id)` returns the chip label
 *  (e.g. "F-03") for a known id, or undefined to leave that id as plain text. */
export function linkCitations(markdown: string, label: (id: string) => string | undefined): string {
  return markdown
    .split(SEGMENT_RE)
    .map((segment, i) => (i % 2 === 1 ? segment : linkSegment(segment, label)))
    .join("");
}

function linkSegment(text: string, label: (id: string) => string | undefined): string {
  return text.replace(GROUP_RE, (whole: string, inner: string) => {
    const ids = parseGroup(inner);
    if (!ids) return whole;
    return ids
      .map((id) => {
        const l = label(id);
        return l ? `[${l}](${FIND_HREF}${encodeURIComponent(id)})` : `[${id}]`;
      })
      .join(" ");
  });
}

/** The find id behind a chip link's href, or null for an ordinary link. */
export function findIdFromHref(href: string | undefined): string | null {
  if (!href || !href.startsWith(FIND_HREF)) return null;
  try {
    return decodeURIComponent(href.slice(FIND_HREF.length));
  } catch {
    return null;
  }
}

/** Every cited id in the markdown, in order of first appearance (outside code). */
export function citedIds(markdown: string): string[] {
  const out: string[] = [];
  markdown.split(SEGMENT_RE).forEach((segment, i) => {
    if (i % 2 === 1) return;
    for (const m of segment.matchAll(GROUP_RE)) {
      for (const id of parseGroup(m[1]) ?? []) if (!out.includes(id)) out.push(id);
    }
  });
  return out;
}

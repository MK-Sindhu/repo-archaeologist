import {
  CircleDot,
  CodeXml,
  FileText,
  GitCommitHorizontal,
  GitPullRequest,
  type LucideIcon,
  MessageSquare,
} from "lucide-react";
import type { CSSProperties } from "react";
import type { EvidenceKind, Find } from "../api/types";
import type { FindState } from "../lib/runModel";
import { accessionLabel } from "../lib/runModel";

export const KIND_ICON: Record<EvidenceKind, LucideIcon> = {
  code: CodeXml,
  doc: FileText,
  commit: GitCommitHorizontal,
  pr: GitPullRequest,
  issue: CircleDot,
  comment: MessageSquare,
};

export const KIND_LABEL: Record<EvidenceKind, string> = {
  code: "code",
  doc: "doc",
  commit: "commit",
  pr: "pull request",
  issue: "issue",
  comment: "comment",
};

const STATE_LABEL: Record<FindState, string> = {
  retrieved: "not graded yet",
  kept: "relevant",
  cited: "cited in the answer",
  dropped: "not relevant",
};

/** Compact identifier for a find: short sha, #number, or file and lines. */
export function shortId(find: Find): string {
  const [kind, ...rest] = find.id.split(":");
  const value = rest.join(":");
  if (kind === "commit") return find.meta.short_sha ?? value.slice(0, 7);
  if (kind === "pr" || kind === "issue" || kind === "gh") return `#${value}`;
  const [path, anchor] = value.split("#");
  const file = (find.meta.path ?? path).split("/").pop() ?? path;
  const { start_line: a, end_line: b } = find.meta;
  if (a && b) return a === b ? `${file}:${a}` : `${file}:${a}–${b}`;
  if (anchor && kind === "code") return `${file}#${anchor}`;
  return file;
}

export function monthYear(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleDateString("en-US", { year: "numeric", month: "short" });
}

export function GradeMarks({ grade }: { grade: number | null }) {
  if (grade === null) return null;
  return (
    <span className="grade" aria-label={`relevance ${grade} of 3`} title={`Relevance ${grade} of 3`}>
      {[1, 2, 3].map((i) => (
        <i key={i} className={i <= grade ? "on" : undefined} />
      ))}
    </span>
  );
}

interface FindTagProps {
  find: Find;
  number: number | undefined;
  state: FindState;
  grade: number | null;
  highlighted?: boolean;
  selected?: boolean;
  mono?: boolean; // render the title as code (hero example)
  onHover?: (id: string | null) => void;
  onSelect?: (id: string) => void;
  style?: CSSProperties;
}

export function FindTag({
  find,
  number,
  state,
  grade,
  highlighted,
  selected,
  mono,
  onHover,
  onSelect,
  style,
}: FindTagProps) {
  const Icon = KIND_ICON[find.kind];
  const label = accessionLabel(number);
  const cls = [
    "find",
    `find--${state}`,
    highlighted && "is-highlighted",
    selected && "is-selected",
    find.partial && "is-partial",
  ]
    .filter(Boolean)
    .join(" ");
  const content = (
    <>
      <span className="find__head">
        <span className="find__no">{label}</span>
        <Icon className="find__icon" size={13} strokeWidth={2} aria-hidden />
        <span className="find__date">{monthYear(find.date)}</span>
      </span>
      <span className={mono ? "find__title find__title--code" : "find__title"}>{find.title}</span>
      <span className="find__foot">
        <GradeMarks grade={grade} />
        <span className="find__id">{shortId(find)}</span>
      </span>
    </>
  );
  const aria = `${label}, ${KIND_LABEL[find.kind]}: ${find.title}. ${STATE_LABEL[state]}${
    grade !== null ? `, relevance ${grade} of 3` : ""
  }`;
  if (!onSelect) {
    return (
      <span className={cls} data-find-id={find.id} style={style} role="img" aria-label={aria}>
        {content}
      </span>
    );
  }
  return (
    <button
      type="button"
      className={cls}
      data-find-id={find.id}
      style={style}
      aria-label={aria}
      aria-haspopup="dialog"
      onMouseEnter={() => onHover?.(find.id)}
      onMouseLeave={() => onHover?.(null)}
      onFocus={() => onHover?.(find.id)}
      onBlur={() => onHover?.(null)}
      onClick={() => onSelect(find.id)}
    >
      {content}
    </button>
  );
}

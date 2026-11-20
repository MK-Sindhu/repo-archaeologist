const NUMBER = new Intl.NumberFormat("en-US");

export function num(n: number): string {
  return NUMBER.format(n);
}

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${num(n)} ${n === 1 ? one : many}`;
}

/** 47 -> "47s", 93.1 -> "1m 33s", 3725 -> "1h 2m" */
export function duration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

/** Running clock: 47 -> "0:47", 125 -> "2:05" */
export function clock(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

export function relativeTime(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "never";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  const diff = Math.max(0, (now - t) / 1000);
  if (diff < 45) return "just now";
  if (diff < 3600) return `${Math.round(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)} h ago`;
  if (diff < 86400 * 7) {
    const d = Math.round(diff / 86400);
    return `${d} day${d === 1 ? "" : "s"} ago`;
  }
  return shortDate(iso);
}

export function shortDate(iso: string | null | undefined): string {
  if (!iso) return "";
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return "";
  return t.toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" });
}

/** Seconds elapsed since an ISO timestamp. */
export function since(iso: string | null | undefined, now = Date.now()): number {
  if (!iso) return 0;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? 0 : Math.max(0, (now - t) / 1000);
}

/** Short label for a canonical ref: "commit:c6d98fc0…" -> "c6d98fc", "pr:12" -> "#12". */
export function refLabel(ref: string): string {
  const [kind, ...rest] = ref.split(":");
  const value = rest.join(":");
  if (kind === "commit") return value.slice(0, 7);
  if (kind === "pr" || kind === "issue" || kind === "gh") return `#${value}`;
  if (kind === "code" || kind === "doc") {
    const [path, anchor] = value.split("#");
    const file = path.split("/").pop() ?? path;
    return anchor ? `${file}#${anchor}` : file;
  }
  return ref;
}

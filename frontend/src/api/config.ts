/** Where the API lives. The website may be hosted apart from the API (VITE_API_URL at build
 *  time); by default it is the same origin. */

/** "https://api.example.com/" -> "https://api.example.com"; blank -> "" (same origin). */
export function normalizeBase(raw: string | undefined | null): string {
  return (raw ?? "").trim().replace(/\/+$/, "");
}

export function apiBase(): string {
  return normalizeBase(import.meta.env.VITE_API_URL);
}

/** Host of a separately hosted API ("api.example.com"), or null for same origin. */
export function apiHost(base = apiBase()): string | null {
  if (!base) return null;
  try {
    return new URL(base).host;
  } catch {
    return base.replace(/^[a-z]+:\/\//i, "");
  }
}

/** Full URL for an API path ("/runs/abc/events?x=1"), with the access code as the `access`
 *  query parameter when given (EventSource cannot send headers). */
export function buildUrl(base: string, path: string, access?: string | null): string {
  const url = `${normalizeBase(base)}/api${path.startsWith("/") ? path : `/${path}`}`;
  if (!access) return url;
  const [beforeHash, hash] = splitHash(url);
  const sep = beforeHash.includes("?") ? (beforeHash.endsWith("?") || beforeHash.endsWith("&") ? "" : "&") : "?";
  return `${beforeHash}${sep}access=${encodeURIComponent(access)}${hash}`;
}

function splitHash(url: string): [string, string] {
  const i = url.indexOf("#");
  return i < 0 ? [url, ""] : [url.slice(0, i), url.slice(i)];
}

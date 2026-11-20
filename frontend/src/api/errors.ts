/** An API failure. `status` is the HTTP status, or 0 when the server could not be reached.
 *  `message` is the server's `detail` sentence (meant for the user) when it sent one. */
export class ApiError extends Error {
  readonly status: number;
  /** The server's own `detail` sentence, when it sent one. */
  readonly detail: string | null;
  /** Something answered, but not the Repo Archaeologist API (e.g. the website's own host because
   *  VITE_API_URL is missing). */
  readonly notApi: boolean;
  constructor(status: number, message: string, detail: string | null = null, notApi = false) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.notApi = notApi;
  }
}

/** The address the website calls is not the Repo Archaeologist API: a setup problem, not a
 *  temporary one, so retrying does not help. */
export function isNotApi(err: unknown): boolean {
  return err instanceof ApiError && err.notApi;
}

/** The server is down, asleep (free hosting plans pause idle servers) or not reachable. */
export function isUnreachable(err: unknown): boolean {
  return err instanceof ApiError && (err.status === 0 || err.status === 502 || err.status === 503 || err.status === 504);
}

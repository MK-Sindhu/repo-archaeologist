import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/** The client against stubbed fetch/EventSource/window, freshly imported per test (it caches
 *  the mock flag and the stored access code at module level). */

interface Call {
  url: string;
  init: RequestInit;
}

let calls: Call[];
let reply: (url: string) => Response | Promise<Response>;
let sources: string[];

function memoryStorage() {
  const data = new Map<string, string>();
  return {
    getItem: (k: string) => data.get(k) ?? null,
    setItem: (k: string, v: string) => void data.set(k, v),
    removeItem: (k: string) => void data.delete(k),
  };
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

beforeEach(() => {
  calls = [];
  sources = [];
  reply = () => json(200, []);
  vi.resetModules();
  vi.stubGlobal("window", { location: { search: "" }, localStorage: memoryStorage() });
  vi.stubGlobal("fetch", (url: string, init: RequestInit) => {
    calls.push({ url, init });
    return Promise.resolve(reply(url));
  });
  vi.stubGlobal(
    "EventSource",
    class {
      static CLOSED = 2;
      readyState = 0;
      onmessage: unknown = null;
      onerror: unknown = null;
      constructor(url: string) {
        sources.push(url);
      }
      close() {}
    },
  );
  vi.stubEnv("VITE_API_URL", "https://api.example.com/");
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

const load = async () => {
  const client = await import("./client");
  const { access } = await import("./access");
  return { ...client, access };
};

const header = (c: Call, name: string) => (c.init.headers as Record<string, string>)[name];

describe("client", () => {
  it("sends every request to VITE_API_URL with the stored access code", async () => {
    const { api, access } = await load();
    access.set("c0de");
    await api.repos();
    await api.runs("toy-1", 5);
    expect(calls.map((c) => c.url)).toEqual([
      "https://api.example.com/api/repos",
      "https://api.example.com/api/runs?limit=5&repo_id=toy-1",
    ]);
    expect(header(calls[0], "X-Access-Code")).toBe("c0de");
  });

  it("sends no access header when no code is stored", async () => {
    const { api } = await load();
    await api.status();
    expect(header(calls[0], "X-Access-Code")).toBeUndefined();
  });

  it("puts the access code on event-stream URLs", async () => {
    const { streams, access } = await load();
    access.set("a b");
    const stop = streams.run("r1", { onEvent: () => undefined });
    stop();
    expect(sources).toEqual(["https://api.example.com/api/runs/r1/events?access=a%20b"]);
  });

  it("reports 401s to the app, except for access checks", async () => {
    const { api, checkAccess, onUnauthorized, ApiError } = await load();
    reply = () => json(401, { detail: "This server needs an access code." });
    const seen: string[] = [];
    onUnauthorized((d) => seen.push(d));
    await expect(api.repos()).rejects.toMatchObject({ status: 401, message: "This server needs an access code." });
    await expect(checkAccess("nope")).rejects.toBeInstanceOf(ApiError);
    await expect(checkAccess("nope")).rejects.toMatchObject({ detail: "This server needs an access code." });
    expect(seen).toEqual(["This server needs an access code."]);
    expect(header(calls[1], "X-Access-Code")).toBe("nope"); // checked without storing it
  });

  it("surfaces a 429 detail from POST /api/runs", async () => {
    const { api } = await load();
    reply = () => json(429, { detail: "Too many questions; try again in 12 minutes." });
    await expect(api.createRun({ repo_id: "r", question: "why?" })).rejects.toMatchObject({
      status: 429,
      message: "Too many questions; try again in 12 minutes.",
    });
  });

  it("treats network errors and 502/503/504 as unreachable", async () => {
    const { api, isUnreachable } = await load();
    reply = () => Promise.reject(new TypeError("Failed to fetch"));
    const down = await api.status().catch((e) => e);
    expect(down.status).toBe(0);
    expect(isUnreachable(down)).toBe(true);
    reply = () => json(503, {});
    expect(isUnreachable(await api.status().catch((e) => e))).toBe(true);
    reply = () => json(500, { detail: "boom" });
    expect(isUnreachable(await api.status().catch((e) => e))).toBe(false);
    reply = () => new Response("nope", { status: 418, statusText: "I'm a teapot" });
    expect(await api.status().catch((e) => e)).toMatchObject({ status: 418, detail: null });
    // a host waking up may answer with an HTML 503 page: still "unreachable", not a setup error
    reply = () => new Response("<html>waking</html>", { status: 503, headers: { "Content-Type": "text/html" } });
    expect(isUnreachable(await api.status().catch((e) => e))).toBe(true);
  });

  it("explains a website that is not connected to its API (e.g. Vercel without VITE_API_URL)", async () => {
    vi.stubEnv("VITE_API_URL", "");
    const { api, isNotApi, isUnreachable } = await load();
    // Vercel serves the website's index.html for GET /api/status ...
    reply = () => new Response("<!doctype html><title>Archaeologist</title>", {
      status: 200, headers: { "Content-Type": "text/html; charset=utf-8" },
    });
    const status = await api.status().catch((e) => e);
    expect(isNotApi(status)).toBe(true);
    expect(isUnreachable(status)).toBe(false);
    expect(status.message).toContain("VITE_API_URL");
    expect(status.message).toContain("redeploy");
    // ... and refuses POST /api/repos with a bare 405
    reply = () => new Response("", { status: 405 });
    const add = await api.addRepo("owner/name").catch((e) => e);
    expect(add).toMatchObject({ status: 405, notApi: true });
    expect(add.message).toContain("VITE_API_URL");
  });

  it("names the configured host when VITE_API_URL points somewhere else", async () => {
    const { api, isNotApi } = await load();
    reply = () => new Response("<html>someone else</html>", { status: 200, headers: { "Content-Type": "text/html" } });
    const err = await api.status().catch((e) => e);
    expect(isNotApi(err)).toBe(true);
    expect(err.message).toContain("api.example.com");
  });

  it("words generic errors without stray spaces", async () => {
    const { api } = await load();
    reply = () => json(500, {});
    expect((await api.status().catch((e) => e)).message).toBe("The server answered with an error 500.");
  });

  it("mock mode needs neither the API nor an access code", async () => {
    vi.stubGlobal("window", { location: { search: "?mock=1" }, localStorage: memoryStorage() });
    vi.stubEnv("VITE_API_URL", "");
    const { api } = await load();
    const status = await api.status();
    expect(status.access_required).toBe(false);
    expect((await api.repos()).length).toBeGreaterThan(0);
    expect(calls).toHaveLength(0);
  });
});

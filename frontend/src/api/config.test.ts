import { describe, expect, it } from "vitest";
import { apiHost, buildUrl, normalizeBase } from "./config";

describe("normalizeBase", () => {
  it("strips trailing slashes and blanks", () => {
    expect(normalizeBase("https://api.example.com/")).toBe("https://api.example.com");
    expect(normalizeBase("https://api.example.com///")).toBe("https://api.example.com");
    expect(normalizeBase("  https://api.example.com  ")).toBe("https://api.example.com");
    expect(normalizeBase("")).toBe("");
    expect(normalizeBase(undefined)).toBe("");
  });
});

describe("buildUrl", () => {
  it("uses the same origin by default", () => {
    expect(buildUrl("", "/status")).toBe("/api/status");
    expect(buildUrl("", "runs")).toBe("/api/runs");
  });

  it("prefixes a separately hosted API", () => {
    expect(buildUrl("https://api.example.com/", "/repos")).toBe("https://api.example.com/api/repos");
  });

  it("adds the access code as a query parameter, URL-encoded", () => {
    expect(buildUrl("", "/runs/abc/events", "s3cr&t ü")).toBe("/api/runs/abc/events?access=s3cr%26t%20%C3%BC");
  });

  it("appends to an existing query", () => {
    expect(buildUrl("https://api.example.com", "/runs?repo_id=x&limit=5", "code")).toBe(
      "https://api.example.com/api/runs?repo_id=x&limit=5&access=code",
    );
    expect(buildUrl("", "/runs?", "code")).toBe("/api/runs?access=code");
  });

  it("keeps a fragment after the query", () => {
    expect(buildUrl("", "/x#frag", "c")).toBe("/api/x?access=c#frag");
  });

  it("leaves the URL alone without a code", () => {
    expect(buildUrl("", "/runs?limit=5", null)).toBe("/api/runs?limit=5");
    expect(buildUrl("", "/runs", "")).toBe("/api/runs");
  });
});

describe("apiHost", () => {
  it("names the API host only when it is separate", () => {
    expect(apiHost("")).toBeNull();
    expect(apiHost("https://archaeologist-api.onrender.com")).toBe("archaeologist-api.onrender.com");
  });
});

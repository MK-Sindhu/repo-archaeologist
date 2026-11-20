// Render public/og.png (1200x630) from the real landing hero, and the favicon PNGs.
// usage: node scripts/render-og.mjs <site url, e.g. http://localhost:4173> [public dir]
// Serve a pre-rendered build first (npm run build && npx vite preview), then run this; it
// rewrites public/og.png, public/favicon-32.png and public/apple-touch-icon.png.
import { spawn } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const PUBLIC = process.argv[3] ?? new URL("../public/", import.meta.url).pathname;
const SITE = (process.argv[2] ?? "http://localhost:4173").replace(/\/+$/, "");
const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const PORT = 9343;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const chrome = spawn(CHROME, ["--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${mkdtempSync(join(tmpdir(), "og-"))}`, "about:blank"], { stdio: "ignore" });
let ws, id = 1; const pending = new Map();
for (let i = 0; i < 60 && !ws; i++) {
  try {
    const page = (await (await fetch(`http://127.0.0.1:${PORT}/json`)).json()).find((t) => t.type === "page");
    if (page) { ws = new WebSocket(page.webSocketDebuggerUrl); await new Promise((r) => (ws.onopen = r)); }
  } catch { await sleep(200); }
}
ws.onmessage = (m) => { const msg = JSON.parse(m.data); if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); } };
const send = (method, params = {}) => new Promise((r) => { const n = id++; pending.set(n, r); ws.send(JSON.stringify({ id: n, method, params })); });
const shot = async (file, clip) => {
  const res = await send("Page.captureScreenshot", { format: "png", ...(clip ? { clip: { ...clip, scale: 1 } } : {}) });
  writeFileSync(file, Buffer.from(res.result.data, "base64"));
};
try {
  await send("Page.enable");
  await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "reduce" }, { name: "prefers-color-scheme", value: "light" }] });
  // -- OG image: the hero's finished frame, no navigation
  await send("Emulation.setDeviceMetricsOverride", { width: 1200, height: 630, deviceScaleFactor: 1, mobile: false });
  await send("Page.navigate", { url: SITE + "/" }); await sleep(2500);
  await send("Runtime.evaluate", { expression: `
    const s = document.createElement("style");
    s.textContent = \`
      .site-header, .skip, .hero-replay__caption, .hero-replay__ticker, .lp-hero__ctas, .lp-hero__sub { display: none !important; }
      .lp-hero { grid-template-columns: minmax(0, 5fr) minmax(0, 6fr) !important; padding-top: 52px !important; gap: 48px !important; max-width: 1200px; }
      .lp-hero__title { font-size: 4.25rem !important; max-width: 9.5ch !important; }
      .hero-replay__frame { min-height: 0 !important; }
      .og-mark { display: flex; align-items: center; gap: 12px; margin-top: 36px; font-family: var(--font-display); font-stretch: 125%; font-weight: 800; font-size: 1.15rem; }
      .og-line { margin-top: 14px; color: var(--ink-soft); font-family: var(--font-prose); font-size: 1.2rem; line-height: 1.45; max-width: 25rem; }
    \`;
    document.head.appendChild(s);
    const copy = document.querySelector(".lp-hero__copy");
    const line = document.createElement("p"); line.className = "og-line";
    line.textContent = "Follows code to its commit, pull request and issue, and cites every claim.";
    const mark = document.createElement("div"); mark.className = "og-mark";
    mark.innerHTML = document.querySelector(".wordmark").innerHTML;
    copy.appendChild(line); copy.appendChild(mark);
    window.scrollTo(0, 0); true` });
  await sleep(800);
  await shot(join(PUBLIC, "og.png"), { x: 0, y: 0, width: 1200, height: 630 });
  // -- favicons from favicon.svg
  const svg = readFileSync(join(PUBLIC, "favicon.svg"), "utf8");
  const html = (size, pad, bg) => `data:text/html,${encodeURIComponent(`<body style="margin:0;background:${bg}"><div style="width:${size}px;height:${size}px;display:grid;place-items:center">${svg.replace("<svg ", `<svg width="${size - 2 * pad}" height="${size - 2 * pad}" `)}</div></body>`)}`;
  await send("Emulation.setDefaultBackgroundColorOverride", { color: { r: 0, g: 0, b: 0, a: 0 } });
  await send("Emulation.setDeviceMetricsOverride", { width: 32, height: 32, deviceScaleFactor: 1, mobile: false });
  await send("Page.navigate", { url: html(32, 0, "transparent") }); await sleep(400);
  await shot(join(PUBLIC, "favicon-32.png"), { x: 0, y: 0, width: 32, height: 32 });
  await send("Emulation.setDeviceMetricsOverride", { width: 180, height: 180, deviceScaleFactor: 1, mobile: false });
  await send("Page.navigate", { url: html(180, 28, "#EDF0EE") }); await sleep(400);
  await shot(join(PUBLIC, "apple-touch-icon.png"), { x: 0, y: 0, width: 180, height: 180 });
  console.log("ok");
} finally { ws?.close(); chrome.kill(); }

// Pre-render the public pages after `vite build`.
//
// 1. Build src/entry-server.tsx for Node (`vite build --ssr`) into a private temp folder under
//    node_modules/.cache (so bare imports resolve, and parallel builds never share it).
// 2. Read the recordings from <outDir>/showcase/ and render every public route with
//    react-dom/server, into <outDir>/<route>/index.html (and <outDir>/404.html), based on the
//    built index.html: the markup goes into #root, the page's <title>, description, canonical
//    URL and Open Graph / Twitter tags into <head>, and the data the page used into
//    <script id="site-data" type="application/json"> so the browser's first render matches.
//    The browser then mounts with createRoot (no hydration). app.html is left alone: the app
//    under /app is never pre-rendered.
// 3. Write robots.txt, and sitemap.xml when the site's URL is known (VITE_SITE_URL, else
//    Vercel's VERCEL_PROJECT_PRODUCTION_URL).
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { build } from "vite";
import { frontendRoot, outDir } from "./outdir.mjs";

const out = outDir();
const siteUrl = (
  process.env.VITE_SITE_URL ||
  (process.env.VERCEL_PROJECT_PRODUCTION_URL ? `https://${process.env.VERCEL_PROJECT_PRODUCTION_URL}` : "")
).replace(/\/+$/, "");

function escapeAttr(s) {
  return String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function escapeText(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function jsonForScript(value) {
  return JSON.stringify(value)
    .replace(/</g, "\\u003c")
    .replaceAll(String.fromCharCode(0x2028), "\\u2028")
    .replaceAll(String.fromCharCode(0x2029), "\\u2029");
}

function readRecordings(dir) {
  const files = fs.existsSync(dir)
    ? fs.readdirSync(dir).filter((f) => f.endsWith(".json") && f !== "index.json")
    : [];
  const recordings = {};
  for (const f of files) {
    try {
      const rec = JSON.parse(fs.readFileSync(path.join(dir, f), "utf8"));
      if (rec && rec.slug && rec.run) recordings[rec.slug] = rec;
    } catch (e) {
      console.warn(`prerender: skipping ${f}: ${e.message}`);
    }
  }
  let index = null;
  try {
    index = JSON.parse(fs.readFileSync(path.join(dir, "index.json"), "utf8"));
  } catch {
    /* rebuilt below */
  }
  // only list recordings that exist; add any the index doesn't know about
  const listed = (index?.recordings ?? []).filter((r) => recordings[r.slug]);
  for (const rec of Object.values(recordings)) {
    if (!listed.some((r) => r.slug === rec.slug)) {
      listed.push({ slug: rec.slug, repo: rec.repo, question: rec.run.question, recorded_at: rec.recorded_at });
    }
  }
  return { index: { recordings: listed }, recordings };
}

function headTags(meta) {
  const url = siteUrl ? `${siteUrl}${meta.path === "/" ? "/" : meta.path}` : null;
  const image = `${siteUrl}/og.png`;
  const tags = [
    `<meta property="og:type" content="website" />`,
    `<meta property="og:site_name" content="Repo Archaeologist" />`,
    `<meta property="og:title" content="${escapeAttr(meta.title)}" />`,
    `<meta property="og:description" content="${escapeAttr(meta.description)}" />`,
    `<meta property="og:image" content="${escapeAttr(image)}" />`,
    `<meta property="og:image:width" content="1200" />`,
    `<meta property="og:image:height" content="630" />`,
    `<meta name="twitter:card" content="summary_large_image" />`,
    `<meta name="twitter:title" content="${escapeAttr(meta.title)}" />`,
    `<meta name="twitter:description" content="${escapeAttr(meta.description)}" />`,
    `<meta name="twitter:image" content="${escapeAttr(image)}" />`,
  ];
  if (url) {
    tags.push(`<meta property="og:url" content="${escapeAttr(url)}" />`);
    tags.push(`<link rel="canonical" href="${escapeAttr(url)}" />`);
  }
  return tags.join("\n    ");
}

function page(template, rendered) {
  const { html, meta, data } = rendered;
  let doc = template;
  doc = doc.replace(/<title>[\s\S]*?<\/title>/, `<title>${escapeText(meta.title)}</title>`);
  doc = doc.replace(
    /<meta name="description" content="[^"]*"\s*\/?>/,
    `<meta name="description" content="${escapeAttr(meta.description)}" />`,
  );
  const extra = `${headTags(meta)}\n    <script id="site-data" type="application/json">${jsonForScript(data)}</script>`;
  doc = doc.includes("<!--site-head-->") ? doc.replace("<!--site-head-->", extra) : doc.replace("</head>", `    ${extra}\n  </head>`);
  if (!doc.includes('<div id="root"></div>')) throw new Error("built index.html has no empty #root");
  return doc.replace('<div id="root"></div>', `<div id="root">${html}</div>`);
}

function target(route) {
  if (route === "/") return path.join(out, "index.html");
  if (route === "/404") return path.join(out, "404.html");
  return path.join(out, ...route.split("/").filter(Boolean).map(decodeURIComponent), "index.html");
}

async function main() {
  const templatePath = path.join(out, "index.html");
  if (!fs.existsSync(templatePath)) throw new Error(`no ${templatePath}: run vite build first`);
  const template = fs.readFileSync(templatePath, "utf8");

  const cacheDir = path.join(frontendRoot, "node_modules", ".cache");
  fs.mkdirSync(cacheDir, { recursive: true });
  const ssrDir = fs.mkdtempSync(path.join(cacheDir, "ra-ssr-"));
  try {
    await build({
      configFile: path.join(frontendRoot, "vite.config.ts"),
      logLevel: "warn",
      build: {
        ssr: true,
        outDir: ssrDir,
        emptyOutDir: true,
        copyPublicDir: false,
        rollupOptions: { input: path.join(frontendRoot, "src", "entry-server.tsx") },
      },
    });
    const entry = fs.readdirSync(ssrDir).find((f) => /^entry-server\.m?js$/.test(f));
    if (!entry) throw new Error(`SSR build produced no entry-server.js in ${ssrDir}`);
    const server = await import(pathToFileURL(path.join(ssrDir, entry)).href);

    const { index, recordings } = readRecordings(path.join(out, "showcase"));
    fs.writeFileSync(path.join(out, "showcase", "index.json"), JSON.stringify(index, null, 1));
    server.prime(index, recordings);

    const routes = [...server.publicRoutes(index), "/404"];
    for (const route of routes) {
      const file = target(route);
      fs.mkdirSync(path.dirname(file), { recursive: true });
      fs.writeFileSync(file, page(template, server.render(route, index, recordings)));
    }

    const robots = ["User-agent: *", "Allow: /", "Disallow: /app", "Disallow: /api/"];
    if (siteUrl) {
      robots.push(`Sitemap: ${siteUrl}/sitemap.xml`);
      const urls = routes
        .filter((r) => r !== "/404")
        .map((r) => `  <url><loc>${escapeText(siteUrl + (r === "/" ? "/" : r))}</loc></url>`)
        .join("\n");
      fs.writeFileSync(
        path.join(out, "sitemap.xml"),
        `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n${urls}\n</urlset>\n`,
      );
    }
    fs.writeFileSync(path.join(out, "robots.txt"), robots.join("\n") + "\n");
    console.log(
      `prerendered ${routes.length} pages (${Object.keys(recordings).length} recordings) into ${path.relative(process.cwd(), out) || "."}`,
    );
  } finally {
    fs.rmSync(ssrDir, { recursive: true, force: true });
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});

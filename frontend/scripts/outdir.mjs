// Where the website build goes; shared by vite.config.ts and scripts/prerender.mjs.
// - FRONTEND_OUT_DIR, when set
// - "dist" on Vercel (VERCEL is set by the platform; it is vercel.json's outputDirectory)
// - otherwise into the Python package, so FastAPI serves the site (src/archaeologist/web/static)
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";

export const frontendRoot = resolve(fileURLToPath(new URL(".", import.meta.url)), "..");

export function outDir() {
  const dir =
    process.env.FRONTEND_OUT_DIR || (process.env.VERCEL ? "dist" : "../src/archaeologist/web/static");
  return resolve(frontendRoot, dir);
}

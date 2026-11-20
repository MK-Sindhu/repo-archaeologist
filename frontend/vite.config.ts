import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Where the build goes:
// - locally: into the Python package, so FastAPI serves the site (src/archaeologist/web/static)
// - on Vercel (VERCEL is set by the platform): "dist", the outputDirectory in vercel.json
// - FRONTEND_OUT_DIR overrides both
const outDir =
  process.env.FRONTEND_OUT_DIR || (process.env.VERCEL ? "dist" : "../src/archaeologist/web/static");

export default defineConfig({
  plugins: [react()],
  build: {
    outDir,
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    port: 5173,
    proxy: {
      // SSE streams through: the backend sends no Content-Length and flushes each event.
      "/api": { target: "http://127.0.0.1:8765", changeOrigin: false },
    },
  },
  preview: { port: 4173 },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});

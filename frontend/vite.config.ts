import { resolve } from "node:path";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";
import { frontendRoot, outDir } from "./scripts/outdir.mjs";

// Two HTML entries with the same app: index.html (the public pages; scripts/prerender.mjs then
// writes one pre-rendered copy per public route) and app.html (the app under /app, never
// pre-rendered). See scripts/outdir.mjs for where the build goes.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: outDir(),
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: {
      input: {
        index: resolve(frontendRoot, "index.html"),
        app: resolve(frontendRoot, "app.html"),
      },
    },
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

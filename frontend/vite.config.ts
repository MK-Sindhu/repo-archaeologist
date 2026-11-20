import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Built assets go into the Python package so FastAPI can serve them (src/archaeologist/web/static).
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../src/archaeologist/web/static",
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

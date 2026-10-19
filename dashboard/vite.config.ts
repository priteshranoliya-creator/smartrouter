import { defineConfig } from "vite";

// The router serves the build under /ui/ and the API from the same origin, so `vite dev` proxies both.
export default defineConfig({
  base: "/ui/",
  esbuild: { jsx: "automatic", jsxImportSource: "preact" },
  build: { outDir: "dist", emptyOutDir: true, target: "es2022" },
  server: { proxy: { "/api": "http://127.0.0.1:8787", "/healthz": "http://127.0.0.1:8787" } },
});

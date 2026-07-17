import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The SPA is served by the Python supervisor from src/chara/front/webui/.
// - base "./"          → asset URLs are relative, so the same build works under
//   any mount path (and would survive file:// if ever needed).
// - non-hashed names   → rebuilds overwrite the same paths so git/wheel churn
//   stays low (the dist is bundled into the wheel via package-data; see the plan).
// - hash routing in the app means the server needs NO SPA-fallback route list.
//
// Dev: run `chara desktop --no-open`, read its printed http/ws ports, and
// point these envs at it. RPC is POST /rpc; the WS lives on a separate port.
const BACKEND = process.env.CHARA_BACKEND || "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "../../src/chara/front/webui",
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: {
      output: {
        entryFileNames: "assets/[name].js",
        chunkFileNames: "assets/[name].js",
        assetFileNames: "assets/[name][extname]",
        // Split the heavy, rarely-changing vendor libs into their own (stable,
        // non-hashed) chunks so the app chunk stays small + cacheable across
        // rebuilds — closes the >500 kB single-chunk advisory. Vite 8 bundles
        // with rolldown, so this uses its native codeSplitting.groups API (the
        // old rollup `manualChunks` object form is rejected — "not a function").
        codeSplitting: {
          groups: [
            { name: "react", test: /[\\/]node_modules[\\/](react|react-dom|scheduler)[\\/]/ },
            { name: "xterm", test: /[\\/]node_modules[\\/]@xterm[\\/]/ },
            { name: "markdown", test: /[\\/]node_modules[\\/](react-markdown|remark-gfm|micromark|mdast-|hast-|unist-|vfile)/ },
          ],
        },
      },
    },
  },
  server: {
    proxy: {
      "/rpc": { target: BACKEND, changeOrigin: true },
      "/asset": { target: BACKEND, changeOrigin: true },
      "/upload": { target: BACKEND, changeOrigin: true },
    },
  },
});

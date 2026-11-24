import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8055",
        changeOrigin: false,
      },
    },
  },
  preview: {
    host: "127.0.0.1",
    port: 4173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8055" },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    chunkSizeWarningLimit: 900,
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            { name: "charts", test: /node_modules[\\/](lightweight-charts|fancy-canvas)/ },
            { name: "markdown", test: /node_modules[\\/](react-markdown|remark-|mdast-|micromark|unified|hast-|unist-|vfile|property-information|decode-named|character-|space-separated|comma-separated|html-url|trim-lines|devlop|bail|is-plain-obj|trough|zwitch|longest-streak|markdown-table|ccount|escape-string|estree|style-to|inline-style|hastscript|web-namespaces)/ },
            { name: "react", test: /node_modules[\\/](react|react-dom|scheduler)[\\/]/ },
          ],
        },
      },
    },
  },
});

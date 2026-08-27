import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: "/",
  build: {
    outDir: "../../src/runtime/operator/web_assets",
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: {
      output: {
        manualChunks: {
          graph: ["@xyflow/react"],
          editor: [
            "@codemirror/lang-json",
            "@codemirror/lang-python",
            "@codemirror/state",
            "@codemirror/view",
            "@uiw/codemirror-theme-github",
          ],
          protobuf: [
            "@protobuf-ts/grpcweb-transport",
            "@protobuf-ts/runtime",
            "@protobuf-ts/runtime-rpc",
          ],
        },
      },
    },
  },
  server: {
    fs: {
      allow: ["../.."],
    },
    port: 5173,
    proxy: {
      "/avalanche.operator.OperatorServiceV2": {
        target: "http://127.0.0.1:7435",
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
  },
});

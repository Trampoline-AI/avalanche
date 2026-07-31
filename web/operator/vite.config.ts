import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  base: "/",
  build: {
    outDir: "../../src/runtime/operator/web_assets",
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: {
      output: {
        manualChunks: {
          graph: ["@xyflow/react"],
          editor: ["@codemirror/lang-json", "@codemirror/state", "@codemirror/view"],
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
    port: 5173,
    proxy: {
      "/avalanche.operator.OperatorService": {
        target: "http://127.0.0.1:7435",
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
  },
});

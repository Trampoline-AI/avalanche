import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

function consoleChunk(id: string): string | undefined {
  if (id.includes("node_modules/@xyflow/react/")) return "graph";
  if (id.includes("node_modules/@codemirror/")) return "editor";
  if (id.includes("node_modules/@protobuf-ts/")) return "protobuf";
  return undefined;
}

export default defineConfig({
  plugins: [react()],
  base: "/",
  build: {
    outDir: "../../src/runtime/operator/web_assets",
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: {
      output: {
        manualChunks: consoleChunk,
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
});

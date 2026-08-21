import { fileURLToPath } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import prefixer from "postcss-prefix-selector";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const external = [
  "@codemirror/lang-json",
  "@codemirror/state",
  "@codemirror/view",
  "@protobuf-ts/grpcweb-transport",
  "@protobuf-ts/runtime",
  "@protobuf-ts/runtime-rpc",
  "@tanstack/react-virtual",
  "@xyflow/react",
  "lucide-react",
  "react",
  "react-dom",
  "react/jsx-runtime",
  "react-markdown",
];

export default defineConfig({
  plugins: [react(), tailwindcss()],

  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
  },
  css: {
    postcss: {
      plugins: [
        prefixer({
          prefix: ".avalanche-console",
          transform(prefix, selector, prefixedSelector) {
            if (selector === ":root" || selector === ":host") return prefix;
            return selector === prefix || selector.startsWith(`${prefix} `)
              ? selector
              : prefixedSelector;
          },
        }),
      ],
    },
  },
  build: {
    emptyOutDir: true,
    lib: {
      entry: fileURLToPath(new URL("./src/index.ts", import.meta.url)),
      fileName: "index",
      formats: ["es"],
    },
    outDir: "dist",
    rollupOptions: {
      external,
      output: {
        assetFileNames: (assetInfo) =>
          assetInfo.name?.endsWith(".css") ? "styles.css" : "assets/[name]-[hash][extname]",
      },
    },
  },
});

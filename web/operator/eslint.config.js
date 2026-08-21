import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: [".vite/**", "dist/**", "src/generated/**"] },
  {
    files: ["*.{js,mjs}", "scripts/**/*.mjs"],
    extends: [js.configs.recommended],
    languageOptions: {
      globals: globals.node,
    },
  },
  {
    files: ["**/*.{ts,tsx}"],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: {
      ecmaVersion: "latest",
      globals: globals.browser,
    },
    plugins: {
      "react-hooks": reactHooks,
    },
    rules: {
      "react-hooks/rules-of-hooks": "error",
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
    },
  },
  {
    files: ["src/**/*.test.{ts,tsx}"],
    rules: {
      "require-yield": "off",
    },
  },
  {
    files: ["src/AnsiText.tsx"],
    rules: {
      "no-control-regex": "off",
    },
  },
);

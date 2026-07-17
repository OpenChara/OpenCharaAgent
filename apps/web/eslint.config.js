import js from "@eslint/js";
import globals from "globals";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";

// Flat config for the OpenCharaAgent web SPA (Vite + React 19 + TS).
// Pragmatic, not strict: typescript-eslint recommended + the React Hooks
// rules + the react-refresh fast-refresh guard, with browser globals.
export default tseslint.config(
  {
    ignores: ["node_modules", "dist", "../../src/chara/front/webui"],
  },
  js.configs.recommended,
  tseslint.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      // The classic React Hooks lint (the two rules the official Vite
      // React-TS template ships). plugin v7's `recommended` preset now also
      // bundles the opt-in React Compiler rules (refs/immutability/purity/
      // set-state-in-effect/preserve-manual-memoization) as errors — those
      // assume you're adopting the React Compiler and flag idiomatic patterns
      // this codebase intentionally uses (ref reads, external-store effects).
      // We don't enable that regime; we keep the pragmatic two.
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "warn",
      "react-refresh/only-export-components": [
        "warn",
        { allowConstantExport: true },
      ],
    },
  },
);

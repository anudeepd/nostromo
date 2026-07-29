import { createRequire } from "node:module";

const require = createRequire(new URL("./scripts/package.json", import.meta.url));
const js = require("@eslint/js");
const globals = require("globals");
const reactHooks = require("eslint-plugin-react-hooks");
const tseslint = require("typescript-eslint");
const { defineConfig, globalIgnores } = require("eslint/config");

const browserSource = ["xwing/frontend/src/**/*.{js,ts,tsx}"];
const testSource = ["scripts/tests/**/*.{js,ts}", "scripts/e2e/**/*.ts"];
const nodeSource = ["eslint.config.mjs", "scripts/*.mjs", "scripts/*.ts"];
const typescriptSource = [
  "xwing/frontend/src/**/*.{ts,tsx}",
  "scripts/tests/**/*.ts",
  "scripts/e2e/**/*.ts",
  "scripts/*.ts",
];

export default defineConfig([
  globalIgnores([
    "scripts/node_modules",
    "scripts/playwright-report",
    "scripts/test-results",
    "xwing/static",
  ]),
  {
    files: [...browserSource, ...testSource, ...nodeSource],
    extends: [js.configs.recommended],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
      globals: {
        ...globals.browser,
        ...globals.node,
      },
    },
  },
  {
    files: typescriptSource,
    extends: [tseslint.configs.recommended],
    rules: {
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
    },
  },
  {
    files: ["xwing/frontend/src/**/*.tsx"],
    plugins: {
      "react-hooks": reactHooks,
    },
    rules: {
      "react-hooks/rules-of-hooks": "error",
    },
  },
]);

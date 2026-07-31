import { build } from "esbuild";
import { fileURLToPath } from "url";
import { dirname, resolve } from "path";
import { mkdirSync, rmSync } from "fs";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const projectRoot = resolve(scriptDir, "..");
const srcDir = resolve(projectRoot, "xwing/frontend/src");
const outDir = resolve(projectRoot, "xwing/static/assets");

rmSync(outDir, { recursive: true, force: true });
mkdirSync(outDir, { recursive: true });

await build({
  entryPoints: [
    resolve(srcDir, "app.tsx"),
    resolve(srcDir, "editor.tsx"),
    resolve(srcDir, "admin.ts"),
  ],
  bundle: true,
  minify: true,
  format: "iife",
  target: ["es2020"],
  jsx: "automatic",
  nodePaths: [resolve(projectRoot, "scripts/node_modules")],
  outdir: outDir,
  entryNames: "[name]",
  assetNames: "fonts/[name]-[hash]",
  loader: { ".woff": "file", ".woff2": "file" },
  logLevel: "info",
});

await build({
  entryPoints: [resolve(srcDir, "style.css"), resolve(srcDir, "admin.css")],
  bundle: true,
  minify: true,
  external: ["/static/fonts/*"],
  outdir: outDir,
  entryNames: "[name]",
  assetNames: "fonts/[name]-[hash]",
  loader: { ".woff": "file", ".woff2": "file" },
  logLevel: "info",
});

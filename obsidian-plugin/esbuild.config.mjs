import esbuild from "esbuild";
import { builtinModules } from "module";

const prod = process.argv.includes("production");

const context = await esbuild.context({
  banner: {
    js: "/* DedupSuite 2.0 Obsidian bridge — built for production */",
  },
  entryPoints: ["src/main.ts"],
  bundle: true,
  format: "cjs",
  target: "es2020",
  logLevel: "info",
  sourcemap: prod ? false : "inline",
  treeShaking: true,
  outfile: "main.js",
  external: [
    "obsidian",
    "electron",
    "@codemirror/autocomplete",
    "@codemirror/collab",
    "@codemirror/commands",
    "@codemirror/language",
    "@codemirror/lint",
    "@codemirror/search",
    "@codemirror/state",
    "@codemirror/view",
    "@lezer/common",
    "@lezer/highlight",
    "@lezer/lr",
    ...builtinModules,
  ],
});

if (prod) {
  await context.rebuild();
  process.exit(0);
}
await context.watch();

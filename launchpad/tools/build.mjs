// Bundles src/ into site/assets/app.js. Run: npm run build
import { build } from "esbuild";

const watch = process.argv.includes("--watch");
const options = {
  entryPoints: ["src/main.js"],
  bundle: true,
  format: "esm",
  platform: "browser",
  target: ["chrome109", "safari16", "firefox115"],
  minify: !watch,
  sourcemap: watch ? "inline" : false,
  legalComments: "none",
  outfile: "site/assets/app.js",
  define: { global: "globalThis", "process.env.NODE_ENV": '"production"' },
  logLevel: "info",
};

if (watch) {
  const { context } = await import("esbuild");
  const ctx = await context(options);
  await ctx.watch();
} else {
  await build(options);
}

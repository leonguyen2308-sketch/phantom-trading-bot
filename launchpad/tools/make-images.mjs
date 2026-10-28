// Renders the social preview (site/assets/og.jpg) and the phone icon (apple-touch-icon.png).
// Run: node tools/make-images.mjs   (E2E_FONTS=<folder> uses cached Google Fonts offline)
import { readFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

const { chromium } = createRequire(import.meta.url)("playwright");
const ASSETS = fileURLToPath(new URL("../site/assets/", import.meta.url));
const FONTS = process.env.E2E_FONTS;
const b64 = async (f) => (await readFile(join(ASSETS, f))).toString("base64");

const og = `<!doctype html><html><head>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500..800&family=JetBrains+Mono:wght@500;700&display=swap">
<style>
  body { margin: 0; width: 1200px; height: 630px; overflow: hidden; background: #0a0d12; color: #f5f2ec; font-family: "Bricolage Grotesque", sans-serif; position: relative; }
  .glow { position: absolute; right: -80px; top: -120px; width: 760px; height: 760px; border-radius: 50%; background: radial-gradient(closest-side, rgba(255,68,54,.45), rgba(255,68,54,.08) 60%, transparent); }
  .rays { position: absolute; right: -120px; top: -150px; width: 900px; height: 900px; fill: rgba(255,203,69,.06); }
  .brand { position: absolute; left: 64px; top: 52px; display: flex; align-items: center; gap: 14px; font-weight: 800; font-size: 40px; letter-spacing: -.04em; }
  h1 { position: absolute; left: 60px; top: 150px; margin: 0; font-size: 112px; line-height: .9; letter-spacing: -.05em; font-weight: 800; font-variation-settings: "opsz" 96; }
  h1 span { color: #ff4436; position: relative; }
  h1 svg { position: absolute; left: -1%; bottom: .02em; width: 102%; height: .2em; color: #ffcb45; }
  .chips { position: absolute; left: 64px; bottom: 64px; display: flex; gap: 14px; }
  .chip { padding: 14px 18px; border-radius: 14px; background: #fff2d8; color: #17110c; font-weight: 700; font-size: 24px; box-shadow: 0 4px 0 #d9c7a3; }
  .chip.red { background: #ff4436; color: #fff; box-shadow: 0 4px 0 #a9231a; transform: rotate(2deg); }
  .chip:first-child { transform: rotate(-2deg); }
  .kim { position: absolute; right: 70px; bottom: -40px; height: 640px; filter: drop-shadow(0 30px 40px rgba(0,0,0,.6)); }
  .rocket { position: absolute; right: 40px; top: 40px; width: 130px; }
</style></head><body>
<div class="glow"></div>
<svg class="rays" viewBox="0 0 400 400">${Array.from({ length: 16 }, (_, i) => {
  const a = (i * Math.PI * 2) / 16, p = (t) => `${(200 + Math.cos(t) * 280).toFixed(1)} ${(200 + Math.sin(t) * 280).toFixed(1)}`;
  return `<path d="M200 200 L${p(a - 0.09)} L${p(a + 0.09)} Z"/>`;
}).join("")}</svg>
<img class="kim" src="data:image/webp;base64,${await b64("kim-sticker.webp")}">
<img class="rocket" src="data:image/svg+xml;base64,${await b64("rocket.svg")}">
<div class="brand"><img src="data:image/svg+xml;base64,${await b64("logo.svg")}" width="56" height="56">kimlaunch</div>
<h1>build your own<br><span>rocket<svg viewBox="0 0 300 24" preserveAspectRatio="none"><path d="M4 17c48-9 112-14 168-11 44 2 82 7 124 3" fill="none" stroke="currentColor" stroke-width="7" stroke-linecap="round"/></svg></span> here.</h1>
<div class="chips"><div class="chip">our own Raydium LaunchLab launchpad</div><div class="chip red">0.01 SOL per launch</div></div>
</body></html>`;

const icon = `<!doctype html><html><body style="margin:0;width:180px;height:180px;background:#0a0d12">
<img src="data:image/svg+xml;base64,${await b64("logo.svg")}" style="width:150px;height:150px;margin:15px"></body></html>`;

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1200, height: 630 } });
await page.route("**/*", async (r) => {
  const url = new URL(r.request().url());
  if (FONTS && url.hostname === "fonts.googleapis.com") return r.fulfill({ contentType: "text/css", body: await readFile(join(FONTS, "fonts.css")) });
  if (FONTS && url.hostname === "fonts.gstatic.com") {
    const file = join(FONTS, url.pathname.slice(1).replace(/\//g, "_"));
    if (existsSync(file)) return r.fulfill({ contentType: "font/woff2", body: await readFile(file) });
  }
  return r.continue();
});
await page.setContent(og, { waitUntil: "networkidle" });
await page.evaluate(() => document.fonts.ready);
await page.screenshot({ path: join(ASSETS, "og.jpg"), type: "jpeg", quality: 86 });
await page.setViewportSize({ width: 180, height: 180 });
await page.setContent(icon);
await page.screenshot({ path: join(ASSETS, "apple-touch-icon.png") });
await browser.close();
console.log("wrote og.jpg and apple-touch-icon.png");

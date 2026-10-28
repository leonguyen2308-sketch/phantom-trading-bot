// Renders the "launching soon" posters at 2x: poster-wide.jpg (1600x900, for X and Telegram) and
// poster-square.jpg (1080x1080). tools/art/kim-sticker-hd.webp is the mascot upscaled 4x
// (Real-ESRGAN) with a cleaned-up cutout, so he stays sharp at poster size.
// Run: node tools/poster.mjs   (OUT=<folder>, URL=<address>, STAMP=<stamp text>, E2E_FONTS=<cached fonts>)
import { mkdir, readFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

const { chromium } = createRequire(import.meta.url)("playwright");
const ASSETS = fileURLToPath(new URL("../site/assets/", import.meta.url));
const ART = fileURLToPath(new URL("./art/", import.meta.url));
const OUT = process.env.OUT || "promo";
const URL_SHOWN = process.env.URL || "kimrocket.netlify.app";
const STAMP = process.env.STAMP || "launching soon";
const FONTS = process.env.E2E_FONTS;
const data = async (dir, file, type) => `data:${type};base64,${(await readFile(join(dir, file))).toString("base64")}`;

const kim = await data(ART, "kim-sticker-hd.webp", "image/webp");
const logo = await data(ASSETS, "logo.svg", "image/svg+xml");
const rocket = await data(ASSETS, "rocket.svg", "image/svg+xml");
const pattern = await data(ASSETS, "pattern.svg", "image/svg+xml");

function rays(n = 20) {
  let s = "";
  for (let i = 0; i < n; i++) {
    const a = (i * Math.PI * 2) / n;
    const p = (t) => `${(500 + Math.cos(t) * 700).toFixed(1)} ${(500 + Math.sin(t) * 700).toFixed(1)}`;
    s += `<path d="M500 500 L${p(a - 0.07)} L${p(a + 0.07)} Z"/>`;
  }
  return `<svg class="rays" viewBox="0 0 1000 1000">${s}</svg>`;
}

const SWOOSH = `<svg class="sw" viewBox="0 0 300 24" preserveAspectRatio="none"><path d="M4 17c48-9 112-14 168-11 44 2 82 7 124 3" fill="none" stroke="currentColor" stroke-width="7" stroke-linecap="round"/></svg>`;
const [domain, rest] = URL_SHOWN.split(/\.(.*)/s);

const CSS = `
  * { box-sizing: border-box; }
  body { margin: 0; overflow: hidden; position: relative; background: #0a0d12; color: #f5f2ec;
    font-family: "Bricolage Grotesque", sans-serif; -webkit-font-smoothing: antialiased; }
  .bg { position: absolute; inset: 0; background-image: url(${pattern});
    background-size: 260px; opacity: .9; }
  .glow { position: absolute; border-radius: 50%; background: radial-gradient(closest-side, rgba(255,68,54,.55), rgba(255,68,54,.1) 60%, transparent); }
  .rays { position: absolute; fill: rgba(255,203,69,.065); }
  .vignette { position: absolute; inset: 0; background: radial-gradient(120% 90% at 30% 40%, transparent 55%, rgba(0,0,0,.55)); }
  .brand { position: absolute; display: flex; align-items: center; gap: 16px; font-weight: 800; letter-spacing: -.04em; }
  h1 { position: absolute; margin: 0; font-weight: 800; line-height: .88; letter-spacing: -.05em; font-variation-settings: "opsz" 96; }
  h1 .red { color: #ff4436; position: relative; display: inline-block; }
  .sw { position: absolute; left: -1%; bottom: -.02em; width: 102%; height: .2em; color: #ffcb45; overflow: visible; }
  .sub { position: absolute; margin: 0; font: 500 30px/1.3 Inter, sans-serif; color: #b4bac4; }
  .sub b { color: #f5f2ec; font-weight: 600; }
  .chips { position: absolute; display: flex; gap: 14px; }
  .chip { padding: 13px 20px; border-radius: 15px; font-weight: 700; font-size: 26px; letter-spacing: -.01em; background: #fff2d8; color: #17110c;
    box-shadow: 0 4px 0 #d9c7a3; white-space: nowrap; }
  .chip.red { background: #ff4436; color: #fff; box-shadow: 0 4px 0 #a9231a; }
  .chip.dark { background: #1a2029; color: #f5f2ec; box-shadow: inset 0 0 0 2px #303a48, 0 4px 0 #05070a; }
  .url { position: absolute; padding: 20px 32px 22px; border-radius: 22px; background: #fff2d8; color: #17110c; font-weight: 800;
    letter-spacing: -.035em; line-height: 1; box-shadow: 0 7px 0 #d9c7a3, 0 26px 60px rgba(0,0,0,.5); white-space: nowrap; }
  .url span { color: #e0352a; }
  .url small { display: block; margin-bottom: 8px; font: 700 17px "JetBrains Mono", monospace; letter-spacing: .14em; text-transform: uppercase; color: #8a7a5c; }
  .kim { position: absolute; filter: drop-shadow(0 30px 40px rgba(0,0,0,.6)); }
  .rocket { position: absolute; }
  .bubble { position: absolute; padding: 16px 22px 18px; border-radius: 22px; background: #fff2d8; color: #17110c; font-weight: 600;
    line-height: 1.15; box-shadow: 0 5px 0 #d9c7a3, 0 20px 40px rgba(0,0,0,.45); transform: rotate(-4deg); }
  .bubble b { display: block; font-weight: 800; }
  .bubble::after { content: ""; position: absolute; bottom: -14px; border: 15px solid transparent; border-top-color: #fff2d8; border-bottom: 0; }
  .stamp { position: absolute; border-radius: 20px; background: rgba(10,13,18,.9); }
  .stamp .ink { padding: 14px 28px 18px; border: 7px solid #ff4436; border-radius: 20px; color: #ff4436; text-align: center;
    box-shadow: inset 0 0 0 4px rgba(10,13,18,.9), inset 0 0 0 7px #ff4436; filter: url(#ink); }
  .stamp b { display: block; font-weight: 800; line-height: .9; letter-spacing: .005em; text-transform: uppercase; white-space: nowrap; }
  .stamp small { display: block; margin-bottom: 10px; font: 700 19px "JetBrains Mono", monospace; letter-spacing: .2em; text-transform: uppercase; }
  .fine { position: absolute; margin: 0; font: 500 17px Inter, sans-serif; color: #6f7886; }

  .wide { width: 1600px; height: 900px; }
  .wide .glow { left: 760px; top: -330px; width: 1000px; height: 1000px; }
  .wide .rays { left: 562px; top: -490px; width: 1400px; height: 1400px; }
  .wide .brand { left: 84px; top: 64px; font-size: 42px; }
  .wide .brand img { width: 58px; height: 58px; }
  .wide h1 { left: 78px; top: 214px; font-size: 136px; }
  .wide .sub { left: 84px; top: 484px; width: 900px; }
  .wide .chips { left: 84px; top: 570px; }
  .wide .url { left: 78px; top: 676px; font-size: 60px; transform: rotate(-1.5deg); }
  .wide .kim { right: 66px; bottom: -36px; height: 850px; }
  .wide .rocket { left: 1390px; top: 46px; width: 170px; height: 170px; }
  .wide .bubble { left: 846px; top: 96px; font-size: 27px; }
  .wide .bubble::after { right: 34px; transform: skewX(-28deg); }
  .wide .stamp { left: 1030px; top: 596px; transform: rotate(-8deg); }
  .wide .stamp b { font-size: 80px; }
  .wide .fine { left: 84px; bottom: 30px; }

  .square { width: 1080px; height: 1080px; }
  .square .glow { left: 380px; top: 60px; width: 900px; height: 900px; }
  .square .rays { left: 160px; top: -160px; width: 1320px; height: 1320px; }
  .square .brand { left: 62px; top: 56px; font-size: 38px; }
  .square .brand img { width: 52px; height: 52px; }
  .square h1 { left: 56px; top: 150px; font-size: 118px; }
  .square .sub { left: 62px; top: 392px; width: 470px; font-size: 27px; }
  .square .chips { left: 62px; top: 500px; flex-direction: column; align-items: flex-start; gap: 16px; }
  .square .chip { font-size: 24px; }
  .square .chip:nth-child(1) { transform: rotate(-2deg); }
  .square .chip:nth-child(2) { transform: rotate(1.5deg); }
  .square .chip:nth-child(3) { transform: rotate(-1deg); }
  .square .url { left: 56px; top: 770px; font-size: 50px; transform: rotate(-1.5deg); }
  .square .kim { right: 6px; bottom: -36px; height: 740px; }
  .square .rocket { left: 870px; top: 44px; width: 150px; height: 150px; }
  .square .bubble { left: 662px; top: 300px; font-size: 23px; }
  .square .bubble::after { right: 40px; transform: skewX(-26deg); }
  .square .stamp { left: 652px; top: 792px; transform: rotate(-8deg); }
  .square .stamp b { font-size: 62px; }
  .square .stamp small { font-size: 15px; }
  .square .fine { left: 62px; bottom: 30px; width: 540px; font-size: 15px; line-height: 1.45; }
`;

const INK = `<svg width="0" height="0" style="position:absolute"><filter id="ink" x="-5%" y="-5%" width="110%" height="110%">
  <feTurbulence type="fractalNoise" baseFrequency=".75" numOctaves="2" seed="11" result="noise"/>
  <feColorMatrix in="noise" type="matrix" values="0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 -9 6.4" result="holes"/>
  <feComposite in="SourceGraphic" in2="holes" operator="in" result="worn"/>
  <feTurbulence type="fractalNoise" baseFrequency=".035" numOctaves="2" seed="4" result="warp"/>
  <feDisplacementMap in="worn" in2="warp" scale="5" xChannelSelector="R" yChannelSelector="G"/>
</filter></svg>`;

const poster = (layout) => `<!doctype html><html><head>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500..800&family=Inter:wght@500;600&family=JetBrains+Mono:wght@700&display=swap">
<style>${CSS}</style></head><body class="${layout}">${INK}
<div class="bg"></div><div class="glow"></div>${rays()}<div class="vignette"></div>
<div class="brand"><img src="${logo}" alt="">kimlaunch</div>
<h1>build your own<br><span class="red">rocket${SWOOSH}</span> here.</h1>
<p class="sub">the supreme memecoin launchpad on <b>Raydium LaunchLab</b>.</p>
<div class="chips"><span class="chip">connect Phantom</span><span class="chip red">0.01 SOL per launch</span><span class="chip dark">trades instantly</span></div>
<div class="url"><small>launch pad address</small>${domain}<span>.${rest}</span></div>
<img class="kim" src="${kim}" alt="">
<img class="rocket" src="${rocket}" alt="">
<div class="bubble">comrades,<b>prepare your wallets.</b></div>
<div class="stamp"><div class="ink"><small>★ by order of the leader ★</small><b>${STAMP.replace(/ (?=\S+$)/, "<br>")}</b></div></div>
<p class="fine">parody launchpad · not affiliated with Raydium, Phantom or any government · memecoins are risky</p>
</body></html>`;

await mkdir(OUT, { recursive: true });
const browser = await chromium.launch();
try {
  for (const [layout, width, height] of [["wide", 1600, 900], ["square", 1080, 1080]]) {
    const page = await browser.newPage({ viewport: { width, height }, deviceScaleFactor: 2 });
    await page.route("**/*", async (r) => {
      const url = new URL(r.request().url());
      if (FONTS && url.hostname === "fonts.googleapis.com") return r.fulfill({ contentType: "text/css", body: await readFile(join(FONTS, "fonts.css")) });
      if (FONTS && url.hostname === "fonts.gstatic.com") {
        const file = join(FONTS, url.pathname.slice(1).replace(/\//g, "_"));
        if (existsSync(file)) return r.fulfill({ contentType: "font/woff2", body: await readFile(file) });
      }
      return r.continue();
    });
    await page.setContent(poster(layout), { waitUntil: "networkidle" });
    await page.evaluate(() => document.fonts.ready);
    const file = join(OUT, `poster-${layout}.jpg`);
    await page.screenshot({ path: file, type: "jpeg", quality: 92 });
    console.log(`wrote ${file} (${width * 2}x${height * 2})`);
    await page.close();
  }
} finally {
  await browser.close();
}

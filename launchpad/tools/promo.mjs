// Records the promo video: an intro card, a walkthrough of the real site UI against the mock world
// (demo coins, mock wallet), and an outro with the site's address. Frames come from a CDP
// screencast; ffmpeg turns them into an H.264 MP4 and adds tools/promo_music.py's soundtrack.
// Run: npm run build && node tools/promo.mjs   (OUT=<folder>, URL=<address shown in the outro>)
import { link, mkdir, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { createRequire } from "node:module";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { createWorld, FIXTURES } from "./world.mjs";

const { chromium } = createRequire(import.meta.url)("playwright");
const OUT = process.env.OUT || "promo";
const URL_SHOWN = process.env.URL || "kimrocket.netlify.app";
const W = 1920;
const H = 1080;
const FPS = 30;
const FRAMES = join(OUT, "frames");
const BEAT = 0.5; // tools/promo_music.py plays 120 BPM...
const DROP = 4; // ...and its beat kicks in after two bars
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---- overlays drawn on top of the site: cursor, captions, demo tag, intro and outro cards ------

const OVERLAY = `(() => {
  const css = \`
    #__cur { position: fixed; left: 0; top: 0; width: 34px; height: 34px; z-index: 2147483647; pointer-events: none;
      transform: translate(-200px, -200px); filter: drop-shadow(0 3px 4px rgba(0,0,0,.55)); transition: opacity .25s; }
    #__cur.off { opacity: 0; }
    #__rip { position: fixed; width: 56px; height: 56px; margin: -28px 0 0 -28px; border-radius: 50%; border: 4px solid #ffcb45;
      z-index: 2147483646; pointer-events: none; opacity: 0; }
    #__rip.go { animation: __rip .5s ease-out; }
    @keyframes __rip { from { transform: scale(.3); opacity: .95 } to { transform: scale(1.5); opacity: 0 } }
    #__cap { position: fixed; left: 50%; bottom: 54px; z-index: 2147483645; pointer-events: none; padding: 18px 30px; border-radius: 20px;
      background: #fff2d8; color: #17110c; font: 800 40px/1.1 "Bricolage Grotesque", system-ui, sans-serif; letter-spacing: -.02em;
      box-shadow: 0 6px 0 #d9c7a3, 0 26px 70px rgba(0,0,0,.6); white-space: nowrap;
      opacity: 0; transform: translate(-50%, 24px) rotate(-1deg); transition: opacity .35s, transform .35s; }
    #__cap.on { opacity: 1; transform: translate(-50%, 0) rotate(-1deg); }
    #__cap b { color: #e0352a; }
    #__demo { position: fixed; right: 22px; bottom: 16px; z-index: 2147483645; pointer-events: none;
      font: 600 15px "JetBrains Mono", monospace; color: rgba(245,242,236,.5); }
    .toasts { bottom: 50px !important; } /* clear of the demo tag */
  \`;
  function install() {
    if (document.getElementById("__cur")) return;
    const st = document.createElement("style"); st.textContent = css; document.head.append(st);
    const cur = document.createElement("div"); cur.id = "__cur";
    cur.innerHTML = '<svg viewBox="0 0 28 28" width="34" height="34"><path d="M5 3l17 9.5-7.4 1.8 4.3 8.3-3.4 1.7-4.3-8.4L5 21z" fill="#fff" stroke="#111" stroke-width="1.6" stroke-linejoin="round"/></svg>';
    const rip = document.createElement("div"); rip.id = "__rip";
    const cap = document.createElement("div"); cap.id = "__cap";
    const demo = document.createElement("div"); demo.id = "__demo"; demo.textContent = "demo footage · test coins"; demo.style.display = "none";
    document.body.append(cur, rip, cap, demo);
    addEventListener("mousemove", (e) => { cur.style.transform = "translate(" + (e.clientX - 5) + "px," + (e.clientY - 3) + "px)"; }, true);
    addEventListener("mousedown", (e) => { rip.style.left = e.clientX + "px"; rip.style.top = e.clientY + "px"; rip.classList.remove("go"); void rip.offsetWidth; rip.classList.add("go"); }, true);
    window.__caption = (h) => { if (!h) return cap.classList.remove("on"); cap.innerHTML = h; cap.classList.add("on"); };
    window.__demo = (on) => { demo.style.display = on ? "" : "none"; };
    window.__cursor = (on) => cur.classList.toggle("off", !on);
  }
  if (document.readyState === "loading") addEventListener("DOMContentLoaded", install); else install();
})();`;

function rays(n = 18, len = 900) {
  let s = "";
  for (let i = 0; i < n; i++) {
    const a = (i * Math.PI * 2) / n;
    const p = (t) => `${(len + Math.cos(t) * len * 1.4).toFixed(1)} ${(len + Math.sin(t) * len * 1.4).toFixed(1)}`;
    s += `<path d="M${len} ${len} L${p(a - 0.085)} L${p(a + 0.085)} Z"/>`;
  }
  return `<svg class="pv-rays" viewBox="0 0 ${len * 2} ${len * 2}">${s}</svg>`;
}

const CARD_CSS = `
  .pv { position: fixed; inset: 0; z-index: 2147483640; overflow: hidden; background: #0a0d12; color: #f5f2ec;
    font-family: "Bricolage Grotesque", system-ui, sans-serif; transition: opacity .6s ease; }
  .pv.out { opacity: 0; }
  .pv.wait, .pv.wait * { animation-play-state: paused !important; }
  .pv-glow { position: absolute; right: -6%; top: -18%; width: 1100px; height: 1100px; border-radius: 50%;
    background: radial-gradient(closest-side, rgba(255,68,54,.5), rgba(255,68,54,.08) 62%, transparent); }
  .pv-rays { position: absolute; right: -520px; top: -640px; width: 1800px; height: 1800px; fill: rgba(255,203,69,.06);
    animation: pvspin 40s linear infinite; }
  @keyframes pvspin { to { transform: rotate(360deg); } }
  .pv-brand { position: absolute; left: 110px; top: 86px; display: flex; align-items: center; gap: 18px; font-weight: 800; font-size: 50px;
    letter-spacing: -.04em; animation: pvfade .5s .1s both; }
  .pv-brand img { width: 70px; height: 70px; }
  .pv-h { position: absolute; left: 104px; top: 250px; margin: 0; font-weight: 800; font-size: 168px; line-height: .9; letter-spacing: -.05em;
    font-variation-settings: "opsz" 96; }
  .pv-h .w { display: inline-block; animation: pvpop .55s cubic-bezier(.2,.9,.3,1.25) both; }
  .pv-h .red { color: #ff4436; position: relative; }
  .pv-sw { position: absolute; left: -1%; bottom: .02em; width: 102%; height: .2em; color: #ffcb45; overflow: visible; }
  .pv-sw path { stroke-dasharray: 340; stroke-dashoffset: 340; animation: pvdraw .7s 1.1s ease-out forwards; }
  @keyframes pvdraw { to { stroke-dashoffset: 0; } }
  @keyframes pvpop { from { transform: translateY(60px) scale(.94); opacity: 0; } to { transform: none; opacity: 1; } }
  @keyframes pvfade { from { opacity: 0; transform: translateY(16px); } to { opacity: 1; transform: none; } }
  .pv-sub { position: absolute; left: 112px; top: 640px; max-width: 900px; margin: 0; font: 500 40px/1.3 Inter, system-ui, sans-serif; color: #b4bac4;
    animation: pvfade .6s 1.35s both; }
  .pv-chips { position: absolute; left: 110px; top: 760px; display: flex; gap: 18px; }
  .pv-chip { padding: 16px 22px; border-radius: 16px; font-weight: 700; font-size: 30px; background: #fff2d8; color: #17110c;
    box-shadow: 0 5px 0 #d9c7a3; animation: pvpop .5s both; }
  .pv-chip.red { background: #ff4436; color: #fff; box-shadow: 0 5px 0 #a9231a; }
  .pv-chip.dark { background: #1a2029; color: #f5f2ec; box-shadow: inset 0 0 0 2px #303a48, 0 5px 0 #05070a; }
  .pv-kim { position: absolute; right: 70px; bottom: -40px; height: 1000px; filter: drop-shadow(0 40px 50px rgba(0,0,0,.6));
    -webkit-mask-image: linear-gradient(#000 85%, transparent 99%); animation: pvkim .9s .35s cubic-bezier(.2,.9,.3,1.1) both; }
  @keyframes pvkim { from { transform: translateY(420px); } to { transform: none; } }
  .pv-rocket { position: absolute; width: 200px; height: 200px; right: 120px; top: 90px; animation: pvfly 1.3s 1.2s cubic-bezier(.3,.8,.3,1) both; }
  @keyframes pvfly { from { transform: translate(-900px, 700px) scale(.6); opacity: 0; } 30% { opacity: 1; } to { transform: none; } }
  .pv-url { position: absolute; left: 104px; top: 560px; padding: 26px 40px; border-radius: 26px; background: #fff2d8; color: #17110c;
    font: 800 92px/1 "Bricolage Grotesque", sans-serif; letter-spacing: -.035em; box-shadow: 0 8px 0 #d9c7a3, 0 30px 80px rgba(0,0,0,.5);
    transform: rotate(-1.5deg); animation: pvpop .6s .7s cubic-bezier(.2,.9,.3,1.3) both; }
  .pv-url span { color: #e0352a; }
  .pv-fine { position: absolute; left: 112px; bottom: 70px; font: 500 24px Inter, sans-serif; color: #7c8594; animation: pvfade .6s 1.6s both; }
`;

const LOGO = `<img src="assets/logo.svg" alt="">`;
const SWOOSH = `<svg class="pv-sw" viewBox="0 0 300 24" preserveAspectRatio="none"><path d="M4 17c48-9 112-14 168-11 44 2 82 7 124 3" fill="none" stroke="currentColor" stroke-width="7" stroke-linecap="round"/></svg>`;
const words = (list, from = 0.15) => list.map(([w, cls = ""], i) => `<span class="w ${cls}" style="animation-delay:${(from + i * 0.12).toFixed(2)}s">${w}</span>`);

function intro(name) {
  const [w1, w2, w3, w4, w5] = words([["build"], ["your"], ["own"], [`rocket${SWOOSH}`, "red"], ["here."]]);
  return `<div class="pv wait" id="pv"><style>${CARD_CSS}</style><div class="pv-glow"></div>${rays()}
    <div class="pv-brand">${LOGO}${name}</div>
    <h1 class="pv-h">${w1} ${w2} ${w3}<br>${w4} ${w5}</h1>
    <p class="pv-sub">the supreme memecoin launchpad on Raydium LaunchLab</p>
    <img class="pv-kim" src="assets/kim-sticker.webp" alt=""><img class="pv-rocket" src="assets/rocket.svg" alt=""></div>`;
}

function outro(name, url) {
  const [d, ot] = url.split(/\.(.*)/s);
  return `<div class="pv" id="pv"><style>${CARD_CSS}
      .pv-h { top: 220px; font-size: 104px; } .pv-chips { top: 790px; } .pv-kim { right: 10px; height: 940px; }
      .pv-rocket { right: 640px; top: 70px; width: 170px; height: 170px; }</style>
    <div class="pv-glow"></div>${rays()}
    <div class="pv-brand">${LOGO}${name}</div>
    <h1 class="pv-h">${words([["build"], ["your"], ["own"], [`rocket${SWOOSH}`, "red"], ["here."]], 0.05).join(" ")}</h1>
    <div class="pv-url">${d}<span>.${ot}</span></div>
    <div class="pv-chips">
      <span class="pv-chip" style="animation-delay:1.05s">connect Phantom</span>
      <span class="pv-chip red" style="animation-delay:1.2s">0.01 SOL per launch</span>
      <span class="pv-chip dark" style="animation-delay:1.35s">trades instantly</span>
    </div>
    <p class="pv-fine">parody launchpad · not affiliated with Raydium, Phantom or any government · memecoins are risky</p>
    <img class="pv-kim" src="assets/kim-sticker.webp" alt=""><img class="pv-rocket" src="assets/rocket.svg" alt=""></div>`;
}

// ---- recording ---------------------------------------------------------------------------------

await rm(OUT, { recursive: true, force: true });
await mkdir(FRAMES, { recursive: true });
const world = await createWorld({ delays: { upload: 450, simulate: 250, sign: 900, send: 350 } });
const browser = await chromium.launch();
const problems = [];
const page = await world.newPage(browser, { width: W, height: H }, { scale: 1, problems, initScripts: [OVERLAY] });

const frames = [];
let writes = Promise.resolve();
const cdp = await page.context().newCDPSession(page);
cdp.on("Page.screencastFrame", ({ data, metadata, sessionId }) => {
  const file = join(FRAMES, `${String(frames.length).padStart(5, "0")}.jpg`);
  frames.push({ file, t: metadata.timestamp });
  writes = writes.then(() => writeFile(file, Buffer.from(data, "base64")));
  cdp.send("Page.screencastFrameAck", { sessionId }).catch(() => {});
});
// Frame timestamps are seconds since the epoch, like Date.now(), and the video starts at the first
// frame. (That frame can carry the timestamp of a paint from before the screencast started.)
const videoTime = () => Date.now() / 1000 - frames[0].t;
async function until(t) {
  while (!frames.length) await sleep(5);
  await sleep(Math.max(0, (t - videoTime()) * 1000));
}

let mouse = { x: W / 2, y: H / 2 };
async function moveTo(target, { ms = 650, dx = 0, dy = 0 } = {}) {
  const box = typeof target === "string" ? await page.locator(target).first().boundingBox() : target;
  const x = box.width !== undefined ? box.x + box.width / 2 + dx : box.x;
  const y = box.height !== undefined ? box.y + box.height / 2 + dy : box.y;
  const from = { ...mouse };
  const start = Date.now();
  for (;;) {
    const k = Math.min(1, (Date.now() - start) / ms);
    const e = k < 0.5 ? 2 * k * k : 1 - (-2 * k + 2) ** 2 / 2; // ease in-out
    await page.mouse.move(from.x + (x - from.x) * e, from.y + (y - from.y) * e);
    if (k >= 1) break;
    await sleep(12);
  }
  mouse = { x, y };
}
async function click(target, opts) {
  await moveTo(target, opts);
  await sleep(90);
  await page.mouse.down();
  await sleep(60);
  await page.mouse.up();
}
const caption = (h) => page.evaluate((x) => window.__caption?.(x), h);
async function scrollTo(y, ms = 900) {
  await page.evaluate(
    ([to, dur]) =>
      new Promise((done) => {
        const from = scrollY;
        const t0 = performance.now();
        const step = (now) => {
          const k = Math.min(1, (now - t0) / dur);
          const e = k < 0.5 ? 4 * k * k * k : 1 - (-2 * k + 2) ** 3 / 2;
          scrollTo(0, from + (to - from) * e);
          k < 1 ? requestAnimationFrame(step) : done();
        };
        requestAnimationFrame(step);
      }),
    [y, ms],
  );
}
async function type(selector, text, delay = 55) {
  await click(selector);
  await page.locator(selector).pressSequentially(text, { delay });
}

const name = world.config.siteName;
try {
  // Warm up: board loaded, pictures decoded.
  await page.goto(world.siteUrl);
  await page.waitForFunction(() => document.querySelectorAll(".card img.coin-img").length >= 10, null, { timeout: 20000 });
  await page.evaluate(() => document.fonts.ready);
  await sleep(800);

  // Intro card, then fade to the site.
  await page.evaluate((h) => document.body.insertAdjacentHTML("beforeend", h), intro(name));
  await sleep(300);
  await cdp.send("Page.startScreencast", { format: "jpeg", quality: 90, maxWidth: W, maxHeight: H, everyNthFrame: 1 });
  await page.evaluate(() => document.getElementById("pv").classList.remove("wait"));
  await until(DROP); // the site comes in with the beat
  await page.evaluate(() => {
    document.getElementById("pv").classList.add("out");
    window.__demo(true);
  });
  await sleep(650);
  await page.evaluate(() => document.getElementById("pv")?.remove());

  // The board.
  await page.mouse.move(mouse.x, mouse.y);
  await caption("the supreme <b>memecoin launchpad</b>");
  await moveTo(".mascot .bubble", { ms: 700 });
  await sleep(900);
  const trendY = await page.evaluate(() => document.querySelector("#trending").getBoundingClientRect().top + scrollY - 120);
  await caption("every rocket trades <b>the second it launches</b>");
  await scrollTo(trendY, 900);
  await moveTo(".tcard:nth-child(3)", { ms: 500 });
  await sleep(700);
  const boardY = await page.evaluate(() => document.querySelector("#board").getBoundingClientRect().top + scrollY - 90);
  await scrollTo(boardY, 800);
  await moveTo(".card:nth-child(2)", { ms: 500 });
  await sleep(700);

  // Connect (the sidebar stays put while the page scrolls).
  await caption("connect <b>Phantom</b>");
  await click(".side [data-wallet-button]", { ms: 600 });
  await page.waitForFunction(() => document.querySelector(".side [data-wallet-button]")?.textContent.includes("SOL"));
  await sleep(700);

  // Build a rocket.
  await click(".side .side-create", { ms: 500 });
  await page.waitForSelector("#name");
  await caption("<b>build</b> your own rocket");
  await moveTo("#drop", { ms: 500 });
  await page.setInputFiles("#file", join(FIXTURES, "supreme.webp"));
  await page.waitForSelector(".drop.has-image");
  await sleep(250);
  await type("#name", "Supreme Leader Coin", 32);
  await type("#symbol", "KIMLEAD", 50);
  await type("#description", "the official coin of the supreme launchpad.", 12);
  await caption("fuel it yourself, <b>before the bots</b>");
  await click('[data-preset="0.5"]', { ms: 500 });
  await page.waitForFunction(() => /of supply/.test(document.querySelector("#devbuy-hint")?.textContent ?? ""));
  await sleep(900);
  await caption("one click · <b>0.01 SOL</b> to launch");
  await moveTo("#costs", { ms: 500 });
  await sleep(500);
  await click("#launch", { ms: 450 });
  await page.waitForSelector(".liftoff", { timeout: 30000 });
  await caption("liftoff. <b>it trades right now</b>");
  await sleep(1800);

  // The coin page: buy on the curve.
  await click(".liftoff a.btn-primary", { ms: 500 });
  await page.waitForSelector("#go");
  await caption("buy & sell on the curve · <b>graduates at 85 SOL</b>");
  await sleep(500);
  await click('[data-amount="0.5"]', { ms: 500 });
  await sleep(300);
  await click("#go", { ms: 450 });
  await page.waitForSelector(".toast-good", { timeout: 20000 });
  await sleep(1100);

  // Outro.
  await caption("");
  await page.evaluate(() => {
    window.__demo(false);
    window.__cursor(false);
  });
  // The card lands on a beat, with the music's impact. It takes a moment to render, so it goes in a
  // little early, once the caption has faded and the page is still.
  const outroAt = Math.ceil((videoTime() + 0.5) / BEAT) * BEAT;
  await until(outroAt - 0.1);
  const inserted = await page.evaluate((h) => {
    document.body.insertAdjacentHTML("beforeend", h);
    return (performance.timeOrigin + performance.now()) / 1000;
  }, outro(name, URL_SHOWN));
  await sleep(4600);
  const endEpoch = Date.now() / 1000;
  await cdp.send("Page.stopScreencast");
  await writes;

  // Video times. The first frame after the insert is the card's: trim (or stretch) the still frame
  // before it so the card starts exactly on the beat.
  const times = frames.map((f) => f.t - frames[0].t);
  const card = frames.findIndex((f) => f.t >= inserted);
  const shift = times[card] - outroAt;
  for (let i = card; i < times.length; i++) times[i] -= shift;
  const ticks = Math.round((endEpoch - frames[0].t - shift) * FPS);
  const length = ticks / FPS;

  // Constant frame rate: each tick shows the latest frame by then (hard links, numbered for ffmpeg).
  const seq = join(OUT, "seq");
  await mkdir(seq);
  for (let n = 0, i = 0; n < ticks; n++) {
    while (i + 1 < frames.length && times[i + 1] <= n / FPS + 1e-6) i++;
    await link(frames[i].file, join(seq, `${String(n).padStart(5, "0")}.jpg`));
  }
  const video = join(OUT, "video.mp4");
  execFileSync("ffmpeg", ["-y", "-loglevel", "error", "-framerate", String(FPS), "-i", join(seq, "%05d.jpg"),
    "-vf", "format=yuv420p", "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-movflags", "+faststart", video]);
  const music = join(OUT, "music.wav");
  // The music's impact lands when the outro card comes in.
  execFileSync("python3", [fileURLToPath(new URL("./promo_music.py", import.meta.url)), music, length.toFixed(2), outroAt.toFixed(2)]);
  const final = join(OUT, "kimlaunch-promo.mp4");
  execFileSync("ffmpeg", ["-y", "-loglevel", "error", "-i", video, "-i", music, "-map", "0:v", "-map", "1:a",
    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-af", `afade=t=in:d=0.4,afade=t=out:st=${(length - 1.6).toFixed(2)}:d=1.6`,
    "-shortest", "-movflags", "+faststart", final]);
  console.log(`${frames.length} frames, outro card on the beat at ${outroAt}s (moved ${Math.round(shift * 1000)} ms) -> ${final} (${length.toFixed(1)}s)`);
  const real = problems.filter((p) => !/Failed to load resource/.test(p));
  if (real.length) console.warn(`browser errors:\n${real.join("\n")}`);
} finally {
  await browser.close();
  world.close();
}

// Browser test of the whole site against a mock chain, mock wallet and mock Arweave:
// board, connect, launch with a first buy (fee checked on the wire), buy, sell, screenshots.
// Run: npm run build && npm run e2e   (screenshots land in $SHOTS or ./shots)
import { createServer } from "node:http";
import { readFile, mkdir, readdir } from "node:fs/promises";
import { existsSync } from "node:fs";
import { extname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";
import assert from "node:assert/strict";
import { Keypair, PublicKey, SystemInstruction, SystemProgram, VersionedTransaction } from "@solana/web3.js";
import { DataItem } from "@dha-team/arbundles";
import * as ll from "../src/chain/launchlab.js";
import { createMockChain } from "./mock-chain.mjs";

const { chromium } = createRequire(import.meta.url)("playwright");
const SITE = fileURLToPath(new URL("../site/", import.meta.url));
const FIXTURES = fileURLToPath(new URL("./fixtures/", import.meta.url));
const SHOTS = process.env.SHOTS || "shots";
const FONTS = process.env.E2E_FONTS; // optional folder with a cached Google Fonts css + files
const FEE_WALLET = new PublicKey("DFAcvisLammoF7ZDLJWyC47EqKzXqWRWgwdNS34qa9Lj");
const RPC = "https://rpc.mock.test/";
let configFeeWallet = FEE_WALLET; // switched to the user's own wallet for the "launch from the fee wallet" check

const TYPES = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".webp": "image/webp", ".jpg": "image/jpeg", ".png": "image/png" };

function serve() {
  return new Promise((resolve) => {
    const server = createServer(async (req, res) => {
      const path = new URL(req.url, "http://x").pathname;
      try {
        const body = await readFile(join(SITE, path === "/" ? "index.html" : path));
        res.writeHead(200, { "content-type": TYPES[extname(path)] ?? "text/html" });
        res.end(body);
      } catch {
        res.writeHead(404).end();
      }
    });
    server.listen(0, () => resolve({ server, url: `http://localhost:${server.address().port}` }));
  });
}

// ---- mock world --------------------------------------------------------------------------------
const chain = createMockChain({ feeWallet: FEE_WALLET });
const arweave = new Map(); // id -> { type, body }
let nextId = 0;
const store = (type, body) => {
  const id = `mock${String(nextId++).padStart(39, "0")}`;
  arweave.set(id, { type, body: Buffer.from(body) });
  return id;
};

const SEEDS = [
  ["Supreme Rocket", "SUPREME", "supreme.webp", 61.2, false, 5400, "the leader's own rocket. approved."],
  ["Juche Coin", "JUCHE", "juche.webp", 85, true, 86400 * 2, "self-reliance, but make it a memecoin."],
  ["Comrade Frog", "CFROG", "frog.webp", 34.5, false, 7200, "ribbit for the republic."],
  ["Orbit Program", "ORBIT", "orbit.webp", 72.8, false, 3600 * 9, "five-year plan: orbit by friday."],
  ["Kimchi Fund", "KIMCHI", "kimchi.webp", 12.1, false, 1500, "fermented gains."],
  ["Hermit Hamster", "HMSTR", "hamster.webp", 4.4, false, 600, ""],
  ["Glorious Medal", "MEDAL", "medal.webp", 85, true, 86400 * 4, "every holder gets one."],
  ["Penguin Parade", "PNGN", "penguin.webp", 21.9, false, 3600 * 30, "march with us."],
  ["Parade Day", "PARADE", "parade.webp", 47.3, false, 3600 * 3, "the biggest parade on solana."],
  ["Dear Shiba", "DSHIB", "shiba.webp", 8.7, false, 900, "much leader. very supreme."],
];

for (const [name, symbol, file, raised, graduated, ago, description] of SEEDS) {
  const image = store("image/webp", await readFile(join(FIXTURES, file)));
  const meta = store(
    "application/json",
    JSON.stringify({ name, symbol, description, image: `https://arweave.net/${image}`, twitter: symbol === "SUPREME" ? "https://x.com/supreme" : "", website: symbol === "ORBIT" ? "https://orbit.example" : "" }),
  );
  chain.seedLaunch({ name, symbol, uri: `https://arweave.net/${meta}`, raisedSol: raised, graduated, agoSeconds: ago });
}
chain.seedNoise();

const user = Keypair.generate();
chain.credit(user.publicKey, 3_000_000_000);

const walletScript = (address, bytes) => `(() => {
  const account = { address: "${address}", publicKey: new Uint8Array(${JSON.stringify(bytes)}), chains: ["solana:mainnet", "solana:devnet"], features: ["solana:signTransaction"] };
  const listeners = {};
  let authorized = false;
  const wallet = {
    version: "1.0.0", name: "Phantom", chains: ["solana:mainnet", "solana:devnet"], accounts: [],
    icon: "data:image/svg+xml;base64,${Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="8" fill="#ab9ff2"/></svg>').toString("base64")}",
    features: {
      "standard:connect": { version: "1.0.0", connect: async ({ silent } = {}) => {
        if (silent && !authorized) return { accounts: [] };
        authorized = true; wallet.accounts = [account]; return { accounts: wallet.accounts }; } },
      "standard:disconnect": { version: "1.0.0", disconnect: async () => { wallet.accounts = []; } },
      "standard:events": { version: "1.0.0", on: (ev, fn) => { (listeners[ev] ||= []).push(fn); return () => {}; } },
      "solana:signTransaction": { version: "1.0.0", supportedTransactionVersions: ["legacy", 0],
        signTransaction: async (...inputs) => Promise.all(inputs.map(async (input) => ({ signedTransaction: new Uint8Array(await window.__mockSign(Array.from(input.transaction))) }))) },
    },
  };
  const register = (api) => api.register(wallet);
  window.addEventListener("wallet-standard:app-ready", (e) => register(e.detail));
  try { window.dispatchEvent(new CustomEvent("wallet-standard:register-wallet", { detail: register })); } catch {}
})();`;

async function route(r, siteUrl) {
  const req = r.request();
  const url = new URL(req.url());
  if (url.origin === siteUrl && url.pathname === "/config.js") {
    const cfg = { siteName: "kimlaunch", siteUrl: "https://kimlaunch.example", network: "mainnet", rpcUrl: RPC, feeWallet: configFeeWallet.toBase58(), feeSol: 0.01, links: { x: "https://x.com/kimlaunch", telegram: "" } };
    return r.fulfill({ contentType: "text/javascript", body: `window.KIMLAUNCH_CONFIG = ${JSON.stringify(cfg)};` });
  }
  if (url.origin === siteUrl) return r.continue();
  if (url.href.startsWith(RPC)) {
    return r.fulfill({ contentType: "application/json", body: JSON.stringify(chain.handle(JSON.parse(req.postData()))) });
  }
  if (url.hostname === "upload.ardrive.io") {
    const body = req.postDataBuffer();
    if (url.pathname === "/v1/tx/solana") {
      const item = new DataItem(body);
      assert.equal(await DataItem.verify(body), true, "upload must be a valid signed data item");
      const type = item.tags.find((t) => t.name === "Content-Type")?.value;
      arweave.set(item.id, { type, body: Buffer.from(item.rawData) });
      return r.fulfill({ contentType: "application/json", body: JSON.stringify({ id: item.id, owner: item.owner }) });
    }
    return r.fulfill({ status: 404, body: "no" });
  }
  if (url.hostname === "arweave.net") {
    const hit = arweave.get(url.pathname.slice(1));
    return hit ? r.fulfill({ contentType: hit.type, body: hit.body, headers: { "access-control-allow-origin": "*" } }) : r.fulfill({ status: 404, body: "" });
  }
  if (url.hostname === "lite-api.jup.ag") {
    return r.fulfill({ contentType: "application/json", body: JSON.stringify({ So11111111111111111111111111111111111111112: { usdPrice: 178.42 } }), headers: { "access-control-allow-origin": "*" } });
  }
  if (FONTS && url.hostname === "fonts.googleapis.com") {
    return r.fulfill({ contentType: "text/css", body: await readFile(join(FONTS, "fonts.css")) });
  }
  if (FONTS && url.hostname === "fonts.gstatic.com") {
    const file = join(FONTS, url.pathname.slice(1).replace(/\//g, "_"));
    if (existsSync(file)) return r.fulfill({ contentType: "font/woff2", body: await readFile(file), headers: { "access-control-allow-origin": "*" } });
  }
  return r.abort();
}

// ---- run ---------------------------------------------------------------------------------------
await mkdir(SHOTS, { recursive: true });
const { server, url: siteUrl } = await serve();
const browser = await chromium.launch();
const problems = [];

async function newPage(viewport) {
  const context = await browser.newContext({ viewport, deviceScaleFactor: 2, isMobile: viewport.width < 600, hasTouch: viewport.width < 600 });
  await context.exposeFunction("__mockSign", (bytes) => {
    const tx = VersionedTransaction.deserialize(Uint8Array.from(bytes));
    tx.sign([user]);
    return Array.from(tx.serialize());
  });
  await context.addInitScript(walletScript(user.publicKey.toBase58(), Array.from(user.publicKey.toBytes())));
  await context.route("**/*", (r) => route(r, siteUrl));
  const page = await context.newPage();
  page.on("pageerror", (e) => problems.push(`pageerror: ${e.message}`));
  page.on("console", (m) => m.type() === "error" && problems.push(`console: ${m.text()}`));
  return page;
}

async function shot(page, name, full = true) {
  if (full) await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: join(SHOTS, `${name}.png`), fullPage: full });
}
const step = (s) => console.log(`• ${s}`);

try {
  // Board
  const page = await newPage({ width: 1440, height: 900 });
  await page.goto(siteUrl);
  await page.waitForSelector(".card .card-name", { timeout: 15000 });
  await page.waitForFunction(() => document.querySelectorAll(".card").length >= 10);
  await page.waitForFunction(() => document.querySelectorAll(".card img.coin-img").length >= 10, null, { timeout: 15000 });
  await page.waitForTimeout(600);
  const names = await page.$$eval(".card .card-name b", (els) => els.map((e) => e.textContent));
  assert.equal(names.length, 10, "every seeded launch is listed, and the bare transfer is not");
  assert.equal(names[0], "Hermit Hamster", "newest first");
  assert.ok(await page.isVisible(".tcard"), "trending rail shows");
  assert.match(await page.textContent(".king-card"), /Orbit Program/, "the supreme rocket is the biggest coin still on its curve");
  await shot(page, "board-desktop", false);
  await shot(page, "board-desktop-full");
  step("board lists 10 launches, newest first, noise ignored");

  // Tabs and search
  await page.click('[data-tab="orbit"]');
  assert.deepEqual((await page.$$eval(".card .card-name b", (els) => els.map((e) => e.textContent))).sort(), ["Glorious Medal", "Juche Coin"]);
  await page.click('[data-tab="new"]');
  await page.fill("#search", "frog");
  await page.waitForFunction(() => document.querySelectorAll(".card").length === 1);
  await page.fill("#search", "");
  step("tabs and search filter the board");

  // Connect
  await page.click(".side [data-wallet-button]");
  await page.waitForFunction(() => document.querySelector(".side [data-wallet-button]")?.textContent.includes("SOL"));
  step("wallet connects and shows its balance");

  // Create with a first buy
  await page.goto(`${siteUrl}/#/create`);
  await page.waitForSelector("#name");
  await page.fill("#symbol", "juche");
  await page.waitForFunction(() => /already on the pad/.test(document.querySelector("#taken")?.textContent ?? ""));
  await page.fill("#symbol", "");
  step("a ticker already on the pad is refused");
  await page.setInputFiles("#file", join(FIXTURES, "supreme.webp"));
  await page.waitForSelector(".drop.has-image");
  await page.fill("#name", "Supreme Leader Coin");
  await page.fill("#symbol", "kimlead");
  assert.equal(await page.inputValue("#symbol"), "KIMLEAD", "ticker is uppercased");
  await page.fill("#description", "the official coin of the launchpad.");
  await page.click("details.box summary");
  await page.fill("#twitter", "https://x.com/kimlaunch");
  await page.click('[data-preset="0.5"]');
  await page.waitForFunction(() => /of supply/.test(document.querySelector("#devbuy-hint")?.textContent ?? ""));
  await page.waitForTimeout(300);
  await shot(page, "create-desktop");
  const before = chain.balance(user.publicKey);
  await page.click("#launch");
  await page.waitForSelector(".liftoff", { timeout: 20000 });
  await page.waitForTimeout(900);
  await shot(page, "launch-success", false);

  const launches = chain.sent.filter((s) => s.effects.created);
  assert.equal(launches.length, 1, "one create transaction reached the chain");
  const created = launches[0];
  assert.equal(created.effects.feePaid, 10_000_000n, "0.01 SOL launch fee paid to the fee wallet");
  const feeIx = created.instructions.find((ix) => ix.programId.equals(SystemProgram.programId) && SystemInstruction.decodeTransfer(ix).toPubkey.equals(FEE_WALLET));
  assert.ok(SystemInstruction.decodeTransfer(feeIx).fromPubkey.equals(user.publicKey), "the launcher pays the fee");
  assert.equal(created.effects.created.name, "Supreme Leader Coin");
  assert.equal(created.effects.created.symbol, "KIMLEAD");
  assert.ok(created.size <= 1232);
  const meta = JSON.parse(arweave.get(created.effects.created.uri.split("/").pop()).body.toString());
  assert.equal(meta.name, "Supreme Leader Coin");
  assert.equal(meta.twitter, "https://x.com/kimlaunch");
  assert.ok(arweave.get(meta.image.split("/").pop()).type.startsWith("image/"), "image stored on arweave");
  const bought = chain.sent.find((s) => s.effects.bought);
  assert.ok(bought && bought.effects.bought.mint === created.effects.created.mint, "first buy landed on the new coin");
  const spent = (before - chain.balance(user.publicKey)) / 1e9;
  assert.ok(spent > 0.5 && spent < 0.54, `launch + 0.5 SOL first buy cost ${spent} SOL`);
  step(`launch: fee paid, coin created, first buy landed (${chain.sent.length} tx, ${created.size} bytes, ${spent.toFixed(4)} SOL)`);

  // Coin page: buy then sell
  await page.click(".liftoff a.btn-primary");
  await page.waitForSelector(".coin-page .kpi");
  await page.waitForSelector("#go");
  assert.match(await page.textContent(".coin-title h1"), /Supreme Leader Coin/);
  await page.waitForTimeout(500);
  await shot(page, "coin-desktop");
  await page.click('[data-amount="0.1"]');
  await page.waitForFunction(() => /you get/.test(document.querySelector("#quote")?.textContent ?? ""));
  await page.click("#go");
  await page.waitForSelector(".toast-good", { timeout: 15000 });
  assert.equal(chain.sent.filter((s) => s.effects.bought).length, 2, "the buy reached the chain");
  await page.click('[data-side="sell"]');
  await page.waitForFunction(() => /max/.test(document.querySelector(".presets")?.textContent ?? ""));
  await page.click('[data-amount="0.5"]');
  await page.click("#go");
  await page.waitForFunction(() => [...document.querySelectorAll(".toast-good")].some((t) => /sold/.test(t.textContent)), null, { timeout: 15000 });
  assert.ok(chain.sent.find((s) => s.effects.sold), "the sell reached the chain");
  step("coin page: buy and sell landed");

  // Without the lookup table, a long name and ticker push create + first buy past one
  // transaction: they go as two, signed in one wallet prompt.
  chain.dropLookupTable();
  const sentBefore = chain.sent.length;
  await page.goto(`${siteUrl}/#/create`);
  await page.waitForSelector("#name");
  await page.setInputFiles("#file", join(FIXTURES, "juche.webp"));
  await page.waitForSelector(".drop.has-image");
  await page.fill("#name", "Supreme Leader Rocket Coin! 🚀");
  await page.fill("#symbol", "KIMROCKETS");
  await page.click('[data-preset="1"]');
  await page.waitForFunction(() => /of supply/.test(document.querySelector("#devbuy-hint")?.textContent ?? ""));
  await page.click("#launch");
  await page.waitForSelector(".liftoff", { timeout: 20000 });
  const [createTx, buyTx] = chain.sent.slice(sentBefore);
  assert.ok(createTx?.effects.created && buyTx?.effects.bought, "create, then buy");
  assert.equal(createTx.effects.feePaid, 10_000_000n, "fee rides with the create");
  assert.equal(buyTx.effects.bought.mint, createTx.effects.created.mint);
  assert.equal(createTx.tx.message.header.numRequiredSignatures, 2);
  assert.equal(buyTx.tx.message.header.numRequiredSignatures, 1);
  step(`split launch: create (${createTx.size} bytes) then first buy (${buyTx.size} bytes), one prompt`);
  await page.click(".liftoff [data-close]");

  // Board shows the new coin
  await page.goto(`${siteUrl}/#/`);
  await page.waitForFunction(() => [...document.querySelectorAll(".card .card-name b")].some((b) => b.textContent === "Supreme Leader Coin"));
  step("the new coin is on the board");

  await page.goto(`${siteUrl}/#/how`);
  await page.waitForSelector(".how-steps");
  await shot(page, "how-desktop");

  await page.goto(`${siteUrl}/#/live`);
  await page.waitForSelector(".card");
  await shot(page, "live-desktop", false);

  // Ideas
  await page.goto(`${siteUrl}/#/`);
  await page.click(".mascot .bubble");
  await page.waitForSelector(".idea");
  await shot(page, "ideas-modal", false);
  await page.click(".idea >> nth=0");
  await page.waitForSelector("#name");
  assert.ok((await page.inputValue("#name")).length > 0, "an idea fills the form");
  step("the leader pitches ideas that fill the form");

  // Launching from the fee wallet itself: the page says the fee comes straight back.
  configFeeWallet = user.publicKey;
  const own = await newPage({ width: 1440, height: 900 });
  await own.goto(`${siteUrl}/#/create`);
  await own.waitForSelector("#name");
  await own.click(".side [data-wallet-button]");
  await own.waitForFunction(() => /comes back to you/.test(document.querySelector("#costs")?.textContent ?? ""));
  configFeeWallet = FEE_WALLET;
  await own.close();
  step("launching from the fee wallet shows the fee coming back");

  // Mobile
  const phone = await newPage({ width: 390, height: 844 });
  await phone.goto(siteUrl);
  await phone.waitForFunction(() => document.querySelectorAll(".card img.coin-img").length >= 10, null, { timeout: 15000 });
  await phone.waitForTimeout(500);
  await shot(phone, "board-mobile", false);
  await shot(phone, "board-mobile-full");
  const overflow = await phone.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  assert.ok(overflow <= 1, `no sideways scroll on a phone (${overflow}px)`);
  await phone.goto(`${siteUrl}/#/create`);
  await phone.waitForSelector("#name");
  await shot(phone, "create-mobile");
  const overflow2 = await phone.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  assert.ok(overflow2 <= 1, `create page fits a phone (${overflow2}px)`);
  const coinMint = created.effects.created.mint;
  await phone.goto(`${siteUrl}/#/coin/${coinMint}`);
  await phone.waitForSelector(".coin-page .kpi");
  await phone.waitForTimeout(400);
  await shot(phone, "coin-mobile");
  step("phone layout fits without sideways scroll");

  const real = problems.filter((p) => !/Failed to load resource/.test(p));
  if (real.length) throw new Error(`browser errors:\n${real.join("\n")}`);
  console.log(`\nall good. screenshots in ${SHOTS}/: ${(await readdir(SHOTS)).join(", ")}`);
} finally {
  await browser.close();
  server.close();
}

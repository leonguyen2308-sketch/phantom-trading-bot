// The mock world browser tools run the site in: a static server for site/, a mock chain with
// demo launches, mock Arweave and price API, and a Wallet Standard "Phantom" that signs in Node.
// Used by tools/e2e.mjs (tests) and tools/promo.mjs (the promo video).
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { extname, join } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";
import { Keypair, PublicKey, VersionedTransaction } from "@solana/web3.js";
import { DataItem } from "@dha-team/arbundles";
import { createMockChain } from "./mock-chain.mjs";

export const SITE = fileURLToPath(new URL("../site/", import.meta.url));
export const FIXTURES = fileURLToPath(new URL("./fixtures/", import.meta.url));
export const FEE_WALLET = new PublicKey("DFAcvisLammoF7ZDLJWyC47EqKzXqWRWgwdNS34qa9Lj");
export const RPC = "https://rpc.mock.test/";

const TYPES = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".webp": "image/webp", ".jpg": "image/jpeg", ".png": "image/png" };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// [name, ticker, picture, SOL raised, graduated, seconds ago, description]
export const SEEDS = [
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

// `delays` (ms) slow the mock down so a recording can show each step: upload, simulate, sign, send.
export async function createWorld({ fonts = process.env.E2E_FONTS, delays = {} } = {}) {
  const chain = createMockChain({ feeWallet: FEE_WALLET });
  const arweave = new Map(); // id -> { type, body }
  let nextId = 0;
  const store = (type, body) => {
    const id = `mock${String(nextId++).padStart(39, "0")}`;
    arweave.set(id, { type, body: Buffer.from(body) });
    return id;
  };
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

  // Mutable: tests switch the fee wallet or the hide list before opening a new page.
  const config = {
    siteName: "kimlaunch", siteUrl: "https://kimlaunch.example", network: "mainnet", rpcUrl: RPC,
    feeWallet: FEE_WALLET.toBase58(), hide: [], feeSol: 0.01, links: { x: "https://x.com/kimlaunch", telegram: "" },
  };
  const extra = []; // (url, route) => handled?

  const { server, url: siteUrl } = await serve();

  async function route(r) {
    const req = r.request();
    const url = new URL(req.url());
    for (const handler of extra) if (await handler(url, r)) return;
    if (url.origin === siteUrl && url.pathname === "/config.js") {
      return r.fulfill({ contentType: "text/javascript", body: `window.KIMLAUNCH_CONFIG = ${JSON.stringify(config)};` });
    }
    if (url.origin === siteUrl) return r.continue();
    if (url.href.startsWith(RPC)) {
      const body = JSON.parse(req.postData());
      const methods = (Array.isArray(body) ? body : [body]).map((b) => b.method);
      if (delays.send && methods.includes("sendTransaction")) await sleep(delays.send);
      if (delays.simulate && methods.includes("simulateTransaction")) await sleep(delays.simulate);
      return r.fulfill({ contentType: "application/json", body: JSON.stringify(chain.handle(body)) });
    }
    if (url.hostname === "upload.ardrive.io") {
      const body = req.postDataBuffer();
      if (delays.upload) await sleep(delays.upload);
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
    if (fonts && url.hostname === "fonts.googleapis.com") {
      return r.fulfill({ contentType: "text/css", body: await readFile(join(fonts, "fonts.css")) });
    }
    if (fonts && url.hostname === "fonts.gstatic.com") {
      const file = join(fonts, url.pathname.slice(1).replace(/\//g, "_"));
      if (existsSync(file)) return r.fulfill({ contentType: "font/woff2", body: await readFile(file), headers: { "access-control-allow-origin": "*" } });
    }
    return r.abort();
  }

  async function newPage(browser, viewport, { problems = [], scale = 2, initScripts = [] } = {}) {
    const phone = viewport.width < 600;
    const context = await browser.newContext({ viewport, deviceScaleFactor: scale, isMobile: phone, hasTouch: phone });
    await context.exposeFunction("__mockSign", async (bytes) => {
      if (delays.sign) await sleep(delays.sign);
      const tx = VersionedTransaction.deserialize(Uint8Array.from(bytes));
      tx.sign([user]);
      return Array.from(tx.serialize());
    });
    await context.addInitScript(walletScript(user.publicKey.toBase58(), Array.from(user.publicKey.toBytes())));
    for (const s of initScripts) await context.addInitScript(s);
    await context.route("**/*", route);
    const page = await context.newPage();
    page.on("pageerror", (e) => problems.push(`pageerror: ${e.message}`));
    page.on("console", (m) => m.type() === "error" && problems.push(`console: ${m.text()}`));
    return page;
  }

  return {
    chain,
    arweave,
    user,
    config,
    siteUrl,
    newPage,
    addRoute: (handler) => extra.push(handler),
    close: () => server.close(),
  };
}

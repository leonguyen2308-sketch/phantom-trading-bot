// kimlaunch: the app shell. Config, live data from chain, wallet, router.
import { Connection, PublicKey } from "@solana/web3.js";
import * as ll from "./chain/launchlab.js";
import { fetchCoinMeta, fetchLaunches, fetchPools } from "./chain/indexer.js";
import { solUsd } from "./chain/price.js";
import { failoverFetch, rpcEndpoints } from "./chain/rpc.js";
import { availableWallets, isMobile, onWalletsChanged, phantomBrowseLink } from "./chain/wallet.js";
import { $, $$, copyText, html, icon, mount, on, openModal, short, sol, toast, ago } from "./ui/dom.js";
import { renderBoard } from "./ui/board.js";
import { renderCreate } from "./ui/create.js";
import { renderCoin } from "./ui/coin.js";
import { renderHow } from "./ui/how.js";
import { pitchThree } from "./ui/ideas.js";

const PRIORITY = { normal: 50_000, fast: 200_000, turbo: 1_000_000 };

function readConfig(raw) {
  const network = raw.network === "devnet" ? "devnet" : "mainnet";
  let feeWallet;
  try {
    feeWallet = new PublicKey(raw.feeWallet);
  } catch {
    throw new Error("config.js: feeWallet is not a valid Solana address");
  }
  const feeSol = Number(raw.feeSol ?? 0.01);
  if (!Number.isFinite(feeSol) || feeSol < 0 || feeSol > 1) throw new Error("config.js: feeSol must be between 0 and 1");
  const listMinFeeSol = Number(raw.listMinFeeSol ?? feeSol);
  const toLamports = (s) => BigInt(Math.round(s * 1e9));
  const siteUrl = String(raw.siteUrl || location.origin).replace(/\/$/, "");
  return {
    name: String(raw.siteName || "kimlaunch"),
    url: siteUrl,
    network,
    rpcEndpoints: rpcEndpoints(network, String(raw.rpcUrl || "").trim()),
    usingPublicRpc: !String(raw.rpcUrl || "").trim(),
    feeWallet,
    feeSol,
    feeLamports: toLamports(feeSol),
    listMinFeeLamports: toLamports(Math.min(listMinFeeSol, feeSol)),
    platformId: raw.platformId ? String(raw.platformId) : "",
    configId: raw.configId ? String(raw.configId) : "",
    migrateType: raw.migrateType === "amm" ? "amm" : "cpmm",
    microLamports: PRIORITY[raw.priorityFee] ?? PRIORITY.fast,
    links: { x: raw.links?.x || "", telegram: raw.links?.telegram || "" },
    hide: new Set((Array.isArray(raw.hide) ? raw.hide : []).map((v) => String(v).trim().replace(/^\$/, "").toLowerCase()).filter(Boolean)),
  };
}

// ---- the app object every view gets --------------------------------------------------------

export const app = {
  cfg: null,
  connection: null,
  program: null,
  wallet: null,
  publicKey: null,
  balance: null,
  launches: [], // newest first
  pools: new Map(), // mint -> pool
  metas: new Map(), // mint -> metadata JSON summary
  solUsd: null,
  status: "loading", // loading | live | error
  error: null,
  done: false,
  listeners: new Set(),
  emit() {
    for (const fn of this.listeners) {
      try {
        fn();
      } catch (e) {
        console.error(e);
      }
    }
  },
  subscribe(fn) {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  },
  explorer(kind, id) {
    const cluster = this.cfg.network === "devnet" ? "?cluster=devnet" : "";
    return `https://solscan.io/${kind}/${id}${cluster}`;
  },
  // Coins with their live numbers, newest first. Later copies of a ticker or name are left out.
  coins() {
    return originals(this.launches.filter((l) => !this.isHidden(l))).map((l) =>
      coinView(l, this.pools.get(l.mint), this.metas.get(l.mint), this.solUsd),
    );
  },
  // The coin already using this ticker or name, if any.
  taken({ name, symbol }) {
    const n = name?.trim().toLowerCase();
    const s = symbol?.trim().toLowerCase();
    return originals(this.launches.filter((l) => !this.isHidden(l))).find((l) => (s && l.symbol.toLowerCase() === s) || (n && l.name.toLowerCase() === n)) ?? null;
  },
  // Coins the site owner listed in config.js `hide`, by mint address or ticker.
  isHidden(coin) {
    const hide = this.cfg?.hide;
    return Boolean(hide?.size && (hide.has(String(coin.mint).toLowerCase()) || hide.has(String(coin.symbol ?? "").toLowerCase())));
  },
};

function originals(launches) {
  const seen = new Set();
  const keep = new Set();
  for (const l of [...launches].sort((a, b) => (a.time ?? 0) - (b.time ?? 0) || a.slot - b.slot)) {
    const keys = [`s:${l.symbol.toLowerCase()}`, `n:${l.name.toLowerCase()}`];
    if (keys.some((k) => seen.has(k))) continue;
    keys.forEach((k) => seen.add(k));
    keep.add(l.mint);
  }
  return launches.filter((l) => keep.has(l.mint));
}

export function coinView(launch, pool, meta, usdPerSol) {
  const live = pool ? ll.poolPrice(pool) : null;
  const graduated = pool ? pool.status !== ll.POOL_STATUS.trading : false;
  return {
    ...launch,
    pool,
    meta,
    image: meta?.image ?? null,
    description: meta?.description ?? "",
    priceSol: live?.price ?? null,
    mcapSol: live?.marketCap ?? null,
    mcapUsd: live && usdPerSol ? live.marketCap * usdPerSol : null,
    progress: pool ? ll.poolProgress(pool) : 0,
    raisedSol: pool ? Number(pool.realB) / 1e9 : 0,
    targetSol: pool ? Number(pool.totalFundRaisingB) / 1e9 : 85,
    graduated,
  };
}

// The biggest coin still flying on its curve, else the biggest overall.
export function supremeRocket(coins) {
  const priced = coins.filter((c) => c.mcapSol !== null);
  const flying = priced.filter((c) => !c.graduated);
  return [...(flying.length ? flying : priced)].sort((a, b) => b.mcapSol - a.mcapSol)[0] ?? null;
}

// ---- live data ------------------------------------------------------------------------------

const cacheKey = () => `kimlaunch:v1:${app.cfg.network}:${app.cfg.feeWallet.toBase58()}`;
let cursor = { newest: null, oldest: null };

function loadCache() {
  try {
    const raw = JSON.parse(localStorage.getItem(cacheKey()) || "null");
    if (raw?.launches) {
      app.launches = raw.launches;
      cursor = raw.cursor ?? cursor;
      app.done = Boolean(raw.done);
    }
    const metas = JSON.parse(localStorage.getItem("kimlaunch:meta:v1") || "{}");
    for (const l of app.launches) if (metas[l.uri]) app.metas.set(l.mint, metas[l.uri]);
  } catch {
    // A bad cache is just an empty one.
  }
}

function saveCache() {
  try {
    localStorage.setItem(cacheKey(), JSON.stringify({ launches: app.launches.slice(0, 400), cursor, done: app.done }));
    const metas = {};
    for (const l of app.launches.slice(0, 400)) {
      const m = app.metas.get(l.mint);
      if (m) metas[l.uri] = m;
    }
    localStorage.setItem("kimlaunch:meta:v1", JSON.stringify(metas));
  } catch {
    // Storage full or blocked: the board still works, it just reloads from chain.
  }
}

const indexOpts = () => ({ program: app.program, feeWallet: app.cfg.feeWallet, minFeeLamports: app.cfg.listMinFeeLamports });

function merge(launches) {
  const known = new Set(app.launches.map((l) => l.mint));
  const fresh = launches.filter((l) => !known.has(l.mint));
  if (!fresh.length) return [];
  app.launches = [...fresh, ...app.launches].sort((a, b) => (b.time ?? 0) - (a.time ?? 0) || b.slot - a.slot);
  return fresh;
}

// New launches since the last look. Pages back (up to 500 signatures) so a long absence leaves no gap.
async function refreshLaunches() {
  const found = [];
  let before;
  let newest = null;
  for (let i = 0; i < 5; i++) {
    const page = await fetchLaunches(app.connection, indexOpts(), { until: cursor.newest ?? undefined, before, limit: 100 });
    if (i === 0 && page.scanned.length) newest = page.scanned[0];
    if (!cursor.newest && i === 0) {
      cursor.oldest = page.oldest;
      app.done = page.done;
    }
    found.push(...page.launches);
    if (!cursor.newest || page.done || !page.oldest) break;
    before = page.oldest;
  }
  if (newest) cursor.newest = newest;
  return merge(found);
}

export async function loadOlder() {
  if (app.done || !cursor.oldest) return 0;
  const page = await fetchLaunches(app.connection, indexOpts(), { before: cursor.oldest, limit: 200 });
  cursor.oldest = page.oldest ?? cursor.oldest;
  app.done = page.done;
  const fresh = merge(page.launches);
  await refreshPools(fresh.map((l) => l.mint));
  loadMetas(fresh);
  saveCache();
  app.emit();
  return fresh.length;
}

async function refreshPools(mints = app.launches.map((l) => l.mint)) {
  if (!mints.length) return;
  const pools = await fetchPools(app.connection, app.program, mints);
  for (const [mint, pool] of pools) app.pools.set(mint, pool);
}

async function loadMetas(launches) {
  const todo = launches.filter((l) => !app.metas.has(l.mint));
  let i = 0;
  const worker = async () => {
    while (i < todo.length) {
      const l = todo[i++];
      const meta = await fetchCoinMeta(l.uri);
      if (meta) {
        app.metas.set(l.mint, meta);
        app.emit();
      }
    }
  };
  await Promise.all([worker(), worker(), worker(), worker()]);
  saveCache();
}

let refreshing = false;
async function refresh({ pools = true } = {}) {
  if (refreshing) return;
  refreshing = true;
  try {
    const fresh = await refreshLaunches();
    if (pools) await refreshPools();
    else if (fresh.length) await refreshPools(fresh.map((l) => l.mint));
    app.status = "live";
    app.error = null;
    saveCache();
    app.emit();
    loadMetas(app.launches);
    if (fresh.length && app.loadedOnce) toast(`$${fresh[0].symbol} just launched`, { kind: "good" });
    if (!app.loadedOnce) {
      app.loadedOnce = true;
      // A busy fee wallet can bury launches: dig a little deeper on first load.
      for (let i = 0; i < 4 && app.launches.length < 12 && !app.done; i++) await loadOlder();
    }
  } catch (e) {
    console.warn("live data", e);
    app.status = "error";
    app.error = e?.message ?? String(e);
    app.emit();
  } finally {
    refreshing = false;
  }
}

async function refreshPrice() {
  const v = await solUsd();
  if (v && v !== app.solUsd) {
    app.solUsd = v;
    app.emit();
  }
}

// After a launch from this browser: show the coin right away, before the indexer catches up.
export function addOwnLaunch(launch) {
  merge([launch]);
  saveCache();
  refreshPools([launch.mint]).then(() => app.emit());
  loadMetas([launch]);
  app.emit();
}

// ---- wallet --------------------------------------------------------------------------------

let offWalletChange = null;

export async function refreshBalance() {
  if (!app.publicKey) return;
  try {
    app.balance = await app.connection.getBalance(app.publicKey, "confirmed");
    app.emit();
  } catch {
    // keep the old balance
  }
}

async function useWallet(wallet, { silent = false } = {}) {
  await wallet.connect({ silent });
  offWalletChange?.();
  app.wallet = wallet;
  app.publicKey = wallet.publicKey;
  app.balance = null;
  offWalletChange = wallet.onChange((pk) => {
    app.publicKey = pk;
    app.balance = null;
    if (!pk) app.wallet = null;
    renderWalletButtons();
    refreshBalance();
    app.emit();
  });
  try {
    localStorage.setItem("kimlaunch:wallet", wallet.name);
  } catch {}
  renderWalletButtons();
  refreshBalance();
  app.emit();
}

export async function connectWallet() {
  if (app.publicKey) return app.publicKey;
  const wallets = availableWallets();
  if (wallets.length === 1) {
    await useWallet(wallets[0]);
    return app.publicKey;
  }
  return new Promise((resolve, reject) => {
    const modal = openModal(walletPicker(wallets), { onClose: () => resolve(app.publicKey) });
    on(modal.el, "click", "[data-wallet]", async (e, el) => {
      const w = wallets.find((x) => x.name === el.dataset.wallet);
      try {
        await useWallet(w);
        modal.close();
      } catch (err) {
        toast(err.message, { kind: "error" });
        reject(err);
      }
    });
  });
}

function walletPicker(wallets) {
  if (!wallets.length) {
    const mobile = isMobile();
    return html`
      <div class="modal-head"><h3>connect a wallet</h3><button class="icon-btn" data-close aria-label="close">${icon("close")}</button></div>
      <p class="muted">${mobile ? "open this page inside the Phantom app to launch and trade." : "you need a Solana wallet. Phantom takes a minute to install."}</p>
      <div class="stack">
        ${mobile
          ? html`<a class="btn btn-primary btn-block" href="${phantomBrowseLink()}">${icon("wallet")} open in Phantom</a>`
          : html`<a class="btn btn-primary btn-block" href="https://phantom.com/download" target="_blank" rel="noopener noreferrer">${icon("wallet")} install Phantom</a>`}
      </div>`;
  }
  return html`
    <div class="modal-head"><h3>connect a wallet</h3><button class="icon-btn" data-close aria-label="close">${icon("close")}</button></div>
    <div class="wallet-list">
      ${wallets.map(
        (w) => html`<button class="wallet-option" data-wallet="${w.name}">
          ${w.icon ? html`<img src="${w.icon}" alt="" width="32" height="32">` : html`<span class="wallet-fallback">${icon("wallet")}</span>`}
          <span>${w.name}</span>${/phantom/i.test(w.name) ? html`<em>recommended</em>` : ""}
        </button>`,
      )}
    </div>`;
}

export async function disconnectWallet() {
  offWalletChange?.();
  offWalletChange = null;
  await app.wallet?.disconnect();
  app.wallet = null;
  app.publicKey = null;
  app.balance = null;
  try {
    localStorage.removeItem("kimlaunch:wallet");
  } catch {}
  renderWalletButtons();
  app.emit();
}

async function reconnect() {
  let last = null;
  try {
    last = localStorage.getItem("kimlaunch:wallet");
  } catch {}
  if (!last) return;
  const w = availableWallets().find((x) => x.name === last);
  if (w) await useWallet(w, { silent: true }).catch(() => {});
}

function renderWalletButtons() {
  for (const el of $$("[data-wallet-button]")) {
    if (app.publicKey) {
      mount(
        el,
        html`<span class="wb-balance">${app.balance === null ? "…" : sol(app.balance / 1e9, 3)}</span><span class="wb-addr">${short(app.publicKey.toBase58())}</span>`,
      );
      el.classList.add("connected");
      el.setAttribute("aria-label", `wallet ${app.publicKey.toBase58()}`);
    } else {
      mount(el, html`${icon("wallet")}<span>connect wallet</span>`);
      el.classList.remove("connected");
      el.setAttribute("aria-label", "connect wallet");
    }
  }
}

function walletMenu() {
  const pk = app.publicKey.toBase58();
  const modal = openModal(html`
    <div class="modal-head"><h3>your wallet</h3><button class="icon-btn" data-close aria-label="close">${icon("close")}</button></div>
    <div class="wallet-card">
      <div class="mono">${pk}</div>
      <div class="big">${app.balance === null ? "…" : sol(app.balance / 1e9, 4)}</div>
      <div class="muted small">${app.wallet?.name ?? ""} · ${app.cfg.network}</div>
    </div>
    <div class="row gap">
      <button class="btn btn-dark grow" data-act="copy">${icon("copy")} copy address</button>
      <a class="btn btn-dark grow" href="${app.explorer("account", pk)}" target="_blank" rel="noopener noreferrer">${icon("external")} explorer</a>
    </div>
    <button class="btn btn-ghost btn-block" data-act="disconnect">${icon("logout")} disconnect</button>`);
  on(modal.el, "click", "[data-act]", async (e, el) => {
    if (el.dataset.act === "copy") copyText(pk);
    if (el.dataset.act === "disconnect") {
      await disconnectWallet();
      modal.close();
    }
  });
}

// ---- chrome: ticker, search, idea modal ------------------------------------------------------

function renderTicker() {
  const el = $("#ticker");
  if (!el) return;
  const coins = app.coins();
  const pills = [];
  const newest = coins[0];
  if (newest) {
    pills.push(html`<a class="pill" href="#/coin/${newest.mint}">
      ${newest.image ? html`<img src="${newest.image}" alt="" loading="lazy">` : html`<span class="pill-dot"></span>`}
      ${icon("sparkles", { size: 15 })}<b>$${newest.symbol}</b> just launched <span class="mono muted">${ago(newest.time)}</span></a>`);
  }
  const top = supremeRocket(coins);
  if (top && top !== newest) {
    pills.push(html`<a class="pill pill-red" href="#/coin/${top.mint}">${icon("crown", { size: 15 })}<b>$${top.symbol}</b> leads the pad</a>`);
  }
  if (!pills.length) pills.push(html`<span class="pill">${icon("rocket", { size: 15 })} the pad is open. launch the first rocket.</span>`);
  mount(el, pills);
}

export function openIdeas(onPick) {
  let seed = Date.now();
  const view = () => html`
    <div class="modal-head"><h3>three rockets, by order of the leader</h3><button class="icon-btn" data-close aria-label="close">${icon("close")}</button></div>
    <div class="ideas">
      ${pitchThree(seed).map(
        (idea, i) => html`<button class="idea" data-idea="${i}">
          <span class="idea-name">${idea.name}</span><span class="idea-sym">$${idea.symbol}</span>
          <span class="idea-line">${idea.description}</span>
        </button>`,
      )}
    </div>
    <button class="btn btn-dark btn-block" data-again>${icon("shuffle")} pitch me three more</button>`;
  const modal = openModal(view());
  on(modal.el, "click", "[data-again]", () => {
    seed += 7919;
    modal.update(view());
  });
  on(modal.el, "click", "[data-idea]", (e, el) => {
    const idea = pitchThree(seed)[Number(el.dataset.idea)];
    modal.close();
    onPick(idea);
  });
}

// ---- router ----------------------------------------------------------------------------------

let cleanup = null;
const view = () => $("#view");

function route() {
  const hash = location.hash.replace(/^#\/?/, "");
  const [page, arg] = hash.split("/");
  cleanup?.();
  cleanup = null;
  window.scrollTo(0, 0);
  let name = page || "board";
  if (name === "coin" && arg) cleanup = renderCoin(view(), app, arg);
  else if (name === "create") cleanup = renderCreate(view(), app);
  else if (name === "how") cleanup = renderHow(view(), app);
  else if (name === "live") cleanup = renderBoard(view(), app, { tab: "new", live: true });
  else {
    name = "board";
    cleanup = renderBoard(view(), app, { search: app.search });
  }
  for (const a of $$("[data-nav]")) a.classList.toggle("active", a.dataset.nav === name);
  document.body.dataset.page = name;
}

export function navigate(path) {
  if (location.hash === `#${path}`) route();
  else location.hash = path;
}

// ---- start -----------------------------------------------------------------------------------

function start() {
  try {
    app.cfg = readConfig(window.KIMLAUNCH_CONFIG || {});
  } catch (e) {
    mount(view(), html`<div class="panel error-panel">${icon("alert")} ${e.message}</div>`);
    return;
  }
  app.connection = new Connection(app.cfg.rpcEndpoints[0], {
    commitment: "confirmed",
    fetch: failoverFetch(app.cfg.rpcEndpoints),
  });
  app.program = ll.NETWORKS[app.cfg.network].program;
  document.documentElement.dataset.network = app.cfg.network;
  for (const el of $$("[data-site-name]")) el.textContent = app.cfg.name;
  if (app.cfg.network === "devnet") $("#netbar")?.removeAttribute("hidden");
  for (const el of $$("[data-link-x]")) app.cfg.links.x ? (el.href = app.cfg.links.x) : el.remove();
  for (const el of $$("[data-link-telegram]")) app.cfg.links.telegram ? (el.href = app.cfg.links.telegram) : el.remove();

  loadCache();
  renderWalletButtons();
  renderTicker();
  app.subscribe(renderTicker);
  app.subscribe(renderWalletButtons);

  on(document.body, "click", "[data-wallet-button]", () => {
    if (app.publicKey) walletMenu();
    else connectWallet().catch((e) => toast(e.message, { kind: "error" }));
  });
  on(document.body, "click", "[data-ideas]", () =>
    openIdeas((idea) => {
      app.pendingIdea = idea;
      navigate("/create");
    }),
  );
  const search = $("#search");
  search?.addEventListener("input", () => {
    app.search = search.value;
    if (!["board", "live"].includes(document.body.dataset.page)) navigate("/");
    else app.emit();
  });

  window.addEventListener("hashchange", route);
  route();

  refresh();
  refreshPrice();
  setInterval(() => !document.hidden && refresh(), 30_000);
  setInterval(() => !document.hidden && refreshPrice(), 60_000);
  setInterval(() => !document.hidden && refreshBalance(), 30_000);
  setInterval(renderTicker, 30_000);
  document.addEventListener("visibilitychange", () => !document.hidden && refresh());
  onWalletsChanged(() => {
    if (!app.publicKey) reconnect();
  });
  reconnect();
}

start();

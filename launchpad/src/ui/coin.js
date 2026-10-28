// One coin: its curve, its numbers, and a buy/sell box while it is still on the curve.
import { PublicKey } from "@solana/web3.js";
import * as ll from "../chain/launchlab.js";
import { fetchCoinMeta } from "../chain/indexer.js";
import { loadCoin, tokenBalance, trade } from "../chain/flow.js";
import { $, ago, compact, copyText, html, icon, mount, on, pct, price, short, sol, toast } from "./dom.js";
import { coinImage, curveSpark, mcap, progressBar, socials } from "./parts.js";
import { coinView, connectWallet, refreshBalance } from "../main.js";

const BUY_PRESETS = [0.1, 0.5, 1, 2];
const SELL_PRESETS = [0.25, 0.5, 1];

function links(app, mint, graduated) {
  const list = [];
  if (app.cfg.network === "mainnet") {
    list.push(["Raydium", graduated ? `https://raydium.io/swap/?inputMint=sol&outputMint=${mint}` : `https://raydium.io/launchpad/token/?mint=${mint}`]);
    list.push(["DexScreener", `https://dexscreener.com/solana/${mint}`]);
    list.push(["Jupiter", `https://jup.ag/swap/SOL-${mint}`]);
  }
  list.push(["Solscan", app.explorer("token", mint)]);
  return list;
}

export function renderCoin(root, app, mintParam) {
  let mint;
  try {
    mint = new PublicKey(mintParam).toBase58();
  } catch {
    mount(root, html`<div class="empty"><h3>that's not a coin address</h3><a class="btn btn-dark" href="#/">back to the pad</a></div>`);
    return () => {};
  }

  const state = { coin: null, loaded: false, meta: null, side: "buy", tokens: 0n, busy: false, error: null };
  mount(
    root,
    html`<section class="coin-page">
      <a class="back" href="#/">${icon("left", { size: 15 })} back to the pad</a>
      <div id="coin"><div class="panel skeleton tall"></div></div>
    </section>`,
  );

  const launch = () => app.launches.find((l) => l.mint === mint);

  function view() {
    const l = launch();
    const onchain = state.coin?.onchain;
    const base = {
      mint,
      name: l?.name ?? onchain?.name ?? "unknown coin",
      symbol: l?.symbol ?? onchain?.symbol ?? "???",
      uri: l?.uri ?? onchain?.uri ?? "",
      creator: l?.creator ?? state.coin?.pool?.creator?.toBase58() ?? "",
      time: l?.time ?? null,
    };
    return coinView(base, state.coin?.pool ?? app.pools.get(mint), state.meta ?? app.metas.get(mint), app.solUsd);
  }

  function quoteText(c) {
    const ctx = state.coin?.ctx;
    if (!ctx || !c.pool) return "";
    const v = Number($("#amount", root)?.value.replace(",", ".") || 0);
    if (!Number.isFinite(v) || v <= 0) return "";
    try {
      if (state.side === "buy") {
        const q = ll.quoteBuy(c.pool, BigInt(Math.round(v * 1e9)), ctx.fees);
        return html`you get ≈ <b>${compact(Number(q.amountA) / 1e6)} $${c.symbol}</b>${q.amountB < BigInt(Math.round(v * 1e9)) ? html` (caps the curve at ${sol(Number(q.amountB) / 1e9)})` : ""}`;
      }
      const q = ll.quoteSell(c.pool, BigInt(Math.round(v * 1e6)), ctx.fees);
      return html`you get ≈ <b>${sol(Number(q.amountB) / 1e9, 4)}</b>`;
    } catch {
      return "";
    }
  }

  function tradeBox(c) {
    if (!state.loaded) return html`<div class="panel trade skeleton"></div>`;
    if (!c.pool) {
      return html`<div class="panel trade"><h3>not on a LaunchLab curve</h3><p class="muted">this coin isn't trading on Raydium LaunchLab, so it can't be traded here.</p></div>`;
    }
    if (c.graduated) {
      return html`<div class="panel trade orbit-box">
        <span class="orbit-icon">${icon("orbit", { size: 28 })}</span>
        <h3>this rocket reached orbit</h3>
        <p class="muted">its curve filled and the liquidity moved to a Raydium pool. trade it there.</p>
        ${links(app, mint, true)
          .slice(0, 3)
          .map(([label, url]) => html`<a class="btn btn-dark btn-block" href="${url}" target="_blank" rel="noopener noreferrer">${label} ${icon("external", { size: 14 })}</a>`)}
      </div>`;
    }
    const buying = state.side === "buy";
    const presets = buying ? BUY_PRESETS : SELL_PRESETS;
    return html`<div class="panel trade">
      <div class="seg">
        <button class="seg-btn ${buying ? "on buy" : ""}" data-side="buy">buy</button>
        <button class="seg-btn ${!buying ? "on sell" : ""}" data-side="sell">sell</button>
      </div>
      <div class="field">
        <label for="amount">${buying ? "you pay" : "you sell"}</label>
        <div class="input-suffix"><input class="input mono big" id="amount" inputmode="decimal" placeholder="0.0"><span>${buying ? "SOL" : `$${c.symbol}`}</span></div>
      </div>
      <div class="presets">
        ${presets.map((p) => html`<button class="preset" data-amount="${p}">${buying ? `${p} SOL` : p === 1 ? "max" : pct(p)}</button>`)}
      </div>
      <p class="quote" id="quote">${quoteText(c)}</p>
      <button class="btn ${buying ? "btn-green" : "btn-primary"} btn-lg btn-block" id="go" ${state.busy ? "disabled" : ""}>
        ${state.busy ? html`<span class="spin"></span> ${state.busy}` : app.publicKey ? (buying ? html`buy $${c.symbol}` : html`sell $${c.symbol}`) : html`${icon("wallet")} connect wallet`}
      </button>
      <div class="trade-foot muted small">
        <span>balance: ${buying ? (app.balance === null ? "—" : sol(app.balance / 1e9, 4)) : `${compact(Number(state.tokens) / 1e6)} $${c.symbol}`}</span>
        <span>slippage 5%</span>
      </div>
    </div>`;
  }

  function draw({ force = true } = {}) {
    // Don't yank the amount box from under someone typing in it.
    if (!force && document.activeElement?.id === "amount") return;
    const c = view();
    const el = $("#coin", root);
    if (!el) return;
    if (app.isHidden(c)) {
      mount(el, html`<div class="empty"><h3>this coin was taken off the pad</h3><p class="muted">the site owner hid it from this launchpad.</p><a class="btn btn-dark" href="#/">back to the pad</a></div>`);
      return;
    }
    if (state.loaded && !state.coin?.pool && !state.coin?.onchain && !launch()) {
      mount(el, html`<div class="empty"><h3>no coin found at ${short(mint, 6)}</h3><p class="muted">${state.error ?? "it may not have landed yet. give it a few seconds."}</p></div>`);
      return;
    }
    const keepAmount = $("#amount", root)?.value ?? "";
    const left = c.pool ? Math.max(0, c.targetSol - c.raisedSol) : null;
    mount(
      el,
      html`<div class="coin-layout">
          <div class="panel coin-hero">
            ${coinImage(c, "coin-pic")}
            <div class="coin-title">
              <div class="coin-meta muted small">
                ${c.creator ? html`<span>${icon("rocket", { size: 13 })} by <a href="${app.explorer("account", c.creator)}" target="_blank" rel="noopener noreferrer">${short(c.creator)}</a></span>` : ""}
                ${c.time ? html`<span>· ${ago(c.time)}</span>` : ""}
                ${c.graduated ? html`<span class="tag tag-gold">${icon("orbit", { size: 12 })} in orbit</span>` : c.pool ? html`<span class="tag">on the curve</span>` : ""}
              </div>
              <h1>${c.name} <span class="ticker">$${c.symbol}</span></h1>
              <button class="ca" data-copy="${mint}"><span class="mono">${mint}</span>${icon("copy", { size: 15 })}</button>
              ${socials(c)}
            </div>
            ${c.description ? html`<p class="coin-desc">${c.description}</p>` : ""}
          </div>
          <div class="panel coin-stats">
            <div class="kpis">
              <div class="kpi"><span>market cap</span><b class="mono">${mcap(c)}</b></div>
              <div class="kpi"><span>price</span><b class="mono">${c.priceSol ? `${price(c.priceSol)} SOL` : "—"}</b></div>
              <div class="kpi"><span>in the tank</span><b class="mono">${compact(c.raisedSol)} / ${compact(c.targetSol, { digits: 0 })} SOL</b></div>
            </div>
            ${progressBar(c)}
            <p class="muted small">${c.graduated
              ? "the curve is full: liquidity moved to Raydium."
              : c.pool
                ? html`${sol(left, 2)} more and it graduates to Raydium.`
                : ""}</p>
            ${c.pool ? html`<div class="curve-big">${curveSpark(c.pool, { w: 640, h: 170 })}<div class="curve-axis muted small"><span>0 SOL</span><span>bonding curve</span><span>${compact(c.targetSol, { digits: 0 })} SOL</span></div></div>` : ""}
          </div>
          <div class="panel coin-links">
            <div class="panel-kicker">${icon("external", { size: 14 })} also on</div>
            <div class="row gap wrap">
              ${links(app, mint, c.graduated).map(([label, url]) => html`<a class="btn btn-dark btn-sm" href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`)}
            </div>
          </div>
        <aside class="coin-side">${tradeBox(c)}</aside>
      </div>`,
    );
    const amount = $("#amount", root);
    if (amount && keepAmount) amount.value = keepAmount;
  }

  async function load() {
    try {
      state.coin = await loadCoin(app.connection, app.cfg.network, mint);
      if (state.coin.pool) app.pools.set(mint, state.coin.pool);
      const uri = launch()?.uri ?? state.coin.onchain?.uri;
      if (uri && !state.meta) state.meta = app.metas.get(mint) ?? (await fetchCoinMeta(uri));
      if (app.publicKey) state.tokens = await tokenBalance(app.connection, app.publicKey, mint);
      state.error = null;
    } catch (e) {
      state.error = e?.message ?? String(e);
    }
    state.loaded = true;
    draw({ force: false });
  }

  on(root, "click", "[data-copy]", (e, b) => copyText(b.dataset.copy));
  on(root, "click", "[data-side]", (e, b) => {
    state.side = b.dataset.side;
    draw();
  });
  on(root, "input", "#amount", (e) => {
    const clean = e.target.value.replace(",", ".").replace(/[^0-9.]/g, "");
    if (clean !== e.target.value) e.target.value = clean;
    mount($("#quote", root), quoteText(view()));
  });
  on(root, "click", "[data-amount]", (e, b) => {
    const v = Number(b.dataset.amount);
    $("#amount", root).value = state.side === "buy" ? String(v) : String(Number(state.tokens) / 1e6 * v);
    mount($("#quote", root), quoteText(view()));
  });
  on(root, "click", "#go", async () => {
    if (state.busy) return;
    if (!app.publicKey) {
      await connectWallet().catch((e) => toast(e.message, { kind: "error" }));
      if (app.publicKey) state.tokens = await tokenBalance(app.connection, app.publicKey, mint);
      draw();
      return;
    }
    const c = view();
    const v = Number($("#amount", root).value || 0);
    if (!(v > 0)) {
      toast("enter an amount", { kind: "error" });
      return;
    }
    const amount = state.side === "buy" ? BigInt(Math.round(v * 1e9)) : BigInt(Math.floor(v * 1e6));
    const sellAll = state.side === "sell" && amount >= state.tokens;
    try {
      await trade({
        connection: app.connection,
        wallet: app.wallet,
        network: app.cfg.network,
        mint,
        coin: { pool: c.pool, ctx: state.coin.ctx },
        side: state.side,
        amount: sellAll ? state.tokens : amount,
        microLamports: app.cfg.microLamports,
        onStep: (s) => {
          state.busy = s === "sign" ? "approve in your wallet" : s === "send" ? "confirming" : "preparing";
          draw();
        },
      });
      toast(state.side === "buy" ? `bought $${c.symbol}` : `sold $${c.symbol}`, { kind: "good" });
      $("#amount", root).value = "";
    } catch (e) {
      toast(e.message, { kind: "error", ms: 7000 });
    } finally {
      state.busy = false;
      await load();
      refreshBalance();
    }
  });

  draw();
  load();
  const timer = setInterval(() => !document.hidden && !state.busy && load(), 15_000);
  const off = app.subscribe(() => draw({ force: false }));
  return () => {
    clearInterval(timer);
    off();
  };
}

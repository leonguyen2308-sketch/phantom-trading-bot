// "build your own rocket here": the create-coin form and the launch flow.
import * as ll from "../chain/launchlab.js";
import { LIMITS, loadLaunchContext } from "../chain/launch.js";
import { launchCoin, uploadCoinFiles } from "../chain/flow.js";
import { $, compact, copyText, html, icon, lamportsToSol, mount, on, openModal, pct, short, sol, toast } from "./dom.js";
import { coinCard } from "./parts.js";
import { prepareImage, makeBadge } from "./image.js";
import { addOwnLaunch, app as appState, connectWallet, openIdeas, refreshBalance } from "../main.js";

// Rent for the mint, pool, two vaults and metadata, plus the creator's token account on a first buy.
const RENT_SOL = 0.0154;
const ATA_RENT_SOL = 0.00204;
const NETWORK_FEE_SOL = 0.0002;
const DEV_PRESETS = [0, 0.1, 0.5, 1, 2];

const bytes = (s) => new TextEncoder().encode(s).length;

function steps(devBuy, split) {
  const list = [
    ["image", "painting the rocket", "storing your picture on Arweave"],
    ["metadata", "writing the flight plan", "storing name, ticker and socials"],
    ["build", "checking the launch pad", "building and test-running the launch"],
    ["sign", "waiting for your signature", "approve it in your wallet"],
    ["launch", "launching", "the network is confirming"],
  ];
  if (devBuy && split) list.push(["buy", "fueling", "your first buy, right after liftoff"]);
  return list;
}

function stepList(current, { devBuy, split, failed, notes = {} }) {
  const all = steps(devBuy, split);
  const at = all.findIndex(([id]) => id === current);
  return html`<ol class="steps">
    ${all.map(([id, title, sub], i) => {
      const state = failed && i === at ? "error" : i < at || current === "done" ? "done" : i === at ? "active" : "todo";
      return html`<li class="step ${state}">
        <span class="step-dot">${state === "done" ? icon("check", { size: 14 }) : state === "error" ? icon("alert", { size: 14 }) : state === "active" ? html`<span class="spin"></span>` : i + 1}</span>
        <span class="step-text"><b>${title}</b><span>${notes[id] ?? sub}</span></span>
      </li>`;
    })}
  </ol>`;
}

export function renderCreate(root, app) {
  const form = {
    image: null, // { blob, type, width, url }
    uploads: null, // { key, imageUrl, uri }
    ctx: null,
  };

  mount(
    root,
    html`<section class="create-page">
      <header class="page-head">
        <span class="kicker">${icon("rocket", { size: 15 })} create coin</span>
        <h1>build your own <span class="hl">rocket</span> here.</h1>
        <p class="lede">one transaction does it all: your coin, its bonding curve on Raydium LaunchLab, and your first buy if you want one.</p>
      </header>
      <div class="create">
        <form class="panel form" id="form" novalidate autocomplete="off">
          <div class="form-top">
            <div class="drop-wrap">
              <label class="drop" id="drop" tabindex="0">
                <input type="file" id="file" accept="image/png,image/jpeg,image/webp,image/gif" hidden>
                <span class="drop-empty">${icon("image", { size: 30 })}<b>drop the paint job</b><span>png, jpg, webp or gif. we square it and squeeze it under 100 KB.</span></span>
                <img id="drop-img" alt="your coin picture" hidden>
              </label>
              <button type="button" class="btn btn-ghost btn-sm" data-badge>${icon("star", { size: 15 })} no picture? make a badge</button>
            </div>
            <div class="fields">
              <div class="field">
                <label for="name">name <span class="count" id="name-count">0/${LIMITS.name}</span></label>
                <input class="input" id="name" maxlength="40" placeholder="Supreme Rocket" required>
              </div>
              <div class="field">
                <label for="symbol">ticker <span class="count" id="symbol-count">0/${LIMITS.symbol}</span></label>
                <div class="input-prefix"><span>$</span><input class="input" id="symbol" maxlength="10" placeholder="ROCKET" required></div>
              </div>
              <p class="taken" id="taken" role="alert"></p>
              <button type="button" class="btn btn-dark btn-sm" data-ideas-inline>${icon("sparkles", { size: 15 })} ask the leader for ideas</button>
            </div>
          </div>
          <div class="field">
            <label for="description">description <span class="muted">(optional)</span></label>
            <textarea class="input" id="description" rows="3" maxlength="500" placeholder="what is this rocket for? one line is plenty."></textarea>
          </div>
          <details class="box">
            <summary>${icon("globe", { size: 16 })} socials <span class="muted">(optional)</span>${icon("down", { size: 16, cls: "chev" })}</summary>
            <div class="grid3">
              <div class="field"><label for="twitter">X</label><input class="input" id="twitter" placeholder="https://x.com/yourcoin"></div>
              <div class="field"><label for="telegram">telegram</label><input class="input" id="telegram" placeholder="https://t.me/yourcoin"></div>
              <div class="field"><label for="website">website</label><input class="input" id="website" placeholder="https://yourcoin.com"></div>
            </div>
          </details>
          <div class="field">
            <label for="devbuy">fuel it yourself <span class="muted">(optional first buy, in SOL)</span></label>
            <div class="devbuy">
              <div class="input-suffix"><input class="input mono" id="devbuy" inputmode="decimal" placeholder="0" value=""><span>SOL</span></div>
              <div class="presets">${DEV_PRESETS.map((v) => html`<button type="button" class="preset" data-preset="${v}">${v === 0 ? "none" : v}</button>`)}</div>
            </div>
            <p class="hint" id="devbuy-hint">buying first, in the same transaction as the launch, means no bot can buy before you.</p>
          </div>
          <details class="box">
            <summary>${icon("info", { size: 16 })} advanced${icon("down", { size: 16, cls: "chev" })}</summary>
            <div class="grid3">
              <div class="field"><label for="slippage">first-buy slippage</label>
                <div class="input-suffix"><input class="input mono" id="slippage" inputmode="decimal" value="5"><span>%</span></div></div>
              <div class="field"><label for="priority">network priority</label>
                <select class="input" id="priority">
                  <option value="50000">normal</option><option value="200000" selected>fast</option><option value="1000000">turbo</option>
                </select></div>
              <div class="field"><label>curve</label><div class="readonly mono" id="curve-info">1B supply · 85 SOL to graduate</div></div>
            </div>
          </details>
        </form>
        <aside class="create-side">
          <div class="panel preview">
            <div class="panel-kicker">${icon("sparkles", { size: 14 })} how it lands on the board</div>
            <div id="preview"></div>
          </div>
          <div class="panel costs">
            <div class="cost-rows" id="costs"></div>
            <button class="btn btn-primary btn-lg btn-block" id="launch" type="button">${icon("rocket", { size: 20 })} launch rocket</button>
            <p class="fine">your wallet shows the full transaction before you approve.
              the ${app.cfg.feeSol} SOL launch fee goes to <span class="mono">${short(app.cfg.feeWallet.toBase58())}</span>, in the same transaction:
              if the launch fails, nothing is charged. name, ticker and picture can't be changed later.</p>
          </div>
        </aside>
      </div>
    </section>`,
  );

  const el = (id) => $(`#${id}`, root);
  const field = (id) => el(id).value.trim();

  function devBuySol() {
    const v = Number(field("devbuy").replace(",", ".") || 0);
    return Number.isFinite(v) && v > 0 ? v : 0;
  }
  function slippageBps() {
    const v = Number(field("slippage").replace(",", ".") || 5);
    return Math.round(Math.min(50, Math.max(0.5, Number.isFinite(v) ? v : 5)) * 100);
  }

  function quote() {
    const dev = devBuySol();
    if (!dev || !form.ctx) return null;
    try {
      return ll.quoteBuy(ll.newPoolState(form.ctx.config), BigInt(Math.round(dev * 1e9)), form.ctx.fees);
    } catch {
      return null;
    }
  }

  function draft() {
    return {
      mint: "preview",
      creator: app.publicKey?.toBase58() ?? "you",
      time: Math.floor(Date.now() / 1000),
      name: field("name") || "your rocket",
      symbol: (field("symbol") || "TICKER").toUpperCase(),
      image: form.image?.url ?? null,
      description: field("description"),
      meta: null,
      pool: form.ctx ? { ...ll.newPoolState(form.ctx.config), status: 0 } : null,
      mcapSol: form.ctx ? ll.poolPrice(ll.newPoolState(form.ctx.config)).marketCap : null,
      mcapUsd: form.ctx && app.solUsd ? ll.poolPrice(ll.newPoolState(form.ctx.config)).marketCap * app.solUsd : null,
      progress: 0,
      raisedSol: 0,
      targetSol: 85,
      graduated: false,
    };
  }

  function update() {
    const name = field("name");
    const symbol = field("symbol");
    const copy = app.taken({ name, symbol });
    mount(
      el("taken"),
      copy
        ? html`${icon("alert", { size: 14 })} <span>${copy.symbol.toLowerCase() === symbol.toLowerCase() ? html`$${copy.symbol}` : copy.name} is already on the pad. one ticker, one coin: pick another.</span>`
        : "",
    );
    el("name-count").textContent = `${bytes(name)}/${LIMITS.name}`;
    el("name-count").classList.toggle("over", bytes(name) > LIMITS.name);
    el("symbol-count").textContent = `${symbol.length}/${LIMITS.symbol}`;

    const coin = draft();
    const q = quote();
    if (q) {
      coin.pool.realA = q.amountA;
      coin.pool.realB = q.amountB - (q.amountB * (form.ctx.fees.protocol + form.ctx.fees.platform + form.ctx.fees.creator)) / ll.FEE_DENOMINATOR;
      coin.progress = ll.poolProgress(coin.pool);
      coin.raisedSol = Number(coin.pool.realB) / 1e9;
      const live = ll.poolPrice(coin.pool);
      coin.mcapSol = live.marketCap;
      coin.mcapUsd = app.solUsd ? live.marketCap * app.solUsd : null;
    }
    mount(el("preview"), coinCard(coin));

    const dev = devBuySol();
    const share = q ? Number(q.amountA) / Number(ll.DEFAULT_CURVE.supply) : 0;
    mount(
      el("devbuy-hint"),
      dev
        ? q
          ? html`you get about <b>${compact(Number(q.amountA) / 1e6)} $${symbol.toUpperCase() || "TICKER"}</b> (${pct(share, 1)} of supply)${share > 0.1 ? html` — <span class="warn">that's a big bag; buyers notice.</span>` : ""}`
          : "loading the curve…"
        : "buying first, in the same transaction as the launch, means no bot can buy before you.",
    );
    for (const b of root.querySelectorAll("[data-preset]")) b.classList.toggle("on", Number(b.dataset.preset) === dev);

    // Launching from the fee wallet itself: the fee goes out and straight back.
    const ownLaunch = Boolean(app.publicKey?.equals(app.cfg.feeWallet));
    const total = (ownLaunch ? 0 : app.cfg.feeSol) + RENT_SOL + NETWORK_FEE_SOL + (dev ? dev + ATA_RENT_SOL : 0);
    const lowBalance = app.balance !== null && app.publicKey && app.balance / 1e9 < total;
    mount(
      el("costs"),
      html`<div class="cost"><span>launch fee${ownLaunch ? html` <em class="muted">(comes back to you: you are the fee wallet)</em>` : ""}</span><b class="mono">${ownLaunch ? html`<s>${app.cfg.feeSol} SOL</s>` : `${app.cfg.feeSol} SOL`}</b></div>
        <div class="cost"><span>Solana rent for the coin's accounts <em class="muted">(not refundable)</em></span><b class="mono">≈ ${(RENT_SOL + (dev ? ATA_RENT_SOL : 0)).toFixed(4)} SOL</b></div>
        ${dev ? html`<div class="cost"><span>your first buy</span><b class="mono">${dev} SOL</b></div>` : ""}
        <div class="cost"><span>network fee</span><b class="mono">≈ ${NETWORK_FEE_SOL} SOL</b></div>
        <div class="cost total"><span>total</span><b class="mono">≈ ${Number(total.toFixed(4))} SOL</b></div>
        ${lowBalance ? html`<div class="cost warn-row">${icon("alert", { size: 14 })} you have ${sol(app.balance / 1e9, 4)}. top up first.</div>` : ""}`,
    );
    const launch = el("launch");
    mount(
      launch,
      app.publicKey
        ? html`${icon("rocket", { size: 20 })} launch rocket`
        : html`${icon("wallet", { size: 20 })} connect wallet to launch`,
    );
  }

  async function setImage(file) {
    try {
      const prepared = await prepareImage(file);
      if (form.image?.url) URL.revokeObjectURL(form.image.url);
      form.image = { ...prepared, url: URL.createObjectURL(prepared.blob) };
      form.uploads = null;
      const img = el("drop-img");
      img.src = form.image.url;
      img.hidden = false;
      el("drop").classList.add("has-image");
      update();
    } catch (e) {
      toast(e.message, { kind: "error" });
    }
  }

  // ---- inputs ----
  el("file").addEventListener("change", (e) => e.target.files?.[0] && setImage(e.target.files[0]));
  const drop = el("drop");
  drop.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      el("file").click();
    }
  });
  for (const ev of ["dragenter", "dragover"])
    drop.addEventListener(ev, (e) => {
      e.preventDefault();
      drop.classList.add("over");
    });
  for (const ev of ["dragleave", "drop"]) drop.addEventListener(ev, () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    const file = e.dataTransfer?.files?.[0];
    if (file) setImage(file);
  });
  const onPaste = (e) => {
    const file = [...(e.clipboardData?.files ?? [])].find((f) => f.type.startsWith("image/"));
    if (file) setImage(file);
  };
  document.addEventListener("paste", onPaste);

  el("symbol").addEventListener("input", (e) => {
    const clean = e.target.value.toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, LIMITS.symbol);
    if (clean !== e.target.value) e.target.value = clean;
  });
  el("devbuy").addEventListener("input", (e) => {
    const clean = e.target.value.replace(",", ".").replace(/[^0-9.]/g, "");
    if (clean !== e.target.value) e.target.value = clean;
  });
  el("form").addEventListener("input", () => {
    form.uploads = null;
    update();
  });
  el("form").addEventListener("submit", (e) => e.preventDefault());
  on(root, "click", "[data-preset]", (e, b) => {
    el("devbuy").value = Number(b.dataset.preset) ? b.dataset.preset : "";
    update();
  });
  on(root, "click", "[data-badge]", async () => {
    const symbol = field("symbol") || field("name").split(/\s+/)[0] || "KIM";
    setImage(await makeBadge(symbol, Math.random()));
  });
  const fillIdea = (idea) => {
    el("name").value = idea.name;
    el("symbol").value = idea.symbol;
    el("description").value = idea.description;
    form.uploads = null;
    update();
  };
  on(root, "click", "[data-ideas-inline]", () => openIdeas(fillIdea));
  if (appState.pendingIdea) {
    fillIdea(appState.pendingIdea);
    appState.pendingIdea = null;
  }

  el("launch").addEventListener("click", () => startLaunch());

  // ---- launch ----
  async function startLaunch() {
    if (!app.publicKey) {
      try {
        await connectWallet();
      } catch (e) {
        toast(e.message, { kind: "error" });
      }
      update();
      return;
    }
    const name = field("name");
    const symbol = field("symbol").toUpperCase();
    const problems = [];
    if (!form.image) problems.push("add a picture (or make a badge)");
    if (!name) problems.push("give it a name");
    if (bytes(name) > LIMITS.name) problems.push(`shorten the name to ${LIMITS.name} bytes`);
    if (!symbol) problems.push("give it a ticker");
    const copy = app.taken({ name, symbol });
    if (copy) problems.push(`$${copy.symbol} / ${copy.name} is already on the pad`);
    if (problems.length) {
      toast(problems.join(" · "), { kind: "error" });
      return;
    }
    const coin = {
      name,
      symbol,
      description: field("description"),
      twitter: field("twitter"),
      telegram: field("telegram"),
      website: field("website"),
    };
    const dev = devBuySol();
    const devBuyLamports = BigInt(Math.round(dev * 1e9));
    const key = JSON.stringify([coin, form.image.blob.size, form.image.url]);
    const notes = {};
    let split = false;
    let current = "image";
    let sentSignature = null;

    const modal = openModal(html``, { dismissable: false });
    const render = (failed, error) => {
      modal.update(html`
        <div class="modal-head"><h3>${failed ? "the launch stopped" : html`launching $${symbol}`}</h3></div>
        ${stepList(current, { devBuy: dev > 0, split, failed, notes })}
        ${failed
          ? html`<div class="alert">${icon("alert", { size: 16 })}<span>${error}</span></div>
              <div class="row gap"><button class="btn btn-primary grow" data-retry>${icon("rocket")} try again</button><button class="btn btn-dark" data-close>close</button></div>`
          : sentSignature
            ? html`<p class="muted small">transaction <a href="${app.explorer("tx", sentSignature)}" target="_blank" rel="noopener noreferrer">${short(sentSignature, 6)}</a></p>`
            : ""}`);
      if (failed) {
        modal.dismissable = true;
        modal.el.querySelector("[data-retry]")?.addEventListener("click", () => {
          modal.close();
          startLaunch();
        });
      }
    };
    const onStep = (step, info) => {
      current = step;
      if (step === "sign") {
        split = info?.transactions > 1;
        notes.sign = info?.cost
          ? `approve in ${app.wallet?.name ?? "your wallet"}: about ${lamportsToSol(info.cost).toFixed(4)} SOL in total`
          : `approve it in ${app.wallet?.name ?? "your wallet"}`;
      }
      render(false);
    };
    render(false);

    try {
      if (!form.uploads || form.uploads.key !== key) {
        const files = await uploadCoinFiles({ image: form.image, coin, site: app.cfg }, onStep);
        form.uploads = { key, ...files };
      }
      const result = await launchCoin({
        connection: app.connection,
        wallet: app.wallet,
        cfg: app.cfg,
        coin,
        uri: form.uploads.uri,
        devBuyLamports,
        slippageBps: slippageBps(),
        microLamports: Number(el("priority").value) || app.cfg.microLamports,
        onStep,
        onSent: (sig) => {
          sentSignature = sig;
          render(false);
        },
      });
      current = "done";
      addOwnLaunch({
        signature: result.createSignature,
        slot: 0,
        time: Math.floor(Date.now() / 1000),
        mint: result.mint,
        creator: app.publicKey.toBase58(),
        payer: app.publicKey.toBase58(),
        name,
        symbol,
        uri: form.uploads.uri,
      });
      refreshBalance();
      success(modal, { ...result, name, symbol, imageUrl: form.uploads.imageUrl });
    } catch (e) {
      console.error(e);
      if (e?.unknown && e.signature) {
        render(true, html`${e.message} <a href="${app.explorer("tx", e.signature)}" target="_blank" rel="noopener noreferrer">check the explorer</a>`);
      } else {
        render(true, e?.message ?? String(e));
      }
    }
  }

  function success(modal, r) {
    const share = `I just built my own rocket: $${r.symbol} 🚀 ${app.cfg.url}/#/coin/${r.mint}`;
    modal.dismissable = true;
    modal.update(html`
      <div class="liftoff">
        <img class="liftoff-pic" src="${r.imageUrl}" alt="" width="96" height="96">
        <h3>liftoff. <span class="hl">$${r.symbol}</span> is live.</h3>
        <p class="muted">it's trading on its bonding curve right now. ${r.quote && !r.buyError ? html`your first buy landed: ${compact(Number(r.quote.amountA) / 1e6)} $${r.symbol}.` : ""}</p>
        ${r.buyError ? html`<div class="alert">${icon("alert", { size: 16 })}<span>the coin is live, but your first buy didn't go through (${r.buyError.message}). buy it on the coin page.</span></div>` : ""}
        <button class="ca" data-copy="${r.mint}"><span class="mono">${r.mint}</span>${icon("copy", { size: 15 })}</button>
        <div class="row gap wrap">
          <a class="btn btn-primary grow" href="#/coin/${r.mint}" data-close>${icon("rocket")} view coin</a>
          <a class="btn btn-dark grow" href="https://x.com/intent/post?text=${encodeURIComponent(share)}" target="_blank" rel="noopener noreferrer">${icon("x", { size: 15 })} share</a>
          <a class="btn btn-dark grow" href="${app.explorer("tx", r.createSignature)}" target="_blank" rel="noopener noreferrer">${icon("external", { size: 15 })} explorer</a>
        </div>
      </div>`);
    modal.el.querySelector("[data-copy]")?.addEventListener("click", (e) => copyText(e.currentTarget.dataset.copy));
  }

  // The curve config, for the first-buy quote and preview.
  loadLaunchContext(app.connection, {
    network: app.cfg.network,
    platform: app.cfg.platformId || undefined,
    config: app.cfg.configId || undefined,
  })
    .then((ctx) => {
      form.ctx = ctx;
      const feePct = Number(ctx.fees.protocol + ctx.fees.platform + ctx.fees.creator) / 1e4;
      el("curve-info").textContent = `1B supply · 85 SOL to graduate · ${feePct}% trade fee`;
      update();
    })
    .catch((e) => console.warn("launch context", e));

  update();
  const off = app.subscribe(update);
  return () => {
    off();
    document.removeEventListener("paste", onPaste);
  };
}

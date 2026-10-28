// The board: hero, trending rail, and every rocket launched here.
import { $, compact, count, html, icon, mount, on, toast, trusted } from "./dom.js";
import { coinCard, coinImage, mcap, trendCard } from "./parts.js";
import { loadOlder, supremeRocket } from "../main.js";

const TABS = [
  { id: "new", label: "new", icon: "sparkles" },
  { id: "mcap", label: "market cap", icon: "chart" },
  { id: "near", label: "about to orbit", icon: "flame" },
  { id: "orbit", label: "in orbit", icon: "orbit" },
];

function sortCoins(coins, tab) {
  const by = (f) => [...coins].sort((a, b) => f(b) - f(a));
  if (tab === "mcap") return by((c) => c.mcapSol ?? -1);
  if (tab === "near") return by((c) => c.progress).filter((c) => !c.graduated && c.progress > 0);
  if (tab === "orbit") return coins.filter((c) => c.graduated);
  return coins;
}

function matches(coin, q) {
  if (!q) return true;
  const s = q.trim().toLowerCase();
  return coin.name.toLowerCase().includes(s) || coin.symbol.toLowerCase().includes(s) || coin.mint.toLowerCase() === s || coin.mint.toLowerCase().startsWith(s);
}

const SWOOSH = `<svg class="swoosh" viewBox="0 0 300 24" preserveAspectRatio="none" aria-hidden="true"><path d="M4 17c48-9 112-14 168-11 44 2 82 7 124 3" fill="none" stroke="currentColor" stroke-width="7" stroke-linecap="round"/></svg>`;

function hero(app) {
  return html`<section class="hero">
    <div class="hero-copy">
      <h1>build your own <span class="hl">rocket${trusted(SWOOSH)}</span> here.</h1>
      <p class="lede">the supreme launchpad on Raydium LaunchLab. paint a rocket, name it, press launch. it trades on a bonding curve
        the second it clears the pad, and lands on Raydium when the tank hits 85 SOL. one wallet, one click, ${app.cfg.feeSol} SOL to fly.</p>
      <div class="chips">
        <span class="chip" style="--tilt:-2deg">${icon("arrow", { size: 15 })} our own Raydium LaunchLab launchpad</span>
        <span class="chip chip-red" style="--tilt:1.5deg">${icon("rocket", { size: 15 })} ${app.cfg.feeSol} SOL per launch</span>
        <span class="chip chip-dark" style="--tilt:-1deg">${icon("wallet", { size: 15 })} phantom ready</span>
        <span class="chip" style="--tilt:2deg">${icon("shield", { size: 15 })} fair curve, no presale</span>
        <span class="chip chip-dark" style="--tilt:-1.5deg">${icon("orbit", { size: 15 })} graduates to Raydium</span>
      </div>
      <div class="cta">
        <a class="btn btn-primary btn-lg" href="#/create">${icon("plus", { size: 20 })} build a rocket</a>
        <a class="btn btn-dark btn-lg" href="#/how">how it works</a>
      </div>
      <div class="stats" id="stats"></div>
    </div>
    <div class="mascot">
      <div class="mascot-glow"></div>
      <svg class="mascot-rays" viewBox="0 0 400 400" aria-hidden="true">${rays()}</svg>
      <img class="mascot-kim" src="assets/kim-sticker.webp" width="462" height="742" alt="the supreme leader, approving your launch">
      <button class="bubble" data-ideas>no idea what to launch? <b>I'll pitch you three.</b></button>
      <img class="mascot-rocket" src="assets/rocket.svg" alt="" width="120" height="120">
      <div class="king" id="king"></div>
    </div>
  </section>`;
}

function rays() {
  let s = "";
  for (let i = 0; i < 16; i++) {
    const a = (i * Math.PI * 2) / 16;
    const a1 = a - 0.09;
    const a2 = a + 0.09;
    s += `<path d="M200 200 L${(200 + Math.cos(a1) * 280).toFixed(1)} ${(200 + Math.sin(a1) * 280).toFixed(1)} L${(200 + Math.cos(a2) * 280).toFixed(1)} ${(200 + Math.sin(a2) * 280).toFixed(1)} Z"/>`;
  }
  return trusted(s);
}

function stats(app, coins) {
  const orbit = coins.filter((c) => c.graduated).length;
  const raised = coins.reduce((n, c) => n + (c.raisedSol || 0), 0);
  return html`
    <div class="stat"><span class="stat-icon">${icon("rocket")}</span><div><b class="mono">${count(coins.length)}</b><span>rockets launched</span></div></div>
    <div class="stat"><span class="stat-icon gold">${icon("orbit")}</span><div><b class="mono gold-text">${count(orbit)}</b><span>in orbit</span></div></div>
    <div class="stat"><span class="stat-icon">${icon("flame")}</span><div><b class="mono">${compact(raised)} SOL</b><span>in the tanks</span></div></div>`;
}

function king(coins) {
  const top = supremeRocket(coins);
  if (!top) {
    return html`<a class="king-card" href="#/create"><span class="king-kicker">${icon("crown", { size: 14 })} supreme rocket</span>
      <span class="king-name">your rocket here</span><span class="king-cap mono">be the first</span></a>`;
  }
  return html`<a class="king-card" href="#/coin/${top.mint}">
    <span class="king-kicker">${icon("crown", { size: 14 })} supreme rocket</span>
    <span class="king-row">${coinImage(top, "king-img")}<span><span class="king-name">${top.name}</span> <span class="ticker">$${top.symbol}</span></span></span>
    <span class="king-cap mono">${mcap(top)}</span></a>
    <span class="king-hint">${icon("down", { size: 12 })} tap ${top.symbol}</span>`;
}

function trending(coins) {
  const top = coins
    .filter((c) => c.mcapSol !== null)
    .sort((a, b) => b.mcapSol - a.mcapSol)
    .slice(0, 12);
  if (top.length < 2) return "";
  const cards = top.map((c, i) => trendCard(c, i + 1));
  // Doubled so the marquee can loop without a seam.
  return html`<section class="section">
    <div class="section-head"><h2>${icon("flame", { size: 22 })} rockets in flight</h2><span class="muted small">point at it to stop it</span></div>
    <div class="rail ${top.length >= 5 ? "rail-loop" : ""}"><div class="rail-track">${cards}${top.length >= 5 ? cards : ""}</div></div>
  </section>`;
}

export function renderBoard(root, app, { tab = "new", live = false } = {}) {
  let current = tab;
  let shown = 30;
  mount(
    root,
    html`${live ? "" : hero(app)}
      <div id="trending"></div>
      <section class="section" id="board">
        <div class="section-head board-head">
          <h2>${live ? html`<span class="live-dot"></span> live launches` : "the launch pad"}</h2>
          <div class="tabs" role="tablist">
            ${TABS.map(
              (t) => html`<button class="tab" role="tab" data-tab="${t.id}" aria-selected="${t.id === current}">${icon(t.icon, { size: 15 })} ${t.label}</button>`,
            )}
          </div>
        </div>
        <div id="grid"></div>
        <div class="more" id="more"></div>
      </section>`,
  );

  const draw = () => {
    const coins = app.coins();
    const filtered = sortCoins(coins.filter((c) => matches(c, app.search)), current);
    const statsEl = $("#stats", root);
    if (statsEl) mount(statsEl, stats(app, coins));
    const kingEl = $("#king", root);
    if (kingEl) mount(kingEl, king(coins));
    if (!live) mount($("#trending", root), trending(coins));

    let body;
    if (app.status === "loading" && !coins.length) {
      body = html`<div class="grid">${Array.from({ length: 6 }, () => html`<div class="card skeleton"></div>`)}</div>`;
    } else if (!filtered.length) {
      body = html`<div class="empty">
        <img src="assets/kim-bust.webp" alt="" width="160" height="144">
        <h3>${app.search ? `nothing called “${app.search}” on the pad` : current === "new" ? "no rockets on the pad yet" : "nothing here yet"}</h3>
        <p class="muted">${app.status === "error"
          ? html`live data is paused: ${app.error}. retrying…${app.cfg.usingPublicRpc ? " (site owner: the public Solana RPC often refuses browsers; set rpcUrl in config.js)" : ""}`
          : "the leader is waiting. build the first one."}</p>
        <a class="btn btn-primary" href="#/create">${icon("plus")} build a rocket</a>
      </div>`;
    } else {
      body = html`<div class="grid">${filtered.slice(0, shown).map(coinCard)}</div>`;
    }
    mount($("#grid", root), body);
    const more = $("#more", root);
    if (filtered.length > shown) mount(more, html`<button class="btn btn-dark" data-more>show more</button>`);
    else if (!app.done && coins.length && current === "new" && !app.search)
      mount(more, html`<button class="btn btn-dark" data-older>${icon("clock", { size: 16 })} load older launches</button>`);
    else mount(more, "");
    if (app.status === "error" && coins.length) {
      mount(more, html`<p class="muted small">${icon("alert", { size: 14 })} live data is paused (${app.error}). showing the last known board.</p>`);
    }
  };

  on(root, "click", "[data-tab]", (e, el) => {
    current = el.dataset.tab;
    shown = 30;
    for (const t of root.querySelectorAll("[data-tab]")) t.setAttribute("aria-selected", String(t === el));
    draw();
  });
  on(root, "click", "[data-more]", () => {
    shown += 30;
    draw();
  });
  on(root, "click", "[data-older]", async (e, el) => {
    el.disabled = true;
    try {
      const n = await loadOlder();
      if (!n) toast("that's every launch so far");
    } catch (err) {
      toast(err.message, { kind: "error" });
    }
  });

  draw();
  return app.subscribe(draw);
}

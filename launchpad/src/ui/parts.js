// Pieces the views share: coin pictures, progress, the bonding-curve sparkline, cards.
import { ago, compact, html, icon, pct, short, sol, trusted, usd } from "./dom.js";

const HUES = [4, 28, 45, 160, 200, 260, 320];

export function coinImage(coin, cls = "") {
  const letter = (coin.symbol || coin.name || "?").trim().charAt(0).toUpperCase() || "?";
  const hue = HUES[(coin.mint?.charCodeAt(5) ?? 0) % HUES.length];
  const fallback = html`<span class="coin-img coin-fallback ${cls}" style="--h:${hue}" aria-hidden="true">${letter}</span>`;
  if (!coin.image) return fallback;
  return html`<span class="coin-img-wrap ${cls}"><img class="coin-img ${cls}" src="${coin.image}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" data-fallback="${letter}" style="--h:${hue}"></span>`;
}

// Broken coin pictures turn into the letter badge.
export function installImageFallback(root = document) {
  root.addEventListener(
    "error",
    (e) => {
      const img = e.target;
      if (!(img instanceof HTMLImageElement) || !img.dataset.fallback) return;
      const span = document.createElement("span");
      span.className = `${img.className} coin-fallback`;
      span.style.cssText = img.style.cssText;
      span.textContent = img.dataset.fallback;
      img.replaceWith(span);
    },
    true,
  );
}

export function progressBar(coin, { label = true } = {}) {
  const p = coin.graduated ? 1 : coin.progress;
  return html`<div class="progress ${coin.graduated ? "done" : ""}" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(p * 100)}" aria-label="bonding curve progress">
      <div class="bar"><span style="width:${(p * 100).toFixed(1)}%"></span></div>
      ${label
        ? coin.graduated
          ? html`<span class="progress-label">${icon("orbit", { size: 14 })} in orbit</span>`
          : html`<span class="progress-label mono">${pct(p)}</span>`
        : ""}
    </div>`;
}

// The curve the coin lives on: price against SOL raised, filled up to where it is now.
export function curveSpark(pool, { w = 120, h = 44 } = {}) {
  if (!pool) return "";
  const vB = Number(pool.virtualB);
  const target = Math.max(1, Number(pool.totalFundRaisingB));
  const now = Math.min(Number(pool.realB), target);
  const y = (x) => (vB + x) ** 2;
  const y0 = y(0);
  const y1 = y(target);
  const steps = 24;
  const pts = [];
  for (let i = 0; i <= steps; i++) {
    const x = (target * i) / steps;
    pts.push([(i / steps) * w, h - 3 - ((y(x) - y0) / (y1 - y0)) * (h - 6)]);
  }
  const line = pts.map(([px, py], i) => `${i ? "L" : "M"}${px.toFixed(1)} ${py.toFixed(1)}`).join(" ");
  const cut = Math.max(0, Math.min(1, now / target));
  const cx = cut * w;
  const cy = h - 3 - ((y(now) - y0) / (y1 - y0)) * (h - 6);
  const filled = pts.filter(([px]) => px <= cx);
  const area = filled.length
    ? `M0 ${h} ${filled.map(([px, py]) => `L${px.toFixed(1)} ${py.toFixed(1)}`).join(" ")} L${cx.toFixed(1)} ${cy.toFixed(1)} L${cx.toFixed(1)} ${h} Z`
    : "";
  return trusted(`<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true">
    <path d="${line}" class="spark-track"/>
    ${area ? `<path d="${area}" class="spark-fill"/>` : ""}
    <circle cx="${cx.toFixed(1)}" cy="${cy.toFixed(1)}" r="3.5" class="spark-dot"/>
  </svg>`);
}

export function socials(coin) {
  const m = coin.meta;
  if (!m) return "";
  const links = [
    m.twitter && html`<a class="social" href="${m.twitter}" target="_blank" rel="noopener noreferrer nofollow" aria-label="X">${icon("x", { size: 14 })}</a>`,
    m.telegram && html`<a class="social" href="${m.telegram}" target="_blank" rel="noopener noreferrer nofollow" aria-label="Telegram">${icon("telegram", { size: 14 })}</a>`,
    m.website && html`<a class="social" href="${m.website}" target="_blank" rel="noopener noreferrer nofollow" aria-label="website">${icon("globe", { size: 14 })}</a>`,
  ].filter(Boolean);
  return links.length ? html`<span class="socials">${links}</span>` : "";
}

export function mcap(coin) {
  if (coin.mcapUsd !== null) return usd(coin.mcapUsd);
  if (coin.mcapSol !== null) return sol(coin.mcapSol, 1);
  return "—";
}

export function coinCard(coin) {
  // The name link stretches over the card, so the social links inside stay real links.
  return html`<article class="card" data-mint="${coin.mint}">
    <div class="card-pic">${coinImage(coin, "pic")}</div>
    <div class="card-body">
      <div class="card-meta">
        <span class="by">${icon("rocket", { size: 13 })} ${short(coin.creator)}</span>
        <span class="dot">·</span><span>${ago(coin.time)}</span>
        ${coin.graduated ? html`<span class="tag tag-gold">${icon("orbit", { size: 12 })} orbit</span>` : ""}
      </div>
      <a class="card-name stretched" href="#/coin/${coin.mint}"><b>${coin.name}</b> <span class="ticker">$${coin.symbol}</span></a>
      <div class="card-stats">
        <div><div class="label">market cap</div><div class="value">${mcap(coin)}</div></div>
        ${curveSpark(coin.pool)}
      </div>
      <p class="card-desc">${coin.description || "no description. the ticker speaks for itself."}</p>
    </div>
    <div class="card-foot">
      ${progressBar(coin)}
      <div class="card-tags">
        <span class="tag tag-dark">${icon("flame", { size: 12 })} ${compact(coin.raisedSol)} / ${compact(coin.targetSol, { digits: 0 })} SOL</span>
        ${socials(coin)}
      </div>
    </div>
  </article>`;
}

export function trendCard(coin, rank) {
  return html`<a class="tcard" href="#/coin/${coin.mint}" data-mint="${coin.mint}">
    ${coinImage(coin, "tcard-img")}
    <span class="shade"></span>
    <span class="rank">#${rank}</span>
    ${coin.graduated ? html`<span class="tcard-orbit">${icon("orbit", { size: 13 })} orbit</span>` : ""}
    <span class="tcard-info">
      <span class="tcard-name">${coin.name}</span>
      <span class="tcard-row"><span class="ticker">$${coin.symbol}</span><b>${mcap(coin)}</b></span>
      ${progressBar(coin, { label: false })}
    </span>
  </a>`;
}

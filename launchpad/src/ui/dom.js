// Small DOM helpers: an escaping html`` template, formatting, icons.

class Html {
  constructor(s) {
    this.s = s;
  }
  toString() {
    return this.s;
  }
}

const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const escape = (v) => String(v).replace(/[&<>"']/g, (c) => ESC[c]);

function part(v) {
  if (v === null || v === undefined || v === false) return "";
  if (v instanceof Html) return v.s;
  if (Array.isArray(v)) return v.map(part).join("");
  return escape(v);
}

// Every interpolated value is escaped unless it is itself html``.
export function html(strings, ...values) {
  let out = strings[0];
  for (let i = 0; i < values.length; i++) out += part(values[i]) + strings[i + 1];
  return new Html(out);
}

export const trusted = (s) => new Html(s);

export function mount(el, content) {
  el.innerHTML = part(content);
  return el;
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export function on(root, event, selector, handler) {
  root.addEventListener(event, (e) => {
    const target = e.target.closest?.(selector);
    if (target && root.contains(target)) handler(e, target);
  });
}

// ---- formatting ---------------------------------------------------------------------------

export function short(address, n = 4) {
  const s = String(address);
  return s.length > n * 2 + 1 ? `${s.slice(0, n)}…${s.slice(-n)}` : s;
}

export function compact(n, { digits = 1 } = {}) {
  if (n === null || n === undefined || !Number.isFinite(n)) return "—";
  const abs = Math.abs(n);
  if (abs >= 1e9) return `${(n / 1e9).toFixed(digits)}B`;
  if (abs >= 1e6) return `${(n / 1e6).toFixed(digits)}M`;
  if (abs >= 1e3) return `${(n / 1e3).toFixed(digits)}k`;
  if (abs >= 100) return n.toFixed(0);
  if (abs >= 1) return n.toFixed(2);
  return n.toPrecision(2);
}

export const usd = (n) => (n === null || n === undefined || !Number.isFinite(n) ? "—" : `$${compact(n)}`);

export function sol(n, digits = 3) {
  if (n === null || n === undefined || !Number.isFinite(n)) return "—";
  if (Math.abs(n) >= 1000) return `${compact(n)} SOL`;
  return `${Number(n.toFixed(digits))} SOL`;
}

export const lamportsToSol = (l) => Number(l) / 1e9;

export function tokenAmount(raw, decimals = 6) {
  return compact(Number(raw) / 10 ** decimals, { digits: 2 });
}

// Tiny prices like 0.0000000281 read as 0.0₇281.
export function price(n) {
  if (!Number.isFinite(n) || n <= 0) return "—";
  if (n >= 0.01) return n.toFixed(4);
  const exp = Math.floor(Math.log10(n));
  const zeros = -exp - 1;
  const digits = Math.round(n * 10 ** (-exp + 2));
  const sub = String(zeros)
    .split("")
    .map((d) => "₀₁₂₃₄₅₆₇₈₉"[d])
    .join("");
  return `0.0${sub}${digits}`;
}

export function ago(unixSeconds) {
  if (!unixSeconds) return "just now";
  const s = Math.max(0, Date.now() / 1000 - unixSeconds);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export const pct = (p, digits = 0) => `${(p * 100).toFixed(digits)}%`;

// ---- icons (Lucide, ISC licence) -----------------------------------------------------------

const PATHS = {
  home: '<path d="m3 9 9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M9 22V12h6v10"/>',
  zap: '<path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/>',
  crown: '<path d="m2 4 3 12h14l3-12-6 7-4-7-4 7-6-7z"/><path d="M5 20h14"/>',
  flame:
    '<path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.07-2.14-.22-4.05 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.15.43-2.29 1-3a2.5 2.5 0 0 0 2.5 2.5z"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  help: '<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><path d="M12 17h.01"/>',
  search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
  wallet:
    '<path d="M19 7V4a1 1 0 0 0-1-1H5a2 2 0 0 0 0 4h15a1 1 0 0 1 1 1v4h-3a2 2 0 0 0 0 4h3a1 1 0 0 0 1-1v-2a1 1 0 0 0-1-1"/><path d="M3 5v14a2 2 0 0 0 2 2h15a1 1 0 0 0 1-1v-4"/>',
  rocket:
    '<path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z"/><path d="m12 15-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z"/><path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0"/><path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5"/>',
  star: '<path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/>',
  copy: '<rect width="14" height="14" x="8" y="8" rx="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  external: '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
  telegram: '<path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/>',
  globe: '<circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/><path d="M2 12h20"/>',
  down: '<path d="m6 9 6 6 6-6"/>',
  arrow: '<path d="M7 7h10v10"/><path d="M7 17 17 7"/>',
  alert: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
  sparkles:
    '<path d="M9.94 15.5A2 2 0 0 0 8.5 14.06l-6.14-1.58a.5.5 0 0 1 0-.96L8.5 9.94A2 2 0 0 0 9.94 8.5l1.58-6.14a.5.5 0 0 1 .96 0l1.58 6.14a2 2 0 0 0 1.44 1.44l6.14 1.58a.5.5 0 0 1 0 .96l-6.14 1.58a2 2 0 0 0-1.44 1.44l-1.58 6.14a.5.5 0 0 1-.96 0z"/>',
  image: '<rect width="18" height="18" x="3" y="3" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.09-3.09a2 2 0 0 0-2.82 0L6 21"/>',
  shuffle:
    '<path d="M2 18h1.4c1.3 0 2.5-.6 3.3-1.7l6.1-8.6c.7-1.1 2-1.7 3.3-1.7H22"/><path d="m18 2 4 4-4 4"/><path d="M2 6h1.9c1.5 0 2.9.9 3.6 2.2"/><path d="M22 18h-5.9c-1.3 0-2.6-.7-3.3-1.8l-.5-.8"/><path d="m18 14 4 4-4 4"/>',
  orbit:
    '<circle cx="12" cy="12" r="3"/><circle cx="19" cy="5" r="2"/><circle cx="5" cy="19" r="2"/><path d="M10.4 21.9a10 10 0 0 0 9.94-15.42"/><path d="M13.5 2.1a10 10 0 0 0-9.84 15.42"/>',
  logout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/>',
  chart: '<path d="M3 3v18h18"/><path d="M18 17V9"/><path d="M13 17V5"/><path d="M8 17v-3"/>',
  shield:
    '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/>',
  clock: '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
  close: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  upload: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m17 8-5-5-5 5"/><path d="M12 3v12"/>',
  info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
};

export function icon(name, { size = 18, cls = "" } = {}) {
  if (name === "x") {
    return trusted(
      `<svg class="icon ${cls}" width="${size}" height="${size}" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M18.24 2.25h3.31l-7.23 8.26 8.5 11.24h-6.66l-5.21-6.82-5.97 6.82H1.68l7.73-8.84L1.25 2.25h6.83l4.71 6.23zm-1.16 17.52h1.83L7.08 4.13H5.12z"/></svg>`,
    );
  }
  return trusted(
    `<svg class="icon ${cls}" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${PATHS[name] ?? ""}</svg>`,
  );
}

// ---- toasts and modals -----------------------------------------------------------------------

export function toast(message, { kind = "info", ms = 4200 } = {}) {
  const root = document.getElementById("toasts");
  if (!root) return;
  const el = document.createElement("div");
  el.className = `toast toast-${kind}`;
  el.setAttribute("role", kind === "error" ? "alert" : "status");
  mount(el, html`${icon(kind === "error" ? "alert" : kind === "good" ? "check" : "info")}<span>${message}</span>`);
  root.append(el);
  setTimeout(() => {
    el.classList.add("out");
    setTimeout(() => el.remove(), 300);
  }, ms);
}

export function openModal(content, { onClose, wide = false, dismissable = true } = {}) {
  const root = document.getElementById("modals");
  const back = document.createElement("div");
  back.className = "modal-back";
  const box = document.createElement("div");
  box.className = `modal${wide ? " modal-wide" : ""}`;
  box.setAttribute("role", "dialog");
  box.setAttribute("aria-modal", "true");
  back.append(box);
  root.append(back);
  const previous = document.activeElement;
  const close = () => {
    back.remove();
    document.removeEventListener("keydown", onKey);
    previous?.focus?.();
    onClose?.();
  };
  const onKey = (e) => {
    if (e.key === "Escape" && api.dismissable) close();
  };
  document.addEventListener("keydown", onKey);
  back.addEventListener("mousedown", (e) => {
    if (e.target === back && api.dismissable) close();
  });
  const api = {
    el: box,
    dismissable,
    close,
    update(next) {
      mount(box, next);
      box.querySelector("[data-close]")?.addEventListener("click", () => close());
    },
  };
  api.update(content);
  requestAnimationFrame(() => box.querySelector("button, a, input")?.focus());
  return api;
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("copied", { kind: "good", ms: 1800 });
  } catch {
    toast("could not copy; select it by hand", { kind: "error" });
  }
}

// Sign in with Phantom, shared by the tracker and admin pages.
// The wallet only signs a text message. Nothing here ever asks Phantom to sign a transaction.
(() => {
  async function api(path, { method = "GET", body } = {}) {
    const res = await fetch(path, {
      method,
      credentials: "same-origin",
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch { /* empty or non-JSON body */ }
    if (!res.ok) {
      const detail = data && typeof data.detail === "string" ? data.detail : `Something went wrong (${res.status}). Try again.`;
      const err = new Error(detail);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  const provider = () => {
    const p = window.phantom && window.phantom.solana;
    return p && p.isPhantom ? p : null;
  };

  // Phantom can inject a moment after the page loads.
  async function waitForProvider(ms = 1500) {
    const start = Date.now();
    while (!provider() && Date.now() - start < ms) await new Promise((r) => setTimeout(r, 100));
    return provider();
  }

  const isMobile = () => /Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
  const openInPhantomUrl = () =>
    `https://phantom.app/ul/browse/${encodeURIComponent(location.href)}?ref=${encodeURIComponent(location.origin)}`;
  const short = (a) => (a ? `${a.slice(0, 4)}…${a.slice(-4)}` : "");

  function toBase64(bytes) {
    let s = "";
    for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
    return btoa(s);
  }

  function friendly(e) {
    const text = (e && e.message) || "";
    if ((e && e.code === 4001) || /reject|cancel|denied/i.test(text)) {
      const err = new Error("You cancelled in Phantom. Nothing was shared.");
      err.code = "CANCELLED";
      return err;
    }
    return new Error(text ? `Phantom: ${text}` : "Phantom couldn't finish. Try again.");
  }

  async function signIn() {
    const p = await waitForProvider();
    if (!p) {
      const err = new Error("Phantom isn't available in this browser.");
      err.code = "NO_PHANTOM";
      throw err;
    }
    let wallet;
    try {
      const res = await p.connect();
      wallet = res.publicKey.toString();
    } catch (e) {
      throw friendly(e);
    }
    const { nonce, message } = await api("/api/auth/nonce", { method: "POST", body: { wallet } });
    let signed;
    try {
      signed = await p.signMessage(new TextEncoder().encode(message), "utf8");
    } catch (e) {
      throw friendly(e);
    }
    const sig = signed && signed.signature instanceof Uint8Array ? signed.signature : new Uint8Array(signed.signature);
    return api("/api/auth/verify", { method: "POST", body: { nonce, signature: toBase64(sig) } });
  }

  // The signed-in wallet as {wallet, isAdmin}, or null.
  async function me() {
    const who = await api("/api/me");
    return who && who.wallet ? who : null;
  }

  async function signOut() {
    try {
      await api("/api/auth/logout", { method: "POST" });
    } finally {
      try { await provider()?.disconnect(); } catch { /* already disconnected */ }
    }
  }

  // Calls back with the new address (or null) when the user switches accounts in Phantom.
  function onAccountChange(cb) {
    const p = provider();
    if (p && typeof p.on === "function") p.on("accountChanged", (pk) => cb(pk ? pk.toString() : null));
  }

  // Small DOM helper: strings become text nodes, never HTML (coin names come from strangers).
  function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
      if (v == null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : String(v));
    }
    for (const c of children.flat()) if (c != null && c !== false) node.append(c instanceof Node ? c : String(c));
    return node;
  }

  // ---------- number formatting ----------
  const SUB = "₀₁₂₃₄₅₆₇₈₉";
  function usd(v, { compact = false } = {}) {
    if (v == null || !isFinite(v)) return "—";
    const a = Math.abs(v);
    if (compact && a >= 10000) {
      return "$" + new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 2 }).format(v);
    }
    if (a >= 1 || a === 0) {
      return v.toLocaleString("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
    const zeros = Math.floor(-Math.log10(a));          // 0.000012 -> 4 zeros after the point
    if (zeros >= 4) {
      const digits = String(Math.round(a * 10 ** (zeros + 4))).slice(0, 4).replace(/0+$/, "") || "0";
      const sub = String(zeros).split("").map((d) => SUB[+d]).join("");
      return `${v < 0 ? "-" : ""}$0.0${sub}${digits}`;
    }
    return `${v < 0 ? "-" : ""}$${Number(a.toPrecision(4)).toString()}`;
  }
  function amount(v) {
    if (v == null || !isFinite(v)) return "—";
    if (Math.abs(v) >= 100000) return new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 2 }).format(v);
    return v.toLocaleString("en-US", { maximumFractionDigits: v >= 1 ? 2 : 6 });
  }
  function pct(v) {
    if (v == null || !isFinite(v)) return { text: "—", cls: "" };
    return { text: `${v > 0 ? "+" : ""}${v.toFixed(2)}%`, cls: v > 0 ? "up" : v < 0 ? "down" : "" };
  }
  function ago(ts, now = Date.now() / 1000) {
    if (!ts) return "—";
    const s = Math.max(0, Math.round(now - ts));
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    return `${Math.floor(s / 86400)} d ago`;
  }

  // A coin's logo, or its first letter on a dark disc when there is no logo.
  function coinLogo(coin) {
    const letter = ((coin.symbol || coin.name || "?").trim()[0] || "?").toUpperCase();
    const fallback = () => el("span", { class: "coin-logo", "aria-hidden": "true", text: letter });
    if (!coin.image || !/^https:\/\//.test(coin.image)) return fallback();
    const img = el("img", { class: "coin-logo", src: coin.image, alt: "", loading: "lazy", referrerpolicy: "no-referrer", width: 36, height: 36 });
    img.addEventListener("error", () => img.replaceWith(fallback()), { once: true });
    return img;
  }

  const dexUrl = (coin) =>
    coin.url && /^https:\/\/dexscreener\.com\//.test(coin.url)
      ? coin.url
      : `https://dexscreener.com/solana/${encodeURIComponent(coin.mint)}`;

  window.PansolWallet = {
    api, provider, waitForProvider, isMobile, openInPhantomUrl, short,
    signIn, signOut, me, onAccountChange,
    el, usd, amount, pct, ago, coinLogo, dexUrl,
  };
})();

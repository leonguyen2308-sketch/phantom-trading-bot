// Meme tracker page: sign in with Phantom, then show this wallet's coins and watchlist.
(() => {
  const W = window.PansolWallet;
  const { el } = W;
  const $ = (id) => document.getElementById(id);
  const REFRESH_MS = 30000;
  const SOL_MINT = "So11111111111111111111111111111111111111112";
  const LAUNCH = { year: 2026, month: 10, day: 15, utcOffsetHours: 7 };

  let me = null;
  let timer = 0;
  let running = null;   // the portfolio request in flight
  let queued = null;    // one more request, started when it finishes

  function show(view) {
    for (const id of ["loading", "signedOut", "app"]) $(id).hidden = id !== view;
    $("account").hidden = view !== "app";
  }

  function setMsg(id, text, kind = "") {
    const node = $(id);
    node.textContent = text || "";
    node.className = `msg${kind ? " " + kind : ""}`;
  }

  function showWallet() {
    $("walletShort").textContent = W.short(me.wallet);
    $("walletShort").title = me.wallet;
  }

  // ---------- signed out ----------

  function clearPortfolio() {
    for (const id of ["holdings", "watchlist"]) $(id).replaceChildren();
    for (const id of ["total", "solAmount", "coinCount", "updated"]) $(id).textContent = "—";
    for (const id of ["holdingsEmpty", "watchEmpty", "hiddenNote"]) $(id).hidden = true;
    setMsg("appMsg", "");
    setMsg("watchMsg", "");
    $("watchMint").value = "";
  }

  async function showSignedOut(message = "") {
    stopRefresh();
    me = null;
    clearPortfolio();
    show("signedOut");
    setMsg("signInMsg", message, message ? "error" : "");
    const p = await W.waitForProvider();
    $("connect").hidden = !p;
    $("openInPhantom").hidden = !!p || !W.isMobile();
    $("getPhantom").hidden = !!p || W.isMobile();
    if (!p) {
      $("openInPhantom").href = W.openInPhantomUrl();
      setMsg("signInMsg", W.isMobile()
        ? "On your phone or tablet, open this page inside the Phantom app to connect."
        : "Phantom isn't installed in this browser. Get it, then reload this page.");
    }
  }

  $("connect").addEventListener("click", async () => {
    const btn = $("connect");
    btn.disabled = true;
    $("connectLabel").textContent = "Check Phantom…";
    setMsg("signInMsg", "Approve the connection, then sign the message in Phantom.");
    try {
      me = await W.signIn();
      showApp();
    } catch (e) {
      setMsg("signInMsg", e.message, "error");
    } finally {
      btn.disabled = false;
      $("connectLabel").textContent = "Connect Phantom";
    }
  });

  $("signOut").addEventListener("click", async () => {
    try {
      await W.signOut();
      showSignedOut();
    } catch (e) {
      setMsg("appMsg", `Couldn't sign out: ${e.message}`, "error");
    }
  });

  // ---------- signed in ----------

  function showApp() {
    clearPortfolio();
    show("app");
    showWallet();
    renderPansol(null);
    load();
    startRefresh();
  }

  function startRefresh() {
    stopRefresh();
    timer = setInterval(() => { if (!document.hidden) load(); }, REFRESH_MS);
  }
  function stopRefresh() {
    clearInterval(timer);
    timer = 0;
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden && me) load(); });

  // One request at a time. A call made while one runs gets a fresh run after it, so a just-added
  // watchlist coin or a just-switched wallet always shows up.
  function load() {
    if (!me) return Promise.resolve();
    if (!running) {
      running = fetchPortfolio().finally(() => { running = null; });
      return running;
    }
    if (!queued) queued = running.then(() => { queued = null; return load(); });
    return queued;
  }

  async function fetchPortfolio() {
    $("refresh").setAttribute("aria-busy", "true");
    try {
      const data = await W.api("/api/portfolio");
      if (!me) return;
      if (data.wallet !== me.wallet) {
        // Someone signed in with another wallet in a different tab; the cookie now belongs to it.
        const who = await W.me().catch(() => null);
        if (!who) return showSignedOut("Your sign-in expired. Connect again.");
        if (who.wallet !== data.wallet) return;
        me = who;
        showWallet();
        setMsg("watchMsg", "");
      }
      render(data);
      setMsg("appMsg", data.prices_ok ? "" : "DexScreener isn't answering right now, so some prices are missing. Retrying shortly.");
    } catch (e) {
      if (e.status === 401) return showSignedOut("Your sign-in expired. Connect again.");
      setMsg("appMsg", e.message, "error");
    } finally {
      $("refresh").removeAttribute("aria-busy");
    }
  }
  $("refresh").addEventListener("click", () => load());

  function render(data) {
    $("total").textContent = W.usd(data.total_usd);
    const sol = data.holdings.find((h) => h.mint === SOL_MINT);
    $("solAmount").textContent = sol ? W.amount(sol.amount) : "0";
    $("coinCount").textContent = String(data.coin_count ?? data.holdings.length);
    $("updated").textContent = new Date(data.updated_at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

    $("holdings").replaceChildren(...data.holdings.map(holdingRow));
    $("holdingsEmpty").hidden = data.holdings.length > 0;
    $("hiddenNote").hidden = !data.hidden_tokens;
    $("hiddenNote").textContent = data.hidden_tokens
      ? `${data.hidden_tokens} more tokens in this wallet aren't listed. They have the lowest value or no market (usually airdropped spam).`
      : "";

    $("watchlist").replaceChildren(...data.watchlist.map(watchRow));
    $("watchEmpty").hidden = data.watchlist.length > 0;
    renderPansol(data.pansol);
  }

  function coinCell(coin, sub, cls = "c-coin coin-cell") {
    const label = coin.name || coin.symbol || W.short(coin.mint);
    return el("div", { class: cls },
      W.coinLogo(coin),
      el("div", { class: "coin-name" },
        el("strong", { text: label }),
        el("span", { text: sub })));
  }

  function holdingRow(h) {
    const change = W.pct(h.change_24h);
    const sub = `${W.amount(h.amount)} ${h.symbol || W.short(h.mint)}${h.price_usd == null ? " · no market found" : ""}`;
    return el("a", { class: "coin-row", href: W.dexUrl(h), target: "_blank", rel: "noopener noreferrer" },
      coinCell(h, sub),
      el("span", { class: "c-price", text: W.usd(h.price_usd) }),
      el("span", { class: `c-change ${change.cls}`, text: change.text }),
      el("span", { class: "c-value", text: W.usd(h.value_usd) }));
  }

  function watchRow(c) {
    const change = W.pct(c.change_24h);
    const sub = c.price_usd == null
      ? "No market found yet"
      : `${c.symbol || W.short(c.mint)} · ${W.usd(c.price_usd)} · MC ${W.usd(c.market_cap, { compact: true })}`;
    const remove = el("button", {
      class: "link-btn c-action",
      type: "button",
      text: "Remove",
      "data-mint": c.mint,
      "aria-label": `Remove ${c.name || c.symbol || c.mint} from watchlist`,
      onclick: (e) => removeWatch(c.mint, e.currentTarget),
    });
    return el("div", { class: "coin-row watch-row-item" },
      el("a", { class: "c-coin", href: W.dexUrl(c), target: "_blank", rel: "noopener noreferrer", style: "text-decoration:none;min-width:0" },
        coinCell(c, sub, "coin-cell")),
      el("span", { class: "c-price", text: W.usd(c.price_usd) }),
      el("span", { class: `c-change ${change.cls}`, text: change.text }),
      el("span", { class: "c-extra", text: W.usd(c.market_cap, { compact: true }) }),
      remove);
  }

  // ---------- watchlist ----------

  let watchBusy = false;
  $("watchForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (watchBusy) return;
    const mint = $("watchMint").value.trim();
    if (!mint) return setMsg("watchMsg", "Paste a coin's token address first.", "error");
    watchBusy = true;
    $("watchAdd").setAttribute("aria-busy", "true");
    try {
      await W.api("/api/watchlist", { method: "POST", body: { mint } });
      $("watchMint").value = "";
      setMsg("watchMsg", "Added. Loading its price…", "ok");
      await load();
      setMsg("watchMsg", "");
    } catch (err) {
      if (err.status === 401) return showSignedOut("Your sign-in expired. Connect again.");
      setMsg("watchMsg", err.message, "error");
    } finally {
      watchBusy = false;
      $("watchAdd").removeAttribute("aria-busy");
      if (me) $("watchMint").focus();
    }
  });

  async function removeWatch(mint, button) {
    // Where keyboard focus goes once this row is gone: the next row's Remove, else the address field.
    const buttons = [...$("watchlist").querySelectorAll("button[data-mint]")];
    const next = buttons[buttons.indexOf(button) + 1] || buttons[buttons.indexOf(button) - 1];
    const nextMint = next ? next.dataset.mint : null;
    try {
      await W.api(`/api/watchlist/${encodeURIComponent(mint)}`, { method: "DELETE" });
      await load();
      const target = nextMint && [...$("watchlist").querySelectorAll("button[data-mint]")].find((b) => b.dataset.mint === nextMint);
      (target || $("watchMint")).focus();
    } catch (err) {
      if (err.status === 401) return showSignedOut("Your sign-in expired. Connect again.");
      setMsg("watchMsg", err.message, "error");
    }
  }

  // ---------- Pansol card: countdown before launch, live price after ----------

  function daysToLaunch() {
    const day = 86400000;
    const local = Date.now() + LAUNCH.utcOffsetHours * 3600000;
    const today = Math.floor(local / day) * day;
    return Math.round((Date.UTC(LAUNCH.year, LAUNCH.month - 1, LAUNCH.day) - today) / day);
  }

  function renderPansol(coin) {
    const card = $("pansolCard");
    if (coin && coin.price_usd != null) {
      const change = W.pct(coin.change_24h);
      card.href = W.dexUrl(coin);
      card.target = "_blank";
      card.rel = "noopener noreferrer";
      $("pansolTitle").textContent = `PANSOL ${W.usd(coin.price_usd)}`;
      $("pansolSub").textContent = `24h ${change.text} · Market cap ${W.usd(coin.market_cap, { compact: true })}`;
      return;
    }
    card.href = "/";
    card.removeAttribute("target");
    const days = daysToLaunch();
    $("pansolTitle").textContent = days > 1 ? `PANSOL launches in ${days} days`
      : days === 1 ? "PANSOL launches tomorrow"
      : days === 0 ? "PANSOL launches today"
      : "PANSOL is live";
    $("pansolSub").textContent = days >= 0
      ? "Launch day: Thursday 15 October 2026. Tap for the countdown."
      : "The panda meme coin on Solana.";
  }

  // ---------- start ----------

  (async () => {
    try {
      me = await W.me();
    } catch {
      me = null;
    }
    if (me) showApp();
    else await showSignedOut();

    await W.waitForProvider();
    W.onAccountChange(async (wallet) => {
      if (!me || wallet === me.wallet) return;
      try {
        await W.signOut();
        showSignedOut("You switched wallets in Phantom. Connect again to track this one.");
      } catch (e) {
        setMsg("appMsg", `You switched wallets in Phantom, but signing out failed (${e.message}). Press Sign out to try again.`, "error");
      }
    });
  })();
})();

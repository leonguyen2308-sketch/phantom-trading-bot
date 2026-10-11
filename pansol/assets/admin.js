// Admin page: sign in with an admin wallet, then list every tracker user.
// The server refuses /api/admin/* for any other wallet; this page only displays what it is given.
(() => {
  const W = window.PansolWallet;
  const { el } = W;
  const $ = (id) => document.getElementById(id);
  const REFRESH_MS = 30000;

  let me = null;
  let users = [];
  let total = 0;
  let serverNow = Date.now() / 1000;
  let timer = 0;
  let running = null;
  let queued = null;
  let openWallet = null;       // wallet whose detail dialog is open
  let renderedWallet = null;   // wallet whose data the dialog currently shows

  function show(view) {
    for (const id of ["loading", "signedOut", "notAdmin", "app"]) $(id).hidden = id !== view;
    $("account").hidden = view !== "app" && view !== "notAdmin";
  }

  function setMsg(id, text, kind = "") {
    $(id).textContent = text || "";
    $(id).className = `msg${kind ? " " + kind : ""}`;
  }

  // Forget every user's data this page holds (sign-out, lost admin rights).
  function clearData() {
    stop();
    users = [];
    total = 0;
    $("rows").replaceChildren();
    $("listNote").hidden = true;
    $("empty").hidden = true;
    if ($("detail").open) $("detail").close();
    clearDetail();
    setMsg("appMsg", "");
  }

  // ---------- sign in ----------

  async function showSignedOut(message = "") {
    me = null;
    clearData();
    show("signedOut");
    setMsg("signInMsg", message, message ? "error" : "");
    const p = await W.waitForProvider();
    $("connect").hidden = !p;
    $("openInPhantom").hidden = !!p || !W.isMobile();
    $("getPhantom").hidden = !!p || W.isMobile();
    if (!p) {
      $("openInPhantom").href = W.openInPhantomUrl();
      setMsg("signInMsg", W.isMobile()
        ? "On your phone or tablet, open this page inside the Phantom app."
        : "Phantom isn't installed in this browser.");
    }
  }

  function signedIn(who) {
    me = who;
    $("walletShort").textContent = W.short(me.wallet);
    $("walletShort").title = me.wallet;
    if (!me.isAdmin) {
      clearData();
      $("notAdminWallet").textContent = me.wallet;
      setMsg("notAdminMsg", "");
      show("notAdmin");
      return;
    }
    show("app");
    load();
    stop();
    timer = setInterval(() => { if (!document.hidden) load(); }, REFRESH_MS);
  }

  $("connect").addEventListener("click", async () => {
    $("connect").disabled = true;
    $("connectLabel").textContent = "Check Phantom…";
    setMsg("signInMsg", "Approve the connection, then sign the message in Phantom.");
    try {
      signedIn(await W.signIn());
    } catch (e) {
      setMsg("signInMsg", e.message, "error");
    } finally {
      $("connect").disabled = false;
      $("connectLabel").textContent = "Connect admin wallet";
    }
  });

  async function signOut() {
    try {
      await W.signOut();
      showSignedOut();
    } catch (e) {
      const where = $("notAdmin").hidden ? "appMsg" : "notAdminMsg";
      setMsg(where, `Couldn't sign out: ${e.message}`, "error");
    }
  }
  $("signOut").addEventListener("click", signOut);
  $("switchWallet").addEventListener("click", signOut);

  function stop() {
    clearInterval(timer);
    timer = 0;
  }

  // Lost admin rights or the session changed in another tab: show whoever is signed in now.
  async function recheck(message) {
    clearData();
    const who = await W.me().catch(() => null);
    if (who) signedIn(who);
    else showSignedOut(message);
  }

  // ---------- user list ----------

  function load() {
    if (!me || !me.isAdmin) return Promise.resolve();
    if (!running) {
      running = fetchUsers().finally(() => { running = null; });
      return running;
    }
    if (!queued) queued = running.then(() => { queued = null; return load(); });
    return queued;
  }

  async function fetchUsers() {
    $("refresh").setAttribute("aria-busy", "true");
    try {
      const data = await W.api("/api/admin/users");
      if (!me || !me.isAdmin) return;
      users = data.users;
      total = data.stats.users;
      serverNow = data.now;
      renderStats(data.stats);
      renderRows();
      setMsg("appMsg", "");
    } catch (e) {
      if (e.status === 401) return showSignedOut("Your admin sign-in expired. Connect again.");
      if (e.status === 403) return recheck("Connect again.");
      setMsg("appMsg", e.message, "error");
    } finally {
      $("refresh").removeAttribute("aria-busy");
    }
  }
  $("refresh").addEventListener("click", () => load());
  document.addEventListener("visibilitychange", () => { if (!document.hidden && me && me.isAdmin) load(); });

  function renderStats(s) {
    const n = new Intl.NumberFormat("en-US");
    $("statUsers").textContent = n.format(s.users);
    $("statNew").textContent = n.format(s.new_24h);
    $("statActive").textContent = n.format(s.active_24h);
    $("statValue").textContent = W.usd(s.tracked_usd, { compact: true });
  }

  function when(ts) {
    const d = new Date(ts * 1000);
    return el("span", { title: d.toLocaleString(), text: W.ago(ts, serverNow) });
  }

  function renderRows() {
    const q = $("search").value.trim().toLowerCase();
    const key = $("sort").value;
    const list = users
      .filter((u) => !q || u.wallet.toLowerCase().includes(q))
      .sort((a, b) => (b[key] ?? -1) - (a[key] ?? -1));
    $("rows").replaceChildren(...list.map((u) => el("tr", {},
      el("td", {},
        el("button", { class: "wallet-btn", type: "button", title: u.wallet, "data-wallet": u.wallet, text: W.short(u.wallet), onclick: () => openDetail(u.wallet) }),
        u.is_admin ? el("span", { class: "badge", text: "admin" }) : null),
      el("td", {}, when(u.first_seen)),
      el("td", {}, when(u.last_seen)),
      el("td", { class: "num", text: String(u.sign_ins) }),
      el("td", { class: "num", text: u.value_usd == null ? "not checked" : W.usd(u.value_usd) }),
      el("td", { class: "num", text: u.token_count == null ? "—" : String(u.token_count) }),
      el("td", { class: "num", text: String(u.watch_count) }),
    )));
    $("empty").hidden = users.length > 0;

    const notes = [];
    if (total > users.length) {
      notes.push(`Showing the ${users.length.toLocaleString("en-US")} most recently active of ${total.toLocaleString("en-US")} users. Export CSV to get everyone.`);
    }
    if (users.length && !list.length) notes.push("No wallet in this list matches that search.");
    $("listNote").textContent = notes.join(" ");
    $("listNote").hidden = notes.length === 0;
  }
  $("search").addEventListener("input", renderRows);
  $("sort").addEventListener("change", renderRows);

  // ---------- one user ----------

  function clearDetail() {
    renderedWallet = null;
    for (const id of ["detailFacts", "detailHoldings", "detailWatch"]) $(id).replaceChildren();
    setMsg("detailMsg", "");
  }

  async function openDetail(wallet, live = false) {
    if (wallet !== renderedWallet) clearDetail();
    openWallet = wallet;
    $("detailAddr").textContent = wallet;
    $("detailSolscan").href = `https://solscan.io/account/${encodeURIComponent(wallet)}`;
    if (!$("detail").open) $("detail").showModal();
    setMsg("detailMsg", live ? "Checking the wallet on Solana…" : "Loading…");
    $("detailLive").setAttribute("aria-busy", "true");
    try {
      const data = await W.api(`/api/admin/users/${encodeURIComponent(wallet)}${live ? "?live=1" : ""}`);
      if (openWallet !== wallet) return;
      renderDetail(data);
      if (!live) setMsg("detailMsg", "");
      else if (data.portfolio && !data.portfolio.prices_ok) {
        setMsg("detailMsg", "Balances checked, but DexScreener isn't answering, so the saved snapshot was kept.", "error");
      } else setMsg("detailMsg", "Updated from the chain just now.", "ok");
      if (live) load();
    } catch (e) {
      if (openWallet !== wallet) return;
      if (e.status === 401) return showSignedOut("Your admin sign-in expired. Connect again.");
      if (e.status === 403) return recheck("Connect again.");
      setMsg("detailMsg", e.message, "error");
    } finally {
      $("detailLive").removeAttribute("aria-busy");
    }
  }

  function renderDetail({ user, watchlist }) {
    renderedWallet = user.wallet;
    const facts = [
      ["First seen", new Date(user.first_seen * 1000).toLocaleString()],
      ["Last seen", new Date(user.last_seen * 1000).toLocaleString()],
      ["Sign-ins", String(user.sign_ins)],
      ["Portfolio", user.value_usd == null ? "not checked" : W.usd(user.value_usd)],
      ["Coins", user.token_count == null ? "—" : String(user.token_count)],
      ["Last check", user.checked_at ? W.ago(user.checked_at, serverNow) : "never"],
    ];
    $("detailFacts").replaceChildren(...facts.map(([k, v]) => el("div", {}, el("dt", { text: k }), el("dd", { text: v }))));

    const rows = (user.holdings || []).map((h) => el("a", { class: "coin-row", href: W.dexUrl(h), target: "_blank", rel: "noopener noreferrer" },
      el("div", { class: "c-coin coin-cell" },
        W.coinLogo(h),
        el("div", { class: "coin-name" },
          el("strong", { text: h.name || h.symbol || W.short(h.mint) }),
          el("span", { text: `${W.amount(h.amount)} ${h.symbol || W.short(h.mint)}` }))),
      el("span", { class: "c-price", text: W.usd(h.price_usd) }),
      el("span", { class: "c-change", text: "" }),
      el("span", { class: "c-value", text: W.usd(h.value_usd) })));
    $("detailHoldings").replaceChildren(...(rows.length ? rows : [el("p", { class: "empty", text: user.checked_at ? "No coins at the last check." : "Not checked yet. Press Refresh live." })]));

    $("detailWatch").replaceChildren(...(watchlist.length
      ? watchlist.map((m) => el("li", {}, el("a", { href: `https://dexscreener.com/solana/${encodeURIComponent(m)}`, target: "_blank", rel: "noopener noreferrer", text: m })))
      : [el("li", { class: "mute", text: "Empty" })]));
  }

  $("detailLive").addEventListener("click", () => {
    if (openWallet && !$("detailLive").hasAttribute("aria-busy")) openDetail(openWallet, true);
  });
  $("detailClose").addEventListener("click", () => $("detail").close());
  $("detail").addEventListener("close", () => {
    // The list may have been redrawn while the dialog was open: return focus to this wallet's row.
    const wallet = openWallet;
    openWallet = null;
    const button = wallet && [...$("rows").querySelectorAll("button[data-wallet]")].find((b) => b.dataset.wallet === wallet);
    if (button) button.focus();
  });
  $("detailCopy").addEventListener("click", async () => {
    const text = $("detailAddr").textContent;
    try {
      await navigator.clipboard.writeText(text);
      setMsg("detailMsg", "Address copied.", "ok");
    } catch {
      const range = document.createRange();
      range.selectNodeContents($("detailAddr"));
      getSelection().removeAllRanges();
      getSelection().addRange(range);
      setMsg("detailMsg", "Press Ctrl+C (or Cmd+C) to copy the selected address.");
    }
  });

  // ---------- start ----------

  (async () => {
    let who = null;
    try { who = await W.me(); } catch { who = null; }
    if (who) signedIn(who);
    else await showSignedOut();

    await W.waitForProvider();
    W.onAccountChange(async (wallet) => {
      if (!me || wallet === me.wallet) return;
      try {
        await W.signOut();
        showSignedOut("You switched wallets in Phantom. Connect again.");
      } catch (e) {
        setMsg($("notAdmin").hidden ? "appMsg" : "notAdminMsg",
          `You switched wallets in Phantom, but signing out failed (${e.message}). Press Sign out to try again.`, "error");
      }
    });
  })();
})();

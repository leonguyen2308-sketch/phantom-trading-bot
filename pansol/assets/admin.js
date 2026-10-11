// Admin page: sign in with an admin wallet, then list every tracker user.
// The server refuses /api/admin/* for any other wallet; this page only displays what it is given.
(() => {
  const W = window.PansolWallet;
  const { el } = W;
  const $ = (id) => document.getElementById(id);
  const REFRESH_MS = 30000;

  let me = null;
  let users = [];
  let serverNow = Date.now() / 1000;
  let timer = 0;
  let openWallet = null;

  function show(view) {
    for (const id of ["loading", "signedOut", "notAdmin", "app"]) $(id).hidden = id !== view;
    $("account").hidden = view !== "app" && view !== "notAdmin";
  }

  function setMsg(id, text, kind = "") {
    $(id).textContent = text || "";
    $(id).className = `msg${kind ? " " + kind : ""}`;
  }

  // ---------- sign in ----------

  async function showSignedOut(message = "") {
    stop();
    me = null;
    users = [];
    $("rows").replaceChildren();
    if ($("detail").open) $("detail").close();
    show("signedOut");
    setMsg("signInMsg", message, message ? "error" : "");
    const p = await W.waitForProvider();
    $("connect").hidden = !p;
    $("openInPhantom").hidden = !!p || !W.isMobile();
    $("getPhantom").hidden = !!p || W.isMobile();
    if (!p) {
      $("openInPhantom").href = W.openInPhantomUrl();
      setMsg("signInMsg", W.isMobile()
        ? "On your phone, open this page inside the Phantom app."
        : "Phantom isn't installed in this browser.");
    }
  }

  function signedIn(who) {
    me = who;
    $("walletShort").textContent = W.short(me.wallet);
    $("walletShort").title = me.wallet;
    if (!me.isAdmin) {
      stop();
      $("notAdminWallet").textContent = me.wallet;
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

  const signOut = async () => { try { await W.signOut(); } finally { showSignedOut(); } };
  $("signOut").addEventListener("click", signOut);
  $("switchWallet").addEventListener("click", signOut);

  function stop() {
    clearInterval(timer);
    timer = 0;
  }

  // ---------- user list ----------

  async function load() {
    $("refresh").disabled = true;
    try {
      const data = await W.api("/api/admin/users");
      users = data.users;
      serverNow = data.now;
      renderStats(data.stats);
      renderRows();
      setMsg("appMsg", "");
    } catch (e) {
      if (e.status === 401) return showSignedOut("Your admin sign-in expired. Connect again.");
      if (e.status === 403) return signedIn({ ...me, isAdmin: false });
      setMsg("appMsg", e.message, "error");
    } finally {
      $("refresh").disabled = false;
    }
  }
  $("refresh").addEventListener("click", load);
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
        el("button", { class: "wallet-btn", type: "button", title: u.wallet, text: W.short(u.wallet), onclick: () => openDetail(u.wallet) }),
        u.is_admin ? el("span", { class: "badge", text: "admin" }) : null),
      el("td", {}, when(u.first_seen)),
      el("td", {}, when(u.last_seen)),
      el("td", { class: "num", text: String(u.sign_ins) }),
      el("td", { class: "num", text: u.value_usd == null ? "not checked" : W.usd(u.value_usd) }),
      el("td", { class: "num", text: u.token_count == null ? "—" : String(u.token_count) }),
      el("td", { class: "num", text: String(u.watch_count) }),
    )));
    $("empty").hidden = users.length > 0;
    if (users.length && !list.length) setMsg("appMsg", "No wallet matches that search.");
    else if ($("appMsg").textContent === "No wallet matches that search.") setMsg("appMsg", "");
  }
  $("search").addEventListener("input", renderRows);
  $("sort").addEventListener("change", renderRows);

  // ---------- one user ----------

  async function openDetail(wallet, live = false) {
    openWallet = wallet;
    $("detailAddr").textContent = wallet;
    $("detailSolscan").href = `https://solscan.io/account/${encodeURIComponent(wallet)}`;
    if (!$("detail").open) $("detail").showModal();
    setMsg("detailMsg", live ? "Checking the wallet on Solana…" : "Loading…");
    $("detailLive").disabled = true;
    try {
      const data = await W.api(`/api/admin/users/${encodeURIComponent(wallet)}${live ? "?live=1" : ""}`);
      if (openWallet !== wallet) return;
      renderDetail(data);
      setMsg("detailMsg", live ? "Updated from the chain just now." : "", live ? "ok" : "");
      if (live) load();
    } catch (e) {
      if (e.status === 401) { $("detail").close(); return showSignedOut("Your admin sign-in expired. Connect again."); }
      setMsg("detailMsg", e.message, "error");
    } finally {
      $("detailLive").disabled = false;
    }
  }

  function renderDetail({ user, watchlist }) {
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

  $("detailLive").addEventListener("click", () => openWallet && openDetail(openWallet, true));
  $("detailClose").addEventListener("click", () => $("detail").close());
  $("detail").addEventListener("close", () => { openWallet = null; });
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
      try { await W.signOut(); } finally {
        showSignedOut("You switched wallets in Phantom. Connect again.");
      }
    });
  })();
})();

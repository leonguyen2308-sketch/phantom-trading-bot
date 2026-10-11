# Pansol site

Three pages served by one small Python server:

| Page | Who | What |
|---|---|---|
| `/` | everyone | Launch page: the Pansol coin, a live countdown to 15 October 2026, and the Join button |
| `/tracker` | everyone | **Meme Tracker**: connect Phantom, see every coin in your wallet with live prices, keep a watchlist |
| `/admin` | admin wallets only | Every tracker user: wallet, first and last visit, sign-ins, portfolio value, coins, watchlist, CSV export |

## How sign-in and the admin view work

1. A visitor presses **Connect Phantom**. Phantom asks them to connect, then to **sign a message**.
   The message is text, not a transaction, so it costs nothing and cannot move coins.
   This app never asks a wallet to sign a transaction.
2. The server checks the signature, records the wallet and gives the browser a session cookie.
3. The tracker shows that wallet's coins (balances from Solana, prices from DexScreener) and its watchlist.
   Each user only ever gets their own data.
4. Wallets listed in `PANSOL_ADMIN_WALLETS` can open `/admin`. The server checks this on every admin
   request, so any other wallet gets "not an admin" and no data at all.

What is stored, and what the tracker page tells users: wallet address, first and last visit, number of
sign-ins, the coins in the wallet at the last check and the watchlist. Wallet balances are public on Solana
anyway. Nothing else is collected: no IP addresses, no emails, no keys.

## Run it on your Mac

```bash
uv run python -m pansol           # http://127.0.0.1:8080, /tracker, /admin
```

Settings go in `.env` (see the Pansol section of `.env.example`). The one you must set:

```
PANSOL_ADMIN_WALLETS=YourPhantomWalletAddress
```

Copy it from Phantom (tap the account name, then copy the Solana address). Several admins: separate with commas.

| Setting | Default | Notes |
|---|---|---|
| `PANSOL_ADMIN_WALLETS` | none | Who can open `/admin` |
| `SOLANA_RPC_URL` | public Solana RPC | The public one is slow and rate-limited. A free [Helius](https://www.helius.dev) URL is much better |
| `PANSOL_PUBLIC_URL` | none | The address(es) people open, e.g. `https://pansol.xyz,https://www.pansol.xyz`. Sign-in only works on these (on Railway and Render the platform's own address is added for you). With none set, sign-in only works on `localhost` |
| `PANSOL_DB` | `state/pansol.db` | The SQLite file with users and watchlists. Back it up |
| `PANSOL_MINT` | none | After launch: Pansol's token address, so every tracker shows its live price |
| `HOST`, `PORT` | `127.0.0.1`, `8080` | Use `HOST=0.0.0.0` on a server |

## Put it online (Railway)

The tracker needs a server and a disk, so a static host like Netlify Drop is no longer enough.

1. On [railway.com](https://railway.com): **New Project → Deploy from GitHub repo**, pick this repo and branch.
2. **Variables**: `RAILWAY_DOCKERFILE_PATH=pansol/Dockerfile`, `PANSOL_ADMIN_WALLETS=<your address>`,
   `SOLANA_RPC_URL=<your Helius URL>`.
3. **Add a Volume** mounted at `/data` (this keeps the user list across deploys).
4. **Settings → Networking → Generate Domain**, then redeploy once so the app learns its address.
   Open it, then `/admin`, and connect your admin wallet.
5. With your own domain (e.g. `pansol.xyz`), add `PANSOL_PUBLIC_URL=https://pansol.xyz` so sign-in works there too.

Any host that runs a Dockerfile with a persistent disk works the same way (Render with a disk, Fly.io with a
volume). Elsewhere, always set `PANSOL_PUBLIC_URL`: the sign-in message names the site, and Phantom checks
that name against the page asking for the signature, which is what stops a copycat site from reusing it.

On phones, the Connect button only works inside Phantom's own browser. The tracker shows visitors an
**Open in Phantom** button that does that for them.

## Launch page details

- **Countdown** counts calendar days on Vietnam time (UTC+7): 7 on 8 Oct, 6 on 9 Oct, ... "Today is launch day"
  on 15 Oct, "Live on Solana" after that. Change it in `CONFIG` at the top of the script in `index.html`.
- **Join counter** uses the free [Abacus](https://abacus.jasoncameron.dev) counting API. Pressing Join opens X and
  adds 1, once per browser. It counts presses, not X followers, and anyone can inflate a public counter.

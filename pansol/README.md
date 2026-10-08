# Pansol launch page

One static page: the Pansol coin, a live countdown to launch day (15 October 2026), and a
**Join** button that opens the Pansol X account and adds one to a shared "people joined" counter.

```
pansol/
  index.html              the whole page (HTML, CSS, JS)
  assets/pansol-coin.jpg  the coin, unchanged
  assets/pansol-gym.jpg   the gym panda
```

## See it locally

```bash
cd pansol && python3 -m http.server 8080    # then open http://localhost:8080
```

## Put it online

Any static host works. Point it at the `pansol` folder:

- **Netlify Drop**: drag the `pansol` folder onto https://app.netlify.com/drop.
- **Vercel / Cloudflare Pages / Netlify (from GitHub)**: import this repo, no build command,
  output (publish) directory `pansol`.

## How it works

- **Countdown** counts calendar days on Vietnam time (UTC+7): 7 on 8 Oct, 6 on 9 Oct, ...
  1 on 14 Oct, "Today is launch day" on 15 Oct, "Live on Solana" after that. The clock under
  the number ticks down to midnight, when the day count drops by one.
- **Join counter** uses the free [Abacus](https://abacus.jasoncameron.dev) counting API. Pressing
  Join opens X in a new tab and adds 1, once per browser. The number refreshes every 15 seconds,
  so every visitor sees the same live total. It counts Join presses, not X followers, and like
  any public counter it can be inflated by someone calling the API directly.

## Change settings

Edit `CONFIG` at the top of the `<script>` in `index.html`: launch date, time zone offset, and
the counter name. The X link is the `href` on the two `data-join` buttons and in the
"Community" row.

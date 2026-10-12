# Phantom trading bot

Trades BTC automatically from a dedicated Phantom (Solana) wallet. **It starts in paper mode and
refuses real money until every go-live check passes.**

- **Jev** scores every 15-minute candle (six fixed-outcome questions, numbers back, no text).
- **Code** decides: the breakout rule, every threshold, position size, every risk limit, every order.
- **Jupiter** executes swaps USDC ↔ cbBTC on Solana, signed by the bot wallet's own key.
- **Dashboard** at `http://localhost:8000`; **Telegram** alerts with `/status` and `/kill`.

The strategy is in [`strategy.md`](strategy.md). It is **not validated yet**.

## 1. Set up (MacBook)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh     # once; then open a new Terminal window
git clone -b claude/chat-session-6gnbjh https://github.com/leonguyen2308-sketch/phantom-trading-bot
cd phantom-trading-bot
./run.sh                                            # creates .env and stops
open -e .env                                        # add TYPESAFE_API_KEY (+ Telegram if you want alerts)
./run.sh jev-test                                   # one real Jev call to check the key
```

## 2. Backtest, then paper trade

```bash
./run.sh backtest                 # rules only, 2 years, graded against the gate
./run.sh backtest --jev           # with Jev (answers cached in state/)
./run.sh test-kill                # fire the kill switch through every trigger, on paper
./run.sh approve-limits           # review and lock config/risk.yaml
./run.sh                          # paper trading + dashboard at http://localhost:8000
scripts/install-mac-service.sh    # keep it running: starts at login, restarts on crash
```

Before trusting Jev in a backtest, set `jev_training_cutoff` in `config/strategy.yaml`. Only data
after that date counts as out of sample. The last 90 days stay locked until you run
`./run.sh backtest --jev --holdout`, and every such run is logged.

## 3. Go live (only when every check is clean)

```bash
./run.sh golive-check
```

It answers, from real evidence on disk:
1. Did this strategy version pass the backtest gate out of sample, and on the holdout?
2. Does paper trading match the backtest? (14+ days, 20+ trades, hit rate within 10 points)
3. Did the kill switch fire in testing through every trigger?
4. Is every hard limit enforced in code, unchanged since you approved it?
5. Is Jev's confidence calibrated on your own decisions?
6. Which market regime would break this?
7. WHAT COULD BLOW UP THIS ACCOUNT? (`./run.sh ack-risks`)

When all pass:
1. In Phantom, **create a new wallet** for the bot. Never use your main wallet.
2. Send it a small amount of USDC and about 0.05 SOL for network fees.
3. In Phantom: Settings → that wallet → Show Private Key. Put it in `.env` as `PHANTOM_BOT_PRIVATE_KEY`,
   and the wallet address as `PHANTOM_BOT_ADDRESS`. Never paste the key anywhere else.
4. Add `JUPITER_API_KEY` and `SOLANA_RPC_URL`, then run `./run.sh wallet-check`. It shows balances and a
   quote and sends nothing.
5. Set `LIVE_TRADING=yes`, then run `./run.sh live` (or `scripts/install-mac-service.sh live`).

## Kill switch

Fires by itself on a 10% drawdown, 5 API errors in a row, or the wallet balance not matching the bot's
position. You can fire it with `/kill` on Telegram, the dashboard button, or `./run.sh kill`. It sells the
position and halts. Only `./run.sh reset` (typing RESET) turns trading back on.

## Hard limits (`config/risk.yaml`)

| Limit | Default |
|---|---|
| Max position | 20% of the wallet, and $100 |
| Max loss per trade at the stop | 0.5% |
| Daily loss | 2%: no new entries until 00:00 UTC |
| Max drawdown | 10%: kill switch |
| Swap price vs Coinbase | 75 bps, else refused |
| Price impact | 50 bps, else refused |

## WHAT COULD BLOW UP THIS ACCOUNT?

- **The bot wallet's key leaks.** Anyone with it can empty the wallet. Keep only what you can lose there.
- **Your Mac sleeps or goes offline while holding BTC.** Stops live in the bot, not on-chain, so nothing sells until it's back.
- **BTC gaps through the stop.** In a crash the sell fills far below it.
- **Jupiter, Solana or your RPC is down or congested.** Swaps fail or land late.
- **cbBTC loses its peg** (custodian problem), or **USDC depegs**.
- **The strategy was overfit** and has no real edge.
- **Jev drifts out of calibration** and Kelly sizes up on false confidence.
- **A bug.** This code is new and has only been tested on paper and simulated data.

## Layout

```
phantom/features.py     snapshot of closed candles (no look-ahead; leak-tested)
phantom/jev.py          six Jev questions, one call per candle, cached
phantom/strategy.py     breakout rule, thresholds, weights, exits
phantom/sizing.py       capped quarter-Kelly
phantom/risk.py         hard limits and the kill switch
phantom/calibration.py  Brier, reliability, isotonic recalibration
phantom/backtest.py     harness, metrics, gate (Sharpe, DD, hit rate, Newey-West t, deflated Sharpe)
phantom/bot.py          the trading loop
phantom/execution/      paper broker and Jupiter (Solana) broker
phantom/golive.py       the final check
dashboard/index.html    the dashboard (sample data when opened on its own)
```

Run the tests with `uv run --group dev pytest`.

## Post trending coins to X (optional)

Separate from trading: every hour it posts a trending coin with its contract address (CA), market
cap, 24h volume, price change and a chart link. Data comes from GeckoTerminal (free, no key).

```bash
./run.sh trending --once --dry-run          # preview, posts nothing
./run.sh trending                           # post every TRENDING_EVERY_MIN minutes
scripts/install-mac-service.sh trending     # keep it running on your Mac
```

Settings are the `X_*` and `TRENDING_*` lines in `.env`. Each coin is posted at most once a day.

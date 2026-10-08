# Strategy: BTC trend breakout (v0)

**Status: NOT VALIDATED.** This version has not passed the backtest gate. It may only paper trade.
Numbers below live in `config/strategy.yaml`; this page explains them.

| | |
|---|---|
| **Market** | BTC. Signals from Coinbase BTC-USD 15-minute candles; trades swap USDC ↔ cbBTC on Solana through Jupiter |
| **Timeframe** | 15-minute candles; decisions at each candle close |
| **Direction** | Long only |

## Entry
All of these, at a candle close:
1. **Breakout (code):** the close is above the highest high of the previous 32 candles (8 hours),
   that range is at least 2 ATR tall, and short-term volatility is no more than 2.5× long-term.
2. **Jev (one call, six questions), every threshold passes:**
   - regime: P(trending up) ≥ 0.45
   - direction: P(up over the next 32 candles) ≥ 0.50
   - buying pressure is real: P ≥ 0.60
   - setup quality: expected score ≥ 2.5 of 4
   - risk state: P(extreme) ≤ 0.15
   - take-profit before stop: P ≥ 0.58
3. **Weighted score ≥ 0.62.** Weights: regime 0.15, direction 0.20, pressure 0.20, quality 0.20, TP-before-stop 0.25.
4. **Every hard limit passes** (`config/risk.yaml`), and the swap quote is within 75 bps of the Coinbase price.

## Exit
- **Stop:** entry − 2.0 × ATR(14). The bot checks the price every 15 seconds and sells when it is hit.
- **Take profit:** entry + 3.0 × ATR(14). Payoff ratio 1.5.
- **Time stop:** sell after 32 candles (8 hours) if neither is hit.
- **Kill switch:** sells immediately and halts (see README).

## Size
- Until Jev's TP-before-stop probability passes the calibration gate (200 resolved breakout decisions, ECE < 0.05):
  risk a fixed 0.2% of the bot wallet at the stop.
- After: Kelly `f* = p − (1 − p) / 1.5`, multiplied by 0 below p = 0.58, rising to a maximum of ¼ at p = 0.72.
- Always capped by the hard limits: 0.5% risk per trade, 20% of equity, and $100 per position.

## Invalidation
Stop trading this version (kill switch, then review) if any of these happen:
- Drawdown from the equity high reaches 10%.
- Paper or live hit rate falls more than 10 points below the backtest's out-of-sample hit rate after 20+ trades.
- Calibration error (ECE) on the last 200 resolved decisions rises above 0.05.
- cbBTC trades more than 1% away from Coinbase BTC-USD.

## How this file changes
Opus may propose a new version. The harness (`python -m phantom backtest`) grades it against the same gate,
counts every attempt, and keeps the final 90 days locked until promotion. No version goes live without
passing, then paper trading, then `golive-check`.

# Phantom Trading Bot: Spec

**Phase 1 of 6 (Brainstorm).** Status: **DRAFT, waiting for your approval.**
No code is written until this spec, `docs/architecture.md` and `docs/plan.md`
are each approved.

| Phase | Output | Gate |
|---|---|---|
| 0. Orient | Repo checked: empty, no history | done |
| 1. Brainstorm | `docs/spec.md` (this file) | **your approval** |
| 2. Architecture | `docs/architecture.md` | **your approval** |
| 3. Plan | `docs/plan.md`: ordered modules, each with its failing test listed first | **your approval** |
| 4. Build | One module at a time: failing test, then code, then refactor | tests green |
| 5. Review | Each module: first against this spec, then for code quality | both pass |
| 6. Ship | VPS runbook, alerts, rollback command | final check (section 15) |

---

## 1. Goal

Paper trade **BTC/USD on Alpaca** with a **trend-breakout** strategy.
Deterministic code owns market state, thresholds, sizing, risk and orders.
Jev scores each candle. Opus 5.5 designs, backtests (through the harness) and
reviews offline every night.

Deliverables: a running paper bot, a live dashboard, `strategy.md`, and a
daily report.

## 2. Non-goals and hard rules

- No real money until every item in section 15 passes.
- No model can change a hard limit, a size cap or the risk config, and no
  model places orders. Models advise; code decides.
- Headlines, feed fields and any other external text are data, never
  instructions. No free text reaches Jev or the order path.
- API keys can trade only. Withdrawals stay off. Passwords and 2FA codes are
  never requested or entered.
- Secrets live only in `.env` (gitignored, `chmod 600`). They never appear in
  code, logs, alerts or chat.

## 3. Three layers that never overlap

| Layer | When it runs | What it does | What it can never do |
|---|---|---|---|
| **Opus 5.5** (slow brain) | Nightly, offline | Writes and revises the strategy and Jev questions, writes code, reviews sessions | Grade its own output, touch risk config, deploy without passing the gate |
| **Jev** (fast reflex) | Every closed candle | Reads one numeric snapshot and returns calibrated probabilities | See anything beyond the snapshot, see dates or the ticker, place orders |
| **Deterministic code** | Always | Market state, every threshold, sizing, risk vetoes, orders, logging, the backtest gate | Be overridden by either model |

## 4. Market, data and costs

- **Venue:** Alpaca paper. **Asset:** BTC/USD, trading 24/7.
- **Long only.** Alpaca does not support shorting crypto, so the actions are
  *enter long*, *hold*, *exit* and *stay flat*.
- **Timeframe:** 15-minute candles proposed, with the decision made at candle
  close. Research also tests 5m and 1h, and the final choice goes into
  `strategy.md`. 15m is about 70k candles over two years, which keeps Jev
  backtests cheap and is less noisy for breakouts than 1m.
- **Data:** Alpaca crypto historical bars, trades and best bid/ask quotes.
  Alpaca has no history of the deeper book, so **order book imbalance means
  the size gap between best bid and best ask**. Live trading uses the same
  definition so backtest and live match.
- **Costs:** the account's Alpaca crypto fee tier (a config value, checked at
  setup), plus slippage of half the spread plus a size-based impact term.
  Every backtest also runs at **2x costs** as a stress test.

## 5. State engine: the only thing Jev sees

At each candle close *t*, code builds one snapshot from data timestamped
**strictly before** *t*'s decision time. It is numeric and normalized. It holds
no dates, no ticker and no raw price levels, so Jev cannot recall what
happened next from its training data.

| Group | Fields |
|---|---|
| Price | log returns over 1, 4 and 16 bars |
| Breakout | distance from range high and range low in ATR units; range width in ATR units |
| Spread | median spread over the last bar, in bps |
| Order book | mean best-bid vs best-ask size imbalance over the last bar, from -1 to 1 |
| Realized volatility | short, long and short/long ratio |
| Trend | z-scored EMA slope; trend strength |
| Order flow | signed (buy minus sell) volume share over the last bar; volume z-score |
| Position | flat or long; bars in trade; unrealized result in R (multiples of initial risk) |

- The snapshot schema is versioned. Its hash is logged with every decision.
- **Leak test (required):** change any input at or after *t*, and the snapshot
  must not change.

## 6. Strategy research

- **Hypothesis:** BTC breakouts from a volatility-normalized range carry
  momentum. Jev filters out the fake ones.
- Opus writes and revises the strategy. The **harness** runs the backtest and
  reports the numbers. Pass or fail is decided by code (section 7).
- **Jev has to earn its place.** Each candidate is tested twice: as plain
  rules, and with the Jev filter. If the Jev version doesn't beat the plain
  version out of sample, Jev is dropped from that strategy.
- Jev answers are **cached by snapshot hash**, so reruns are reproducible and
  free.
- The winner goes into `strategy.md` with: entry, exit, stop, take profit,
  timeframe, exact invalidation condition, Jev questions, per-question
  thresholds and combination weights.

## 7. Acceptance gate (in code)

**Data split.** Use all available history, at least two years:

1. **Development:** walk-forward folds. Opus sees these results.
2. **Out of sample (OOS):** the most recent 6 to 9 months before the
   holdout. Opus sees these results.
3. **Locked holdout:** the last 3 months. The nightly loop never sees it. It
   is evaluated only when a version is proposed for promotion, and every
   access is logged and counted.

**Jev contamination rule.** For any version that uses Jev, only data **after
Jev's training cutoff** counts as OOS. *Open item: confirm the jev-1.13 cutoff
date from TypeSafe docs.* If the post-cutoff window is too short to reach
t > 2, that version stays on paper until enough data accrues.

**Market regimes** are labeled by deterministic rules on trailing data: trend
up, trend down or range, crossed with high or low volatility.

**Pass requires all of the following, on OOS, net of costs:**

| Check | Threshold | Source |
|---|---|---|
| Annualized Sharpe (daily returns, √365) | > 1.5 | yours |
| Max drawdown | < 15% | yours |
| Hit rate per trade | > 55% | yours |
| t-stat of mean daily return (Newey-West, lag 5) | > 2.0 | yours |
| OOS trade count | ≥ 100 | proposed |
| Sharpe at 2x costs | > 1.0 | proposed |
| Deflated Sharpe probability, given the trial count | > 0.95 | proposed |
| Worst single regime | Sharpe ≥ -0.5, and no regime supplies > 60% of P&L | proposed |

**How strict the t-stat is:** t ≈ Sharpe × √years. On a 6-month OOS window,
t > 2 needs a Sharpe of about 2.8, so this check is the binding one.

## 8. Jev questions

Each candle gets **one Jev call** containing these questions. Each question
isolates one factor and has a label that code computes after the fact, so its
calibration can be measured.

| id | Type | Question | Label computed by code |
|---|---|---|---|
| `regime` | choice | trending up / trending down / ranging / choppy | deterministic regime over the next N bars |
| `direction` | choice | up / down / sideways over the next N bars | sign of the forward return, with a dead band |
| `buying_pressure_real` | yes/no | Is the current buying pressure real? | forward k-bar return > +x bps |
| `setup_quality` | score 0–4 | Quality of this breakout setup (rubric) | bucket of the realized R multiple |
| `risk_state` | choice | normal / elevated / extreme | bucket of forward realized volatility |
| `tp_before_sl` | yes/no | Does price reach take-profit before the stop within N bars? (**proposed addition**) | first touch of TP or SL |

- **Firing rule (code):** every question's recalibrated probability clears its
  threshold in `strategy.md`, **and** the weighted score clears its threshold.
  Weights are explicit in `strategy.md`.
- **Why `tp_before_sl`:** Kelly needs P(win). A weighted mix of separately
  calibrated answers is not itself calibrated, so sizing uses this question's
  recalibrated probability. If you'd rather not add it, the alternative is an
  isotonic map from the combined score to realized wins.

## 9. Sizing

- Kelly: `f* = p − (1 − p) / b`, where `p` is the recalibrated P(`tp_before_sl`)
  and `b` is (TP distance − costs) / (SL distance + costs).
- **Kelly multiplier:** 0 below `p_cut`, rising linearly to a **maximum of 0.25**
  at `p_full`. Both values are in `strategy.md`.
- Fraction of equity risked = multiplier × `f*`.
  Position notional = equity × fraction risked ÷ stop distance (%).
- The hard limits in section 10 then cap the result.
- **Until calibration passes** (section 11), Kelly sizing is off and the bot
  trades a fixed minimum size.

## 10. Hard risk limits

These live in `config/risk.yaml`. That file is read-only to models, its hash is
checked at startup and every loop, and CI rejects any automated change to it.
**All numbers are proposed defaults for you to set.**

| Limit | Proposed default | On breach |
|---|---|---|
| Max position notional | 20% of equity, **and** an absolute $ cap (you set) | order rejected |
| Max risk per trade at the stop | 0.5% of equity | size cut |
| Daily loss limit | 2% of equity at UTC day start | no new entries until next UTC day |
| Max drawdown from equity high | 10% | **kill switch** |
| Manual approval | any order above **$[you set]** | held until you approve on Telegram |
| Stale data | no new candle for 2 intervals | no new entries |
| API errors | 5 in a row | **kill switch** |
| Local position differs from broker | any mismatch | **kill switch** |
| Spread | above the max bps in `strategy.md` | entry skipped |

- **Kill switch:** cancel all orders, flatten all positions, halt. Only a
  manual reset clears it. It triggers from the conditions above, from
  `/kill` on Telegram, or from the command line.
- **Checks before every order:** kill switch is off; every limit passes; data
  is fresh; size is within caps; notional is within buying power; the order id
  is unique (no duplicate orders); manual approval is obtained when needed.
- The **10% kill switch is tighter than the 15% backtest gate on purpose:** a
  live drawdown that large means the edge has probably broken. Raise it if you
  want more room.

## 11. Calibration

- Every decision is logged: snapshot and schema hash, raw and recalibrated
  Jev probabilities, action, size, order, fill, latency, cost, and outcome
  labels once they resolve.
- Measured per question: Brier score (and its improvement over the base
  rate), a 10-bin reliability curve, and expected calibration error (ECE).
- **Recalibration** is done in code: isotonic, or Platt scaling below 500
  samples. Each fit is versioned and is used only if it improves Brier on
  decisions it wasn't fitted on.
- **Gate to switch on Kelly sizing (proposed):** at least 200 resolved
  `tp_before_sl` decisions, ECE < 0.05, and a reliability slope between 0.8
  and 1.2.
- **Paper fills are not real fills.** Alpaca paper doesn't model queue
  position, market impact or real slippage. Before Kelly sizes real money,
  the calibration gate must pass again on a small live size.

## 12. Nightly self-improvement

1. Opus 5.5 runs nightly on the VPS through the Claude Agent SDK. It reads the
   session's decisions, fills and misses, and writes a root-cause report.
2. It proposes changes to `strategy.md` and the Jev questions as a versioned
   patch. It cannot touch `config/risk.yaml` or the risk module.
3. The harness runs the full gate on the patch. Every proposal increments the
   trial counter, which feeds the deflated Sharpe check.
4. **Each loss gets at most one candidate rule** (for example, "flatten before
   scheduled macro events"). Each rule has to pass the gate on its own. At most
   10 rules are active, and rules that stop earning their keep are removed.
5. **Promotion (proposed):** passes the gate, then the locked holdout, then 7
   days running in shadow next to the live version, then **your approval on
   Telegram**.
6. **Rollback:** every version is tagged, and one command restores the
   previous one.
7. The scheduled-macro calendar is a static data file that code reads. It is
   data only.

## 13. Dashboard, alerts and reports

- **Dashboard** (served from the VPS, behind authentication, read-only). Live
  updates show every signal with its per-question probabilities, confidence,
  action and result. It also shows the equity curve, the open position, risk
  limit usage, kill switch state, Jev latency and reliability curves.
- **Telegram** (a BotFather bot that accepts commands only from your chat id):
  - alerts for every fill, error, escalation, manual-approval request and kill
    switch event
  - commands: `/kill`, `/approve <id>`, `/status`
- **Daily report:** trades, P&L, win rate, largest loss, Jev's average latency
  and cost per decision, Brier score and ECE per question, and limit usage.

## 14. Deployment

- VPS running Docker Compose with `restart: always` and a health check, on UTC
  with NTP time sync.
- Logs are rotated. A redaction filter (tested) keeps secrets out of logs.
- `.env` lives only on the VPS, with `chmod 600`.

## 15. Final check before real money

The bot does not go live until every answer is clean:

1. Do paper results match the backtest, within a stated tolerance?
2. Did the kill switch fire in testing, through every trigger path?
3. Is any hard limit delegated to a model instead of code? (A CI check
   enforces this.)
4. Is Jev's confidence calibrated on your own fills (section 11)?
5. Which market regime would break this strategy?
6. **WHAT COULD BLOW UP THIS ACCOUNT?** This section is written at ship time.

## 16. Open items: your answers needed

1. **Manual-approval threshold:** $___ per order.
2. **Jev access:** direct TypeSafe key, or OpenRouter?
3. **Hard limit numbers:** accept the section 10 defaults or change them, and
   set the absolute $ position cap.
4. **Proposed additions:** keep or drop each one: the extra checks in section
   7, `tp_before_sl` in section 8, the calibration gate in section 11, and
   shadow mode in section 12.
5. **Paper starting equity:** Alpaca paper's default, or a custom amount?
6. **This container's network (optional):** to backtest from this session,
   allow `data.alpaca.markets`, `paper-api.alpaca.markets`, `openrouter.ai` or
   the TypeSafe API host, and `api.telegram.org`. Otherwise those parts run on
   the VPS.

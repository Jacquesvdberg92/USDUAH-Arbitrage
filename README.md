# USDUAH-Arbitrage

Arbitrage on Binance spot. The repo has two separate tools:

- `arbitrage_bot/`: a triangular-arbitrage bot (described below). It has a paper-trading
  **test mode**, risk limits and a live browser dashboard.
- `sol_arbitrage.py`: a single-script SOL arbitrage across USDT/FDUSD/USDC with a Tkinter
  window. It reads its API keys from `key.py`; `arbitrage_bot` doesn't use `key.py` at all.

If a configured market has been suspended on Binance (status `BREAK`), the bot says so at
startup and skips that triangle.

## What the bot does

Every `poll_interval_sec` it:

1. **Screens** every configured triangle, in both directions, using one `bookTicker` request.
   It computes the top-of-book return after fees. For profitable cycles that's an upper bound
   on the real profit, so cycles that can't make the threshold are skipped without any further
   API calls.
2. **Plans** the promising ones against the full order books, fetched in parallel. It walks
   the depth, applies taker fees, and rounds every quantity to Binance's `LOT_SIZE` / `NOTIONAL`
   rules exactly as the order will be sent. It also values the rounding leftovers ("dust"), and
   tries a range of trade sizes to find the one with the largest profit that still clears both
   `min_profit_bps` and `min_profit_abs`.
3. **Executes** the plan:
   - **Leg 1** is a `LIMIT IOC` order at the planned worst price. If the opportunity has already
     gone, it simply doesn't fill and you hold nothing.
   - **Legs 2 and 3** are each sized from what the previous leg *actually* returned after fees.
     Each tries an IOC at the planned price first, then sends a market order for any remainder,
     because once you hold an intermediate currency you need to get back to the home asset.
   - If a later leg fails, the position is **unwound** straight back to the home asset.
4. **Tracks** realised P&L, dust and stats. It writes every cycle to `trades.jsonl` and converts
   dust back to the home asset once there's enough to trade. Fees charged in BNB are valued in
   the home asset and count against P&L and the risk limits too.

**When an order's fate is unknown.** A timeout, an HTTP 5xx or Binance error -1006/-1007 does
*not* mean the order failed: it may have executed. Every order carries its own client order ID.
If one of those errors happens, the bot looks the order up and uses its real fills. If Binance
still can't say what happened, the bot stops instead of guessing.

**Risk limits.** The bot stops on any of:
- a cumulative loss limit
- N losing cycles in a row
- N API errors in a row (with backoff, and honouring `Retry-After`)
- N rejected orders in a row
- an API key without trading permission
- `max_cycles`
- a position it couldn't unwind, or an order whose outcome it couldn't determine

Trade size is also capped by `max_trade` and by a fraction of your balance. Any cycle that
ends a run early is still written to `trades.jsonl`.

**Stopping it.** Pressing Ctrl+C while orders are in flight lets the current cycle (and its
bookkeeping) finish first, so no position is left half-done. Pressing it again forces an exit; the
cycle is then logged as `interrupted` with whatever it still held. The process exits with code `2`
when it stopped for a reason that needs a human, so a supervisor such as systemd or cron can alert
you. Those reasons are: loss limit, stuck position, unknown order, repeated errors, or a forced
exit mid-cycle. A normal stop exits `0`.

## Modes

| mode | market data | orders | needs keys |
|------|-------------|--------|-----------|
| `paper` (default, **test mode**) | live, from `data-api.binance.vision` | simulated against the live order book, with fake balances (`paper_balances`) | no |
| `testnet` | Binance Spot Testnet | real orders on the testnet (fake money) | `BINANCE_TESTNET_API_KEY` / `_SECRET` |
| `live` | Binance | **real orders, real money**; also requires `--confirm-live` | `BINANCE_API_KEY` / `_SECRET` |

Paper mode re-fetches the book for every simulated order, so prices that move while the legs are
in flight affect the result. It also enforces the same filters and balance checks as Binance, and
it remembers the liquidity it has "taken", so it can't trade the same resting order twice. What it
**can't** model is a faster bot taking the liquidity first, so treat paper results as an
upper bound. The testnet has few symbols and unrealistic books; use it to check your keys and
order plumbing, not profitability.

## Quick start

```bash
pip install -r requirements.txt

# What does every cycle look like right now? (never trades)
python -m arbitrage_bot --once

# Paper trade on live data (Ctrl+C prints a summary)
cp config.example.json config.json      # optional: edit settings
python -m arbitrage_bot

# ...with a live dashboard in your browser
python -m arbitrage_bot --ui

# Real money - only after paper trading shows a real edge
export BINANCE_API_KEY=... BINANCE_API_SECRET=...
python -m arbitrage_bot --mode live --confirm-live
```

**Dashboard (`--ui`).** This opens http://127.0.0.1:8765/ in your browser and shows what the bot is doing, live:
- every cycle's current edge against your threshold
- the best edge over time
- P&L, trades and how close each risk limit is
- the activity log

It has three buttons: **Pause trading** (keep watching, place no orders), **Resume** and **Stop** (finish the current cycle, then stop).
- It only listens on your own machine, and nothing on it can place an order or change a setting.
- When the bot stops, the page stays up so you can see why; press Ctrl+C to exit.
- Use `--ui-port` to change the port and `--no-browser` to skip opening a tab.

With live keys set, paper mode also reads your account's real taker fee for each symbol
(read-only), so paper results reflect your fee tier.

**API keys:** enable *spot trading* only, never withdrawals, and restrict the key to your IP.
Keys are read only from the environment. `config.json`, `.env`, logs and trade logs are gitignored.

## Config (`config.json`)

| key | default | meaning |
|-----|---------|---------|
| `home_asset` | `USDT` | the currency each cycle starts and ends in |
| `triangles` | 8 USDT triangles | each one is the home asset plus two others; both directions are traded |
| `min_trade` / `max_trade` | 20 / 200 | trade size range, in the home asset |
| `max_balance_fraction` | 0.9 | never commit more than this share of the home balance |
| `min_profit_bps` / `min_profit_abs` | 2 / 0.01 | after fees, depth and rounding, both must be met to trade |
| `taker_fee_bps` | 10 | fee used when the account fee can't be read |
| `fee_overrides_bps` | `{}` | per-symbol fee, e.g. `{"FDUSDUSDT": 0}` for zero-fee promotions; overrides everything |
| `use_account_fees` | true | read your real fee for each symbol from Binance when keys are available |
| `complete_with_market` | true | finish legs 2 and 3 with a market order if the IOC doesn't fully fill |
| `depth_limit` | 20 | order book levels to fetch |
| `poll_interval_sec` / `cooldown_sec` | 1 / 2 | time between scans / pause after a trade |
| `max_depth_checks_per_scan` | 3 | at most this many full-depth checks per scan |
| `max_cycles` | 0 | stop after this many cycles (0 = never) |
| `max_loss` | 5 | stop when realised P&L, minus fees paid in other assets, plus dust falls this far below zero (home asset) |
| `max_consecutive_losses` / `max_consecutive_errors` | 5 / 5 | stop conditions (the error limit also applies to rejected orders) |
| `paper_balances` | `{"USDT": 1000}` | starting balances in paper mode |
| `paper_depletion_sec` | 60 | how long paper mode remembers liquidity it already took |
| `log_file` / `trade_log` | `arbitrage.log` / `trades.jsonl` | where output goes (`""` disables) |

## Will it make money? Be realistic

The bot only takes a trade when it expects a profit **after** fees, depth and rounding, so it
shouldn't knowingly take a bad one. Whether profitable trades come along at all depends on
your fees and speed. Snapshot from 2026-10-06, using `--once` and a 2-minute paper run:

- At the standard **0.10% taker fee**, every one of the 16 default cycles was about **−30 bps**.
  Nothing comes close.
- Even with **zero fees**, the best top-of-book edge in 2 minutes was **+1.6 bps**. After depth
  and lot-size rounding it didn't qualify, so there were no trades.

These markets are watched by professional, co-located bots. To have a real chance you would need:

- **Lower fees.** Zero-fee promotion pairs (set `fee_overrides_bps` or let the bot read your
  account), a VIP tier, or the BNB fee discount (not modelled, so the bot's fee estimate is
  conservative).
- **Lower latency.** Run it close to Binance (AWS Tokyo, `ap-northeast-1`). The obvious next code
  steps are a WebSocket price feed instead of REST polling, and keeping inventory in all three
  currencies so the three legs can be sent at the same time instead of one after another.
- **Patience and data.** Run paper mode for days and check `trades.jsonl` and the `STATS` lines
  before trusting real money to it.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

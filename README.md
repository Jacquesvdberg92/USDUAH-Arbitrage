# USDUAH-Arbitrage

Triangular arbitrage on Binance spot.

- `BUSD USDT Oppertunity.py` + `key.py`: the original 2023 script, kept as it was.
- `arbitrage_bot/`: a rewrite that is meant to actually work, with a paper-trading **test mode**.

## Why the original can't work any more

1. **Its markets are gone.** Binance has suspended every UAH pair (`USDTUAH`, `BUSDUAH`, …) and
   `BUSDUSDT`. They all report status `BREAK`. The new bot checks this at startup and tells you.
2. **The signal didn't match the trades.** It compared `USDTUAH ask − BUSDUAH bid`, but the trades
   it then placed bought at the BUSD *ask* and sold USDT at the *bid*. The `BUSDUSDT` leg and
   trading fees (about 0.1% per leg, so about 0.3% per cycle) were never included in the check.
3. **Order sizes didn't chain.** Every leg used a fixed `quantity=100` in base units, regardless
   of what the previous leg actually produced, so balances drifted or orders failed.
4. **No error handling.** A failed leg left a half-done position, and any API error crashed the loop.

## What the rewrite does

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
   dust back to the home asset once there's enough to trade.

**Risk limits.** The bot stops on any of: a cumulative loss limit, N losing cycles in a row,
N API errors in a row (with backoff, and honouring `Retry-After`), `max_cycles`, or a position
it couldn't unwind. Trade size is also capped by `max_trade` and by a fraction of your balance.

## Modes

| mode | market data | orders | needs keys |
|------|-------------|--------|-----------|
| `paper` (default, **test mode**) | live, from `data-api.binance.vision` | simulated against the live order book, with fake balances (`paper_balances`) | no |
| `testnet` | Binance Spot Testnet | real orders on the testnet (fake money) | `BINANCE_TESTNET_API_KEY` / `_SECRET` |
| `live` | Binance | **real orders, real money**; also requires `--confirm-live` | `BINANCE_API_KEY` / `_SECRET` |

Paper mode re-fetches the book for every simulated order, so prices that move while the legs are
in flight affect the result. It also enforces the same filters and balance checks as Binance.
What it **can't** model is a faster bot taking the liquidity first, so treat paper results as an
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

# Real money - only after paper trading shows a real edge
export BINANCE_API_KEY=... BINANCE_API_SECRET=...
python -m arbitrage_bot --mode live --confirm-live
```

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
| `max_loss` | 5 | stop when realised P&L plus dust falls this far below zero (home asset) |
| `max_consecutive_losses` / `max_consecutive_errors` | 5 / 5 | stop conditions |
| `paper_balances` | `{"USDT": 1000}` | starting balances in paper mode |
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

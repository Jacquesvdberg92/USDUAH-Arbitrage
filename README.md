# SOL Arbitrage (Binance)

A desktop bot for Binance spot that trades SOL across its three stablecoin markets: USDT, FDUSD and USDC. A route starts in stablecoin A, buys SOL on SOL/A, sells it on SOL/B and converts B back to A on the stablecoin pair. It trades only when the loop still pays after every fee in the worst case.

The on-chain Solana DEX bot is a separate project: [solana-dex-arbitrage](https://github.com/Jacquesvdberg92/solana-dex-arbitrage).

## Run it

Python 3.10+ with Tkinter (`sudo apt install python3-tk` on Debian/Ubuntu).

```bash
pip install -r requirements.txt
python sol_arbitrage.py
```

Without API keys it runs on Binance's public market data in **dry run**: it shows every opportunity and paper-trades it, and never places an order. Put your key in `key.py` to use your real fee tier and balances, and to allow live trading:

```python
api_key = '...'
api_secret = '...'
```

Give the key spot trading permission only, never withdrawals, and restrict it to your IP address.

## The window

- **Settings**: trade size, the minimum profit, check interval, fallback fee, cooldown, and two stops: a session loss limit and a maximum trade count. Hover a field for what it means.
- **Dry run / Live trading**: live asks for confirmation before it starts, and again before it resumes after a stop.
- **Dashboard**: the best route right now and its worst-case profit, session P&L, trades, a price chart and a log.

## How it keeps money safe

- Prices and quantities are exact decimals built from Binance's own strings and rounded to each symbol's tick and lot size.
- A route triggers only if it is still profitable when every leg fills entirely at its limit price, using your real per-pair fees.
- Leg 1 is Fill-Or-Kill: the whole buy fills at the planned price or nothing happens.
- Leg 2 never sells below break-even. Leftover SOL is sold back with a capped loss (0.5%).
- An order whose outcome is unknown (timeout, 5xx, -1006/-1007) is looked up by its client order id, never sent twice. If Binance cannot say what happened, the bot stops.
- The session stops at the loss limit or trade count, and on key or permission errors.

## Tests

```bash
python -m unittest -v test_arb_engine
```

48 offline tests run whole trades end to end against a fake exchange with real order book matching, balances and commission. They need no network and no keys.

## Files

```
sol_arbitrage.py    the window (Tkinter)
arb_engine.py       market data, route maths and order execution (no UI code)
test_arb_engine.py  offline tests
key.py              your API key (placeholders in the repo)
```

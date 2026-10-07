"""SOL triangular arbitrage engine: market data, route maths and order execution.

A route starts with stablecoin A, buys SOL on SOL/A, sells it on SOL/B and converts B
back to A on the stablecoin pair. There is no UI code in here, so it can be tested on its
own (see test_arb_engine.py); sol_arbitrage.py is the window around it.

Money safety rules:
  * Prices/quantities are Decimals built from the API's strings and sent as plain strings.
  * A route only triggers if it is still profitable when every leg fills entirely at its
    limit price (the worst an IOC/FOK order can do), using your real per-pair fees.
  * Leg 1 is Fill-Or-Kill: either the whole buy fills at <= the planned price, or nothing
    happens and nothing is lost.
  * Leg 2 never sells below break-even; leftover SOL is sold back with a capped loss.
  * Any order whose outcome is unknown is looked up by its id, never sent twice.
"""
import json
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal as D, ROUND_DOWN, ROUND_UP

import requests
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException

COIN = 'SOL'
QUOTES = ['USDT', 'FDUSD', 'USDC']
# (base, quote) -> symbol. There is no USDCFDUSD: FDUSD is the base of FDUSDUSDC.
STABLE_PAIRS = {('FDUSD', 'USDT'): 'FDUSDUSDT', ('USDC', 'USDT'): 'USDCUSDT', ('FDUSD', 'USDC'): 'FDUSDUSDC'}
SYMBOLS = [COIN + q for q in QUOTES] + list(STABLE_PAIRS.values())
ASSETS = QUOTES + [COIN, 'BNB']

# Defaults, chosen from live order-book sampling and 90 days of price history: at regular
# fees a route costs ~0.2-0.3%, so these only fire on a real dislocation (e.g. a stablecoin
# depeg). Every one of them can be overridden in key.py or in the window.
DEFAULTS = {
    'size': 100.0,          # trade size, in the starting stablecoin
    'min_profit': 0.15,     # % net profit required in the worst case (after fees and spread)
    'interval': 1.0,        # seconds between checks
    'fallback_fee': 0.10,   # % taker fee per leg, used when your real fees cannot be read
    'cooldown': 30.0,       # seconds to pause after a trade
    'max_loss': 1.50,       # stop when the session has lost this much (USD)
    'max_trades': 20,       # stop after this many trades
    'dry_run': True,        # True = paper trading only, never place orders
}

BOOK_LIMIT = 20            # depth levels fetched for planning (weight 5 per book)
DEPTH_CUSHION = D(3)       # SOL/B bids at >= our price must hold 3x our sell size
UNWIND_MAX_LOSS = D('0.005')  # leftover SOL is sold back on SOL/A at most 0.5% below cost
LEG2_WAIT = 1.0            # seconds to wait for a better SOL/B bid after a partial leg 2
FEE_REFRESH = 900          # seconds between fee re-reads
BALANCE_REFRESH = 30       # seconds between balance re-reads
CLOCK_REFRESH = 600        # seconds between clock syncs
DATA_API = 'https://data-api.binance.vision/api'
UNKNOWN_CODES = {-1000, -1001, -1006, -1007, -1008}   # order may or may not exist
FATAL_CODES = {-2014, -2015, -1022}                   # key / permission / signature problems
ZERO, ONE, HUNDRED = D(0), D(1), D(100)
BIG = D(10) ** 12


# ----------------------------------------------------------------------------- numbers
def floor_to(x, step):
    return x if step == 0 else (x / step).to_integral_value(rounding=ROUND_DOWN) * step


def ceil_to(x, step):
    return x if step == 0 else (x / step).to_integral_value(rounding=ROUND_UP) * step


def fmt(x, step):
    """Plain string with exactly the step's decimals (never '1E+1', never float noise)."""
    places = max(0, -step.normalize().as_tuple().exponent) if step != 0 else 8
    return f"{x.quantize(D(1).scaleb(-places), rounding=ROUND_DOWN):.{places}f}"


def levels(raw):
    return [(D(p), D(q)) for p, q, *_ in raw]


class Rules:
    """Exchange filters for one symbol."""

    def __init__(self, s):
        f = {x['filterType']: x for x in s['filters']}
        notional = f.get('NOTIONAL') or f.get('MIN_NOTIONAL') or {}
        self.symbol, self.base, self.quote, self.status = s['symbol'], s['baseAsset'], s['quoteAsset'], s['status']
        self.tick = D(f['PRICE_FILTER']['tickSize'])
        self.min_price, self.max_price = D(f['PRICE_FILTER']['minPrice']), D(f['PRICE_FILTER']['maxPrice'])
        self.step = D(f['LOT_SIZE']['stepSize'])
        self.min_qty, self.max_qty = D(f['LOT_SIZE']['minQty']), D(f['LOT_SIZE']['maxQty'])
        self.min_notional = D(notional.get('minNotional', '0'))

    def price(self, side, p):
        # a BUY limit rounds down (never pay more), a SELL limit rounds up (never accept less)
        return floor_to(p, self.tick) if side == 'BUY' else ceil_to(p, self.tick)

    def check(self, qty, price):
        if self.status != 'TRADING':
            return f'{self.symbol} is {self.status}'
        if price % self.tick or (self.min_price and price < self.min_price) or (self.max_price and price > self.max_price):
            return f'{self.symbol} price {price} breaks PRICE_FILTER'
        if qty < self.min_qty or qty > self.max_qty or qty % self.step:
            return f'{self.symbol} quantity {qty} breaks LOT_SIZE'
        if qty * price < self.min_notional:
            return f'{self.symbol} order value below minimum {self.min_notional}'
        return None


class Fees:
    """Taker rate per (symbol, side), as a fraction. Falls back to one rate for everything."""

    def __init__(self, fallback):
        self.rates, self.fallback, self.source = {}, D(str(fallback)), 'fallback'

    def get(self, symbol, side):
        return self.rates.get((symbol, side), self.fallback)

    def load_from_account(self, client):
        """GET /api/v3/account/commission per symbol: standard + tax + special, taker + buyer/seller.
        The BNB discount is ignored on purpose (conservative)."""
        rates = {}
        for s in SYMBOLS:
            c = client._get('account/commission', True, data={'symbol': s})
            for side, extra in (('BUY', 'buyer'), ('SELL', 'seller')):
                std = c['standardCommission']     # required: a missing block must not read as 0%
                rates[(s, side)] = D(std['taker']) + D(std[extra]) + sum(
                    (D(c[k]['taker']) + D(c[k][extra]) for k in ('taxCommission', 'specialCommission') if k in c), ZERO)
        self.rates, self.source = rates, 'account'

    def route_total(self, a, b):
        stable, side = stable_leg(a, b)
        return self.get(COIN + a, 'BUY') + self.get(COIN + b, 'SELL') + self.get(stable, side)


def stable_leg(a, b):
    """Symbol and side that converts B back into A."""
    if (b, a) in STABLE_PAIRS:
        return STABLE_PAIRS[(b, a)], 'SELL'   # B is the base: sell B for A
    return STABLE_PAIRS[(a, b)], 'BUY'        # A is the base: buy A with B


def route_name(a, b):
    return f'{a} > {COIN} > {b} > {a}'


ROUTES = [(a, b) for a in QUOTES for b in QUOTES if a != b]


# ------------------------------------------------------------------------ book walking
def walk_buy_with_quote(asks, budget, step):
    """Largest base qty (multiple of step) the budget buys, and the worst ask touched."""
    got, left, worst = ZERO, budget, ZERO
    for p, q in asks:
        take = min(q, floor_to(left / p, step))
        if take <= 0:
            break
        got, left, worst = got + take, left - take * p, p
        if take < q:
            break
    return got, worst


def walk(levels_, qty):
    """Fill qty against levels -> (filled, quote value, worst price touched)."""
    left, value, worst = qty, ZERO, ZERO
    for p, q in levels_:
        take = min(q, left)
        value, left, worst = value + take * p, left - take, p
        if left <= 0:
            break
    return qty - left, value, worst


def depth_at(levels_, price, side):
    """Quantity available at prices at least as good as `price` for us."""
    if side == 'SELL':   # we sell into bids >= price
        return sum((q for p, q in levels_ if p >= price), ZERO)
    return sum((q for p, q in levels_ if p <= price), ZERO)


# ------------------------------------------------------------------------------ plan
class Plan:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    @property
    def name(self):
        return route_name(self.a, self.b)


def plan_route(a, b, budget, books, rules, fees, cushion=ZERO, dust_steps=12):
    """Plan one route against the given books. Returns (Plan, None) or (None, reason).

    `pct` is the worst case: every leg fills entirely at its limit price, with real fees and
    lot rounding (whole units on the stable pair), counting only what comes back as A.
    The few cents of SOL/B dust left over are a bonus on top (`pct_with_dust`)."""
    sa, sb = COIN + a, COIN + b
    stable, side3 = stable_leg(a, b)
    if any(s not in rules or s not in books or books[s] is None for s in (sa, sb, stable)):
        return None, 'market data missing'
    ra, rb, rs = rules[sa], rules[sb], rules[stable]
    for r in (ra, rb, rs):
        if r.status != 'TRADING':
            return None, f'{r.symbol} is {r.status}'

    # leg 1: BUY SOL on SOL/A, limit = worst ask touched
    q1, worst_ask = walk_buy_with_quote(books[sa]['asks'], budget, ra.step)
    if q1 <= 0:
        return None, f'{sa} has no asks within budget'
    p1 = ra.price('BUY', worst_ask)
    f1 = fees.get(sa, 'BUY')
    keep = ONE - f1                                # the buy fee is taken in SOL
    q2_max = floor_to(q1 * keep, rb.step)

    # leg 2: SELL SOL on SOL/B, limit = worst bid touched
    filled, _, worst_bid = walk(books[sb]['bids'], q2_max)
    if filled < q2_max:
        return None, f'{sb} bids too thin'
    p2 = rb.price('SELL', worst_bid)
    if cushion and depth_at(books[sb]['bids'], p2, 'SELL') < cushion * q2_max:
        return None, f'{sb} bids too thin for a safe sell'
    f2 = fees.get(sb, 'SELL')

    # leg 3: B -> A in whole units, limit = worst level touched
    f3 = fees.get(stable, side3)
    b_max = q2_max * p2 * (ONE - f2)
    if side3 == 'SELL':
        filled3, _, worst3 = walk(books[stable]['bids'], floor_to(b_max, rs.step))
        p3 = rs.price('SELL', worst3)
        b_to_a = p3 * (ONE - f3)
    else:
        if not books[stable]['asks']:
            return None, f'{stable} has no asks'
        est = floor_to(b_max / books[stable]['asks'][0][0], rs.step)
        filled3, _, worst3 = walk(books[stable]['asks'], est)
        p3 = rs.price('BUY', worst3) if worst3 else ZERO
        b_to_a = (ONE - f3) / p3 if p3 else ZERO
    if not p3 or filled3 <= 0 or filled3 < (floor_to(b_max, rs.step) if side3 == 'SELL' else est):
        return None, f'{stable} book too thin'

    def legs_2_3(q2):
        got_b = q2 * p2 * (ONE - f2)
        if side3 == 'SELL':
            q3 = floor_to(got_b, rs.step)
            return q3, q3 * p3 * (ONE - f3), got_b - q3
        q3 = floor_to(got_b / p3, rs.step)
        return q3, q3 * (ONE - f3), got_b - q3 * p3

    # whole-unit stable step: shave a few SOL steps so the B proceeds land just above a
    # whole number, which cuts the unconverted B left over from up to ~1 unit to ~0.1
    best = None
    for k in range(dust_steps + 1):
        q2 = q2_max - k * rb.step
        if q2 < rb.min_qty:
            break
        q3, a_back, b_dust = legs_2_3(q2)
        if q3 < rs.min_qty or q3 * p3 < rs.min_notional:
            continue
        if best is None or b_dust < best[3]:
            best = (q2, q3, a_back, b_dust)
    if best is None:
        return None, 'trade too small for the stablecoin pair minimums'
    q2, q3, a_back, b_dust = best
    q1 = ceil_to(q2 / keep, ra.step)               # just enough SOL that q2 is left after the fee
    sol_dust = q1 * keep - q2

    for r, q, p in ((ra, q1, p1), (rb, q2, p2), (rs, q3, p3)):
        why = r.check(q, p)
        if why:
            return None, why

    spend = q1 * p1
    dust_value = sol_dust * p2 * (ONE - f2) * b_to_a + b_dust * b_to_a
    profit = a_back + dust_value - spend
    return Plan(a=a, b=b, sa=sa, sb=sb, stable=stable, side3=side3,
                q1=q1, p1=p1, q2=q2, p2=p2, q3=q3, p3=p3,
                spend=spend, a_back=a_back, dust_value=dust_value, profit=profit,
                cash_profit=a_back - spend, pct=(a_back - spend) / spend * HUNDRED,
                pct_with_dust=profit / spend * HUNDRED, fees_pct=(f1 + f2 + f3) * HUNDRED), None


def books_from_tickers(tickers, deep=False):
    """Turn bookTicker rows into one-level books. deep=True pretends each level is endless,
    which gives the price-only estimate shown in the window."""
    books = {}
    for t in tickers:
        bid, ask = D(t['bidPrice']), D(t['askPrice'])
        if bid <= 0 or ask <= 0:
            books[t['symbol']] = None
            continue
        bq, aq = (BIG, BIG) if deep else (D(t['bidQty']), D(t['askQty']))
        books[t['symbol']] = {'bids': [(bid, bq)], 'asks': [(ask, aq)]}
    return books


# -------------------------------------------------------------------------- execution
class Fill:
    def __init__(self, resp, trades=None):
        self.symbol, self.side, self.status = resp['symbol'], resp['side'], resp['status']
        self.order_id, self.client_id = resp.get('orderId'), resp.get('clientOrderId')
        self.qty, self.quote = D(resp['executedQty']), D(resp['cummulativeQuoteQty'])
        self.commission = {}
        for f in resp.get('fills') or trades or []:
            self.commission[f['commissionAsset']] = self.commission.get(f['commissionAsset'], ZERO) + D(f['commission'])

    def net_base(self, asset):
        return self.qty - self.commission.get(asset, ZERO)

    def net_quote(self, asset):
        return self.quote - self.commission.get(asset, ZERO)


class OrderUnknown(Exception):
    """An order was sent but its outcome could not be confirmed."""


def place_limit(client, rules, side, qty, price, tif, tag):
    """LIMIT IOC/FOK with a FULL response. Raises BinanceAPIException only for a definite
    rejection (the order never reached the matching engine) and OrderUnknown if the
    outcome cannot be confirmed."""
    coid = f'arb{tag}{uuid.uuid4().hex[:20]}'
    params = dict(symbol=rules.symbol, side=side, type='LIMIT', timeInForce=tif,
                  quantity=fmt(qty, rules.step), price=fmt(price, rules.tick),
                  newClientOrderId=coid, newOrderRespType='FULL')
    try:
        return Fill(client.create_order(**params))
    except BinanceAPIException as e:
        if e.status_code < 500 and e.code not in UNKNOWN_CODES:
            raise
    except Exception:   # timeout, connection reset, bad JSON: the order may or may not exist
        pass
    return reconcile(client, rules.symbol, coid)


RECONCILE_TRIES, RECONCILE_DELAY = 5, 0.5


def reconcile(client, symbol, coid):
    """Look up an order whose response was lost. Never resend blindly: that can double-buy.
    Only ever returns a Fill or raises OrderUnknown."""
    for i in range(RECONCILE_TRIES):
        time.sleep(RECONCILE_DELAY * (i + 1))
        try:
            o = client.get_order(symbol=symbol, origClientOrderId=coid)
            if o['status'] in ('NEW', 'PARTIALLY_FILLED', 'PENDING_NEW'):
                continue
            executed = D(o['executedQty'])
            trades = client.get_my_trades(symbol=symbol, orderId=o['orderId']) if executed > 0 else []
            if sum((D(t['qty']) for t in trades), ZERO) != executed:
                continue                       # not all trades visible yet
            return Fill(o, trades)
        except BinanceAPIException as e:
            if e.status_code in (418, 429):
                time.sleep(2)
        except Exception:
            pass
    raise OrderUnknown(f'{symbol} order {coid}: outcome unknown, check your Binance order history')


class Result:
    def __init__(self):
        self.outcome, self.fills, self.notes, self.code = 'STARTED', [], [], None
        self.a_spent = self.a_received = self.sol_left = self.b_left = self.pnl = ZERO
        self.leg_rejected = False


def execute(client, plan, rules, fees, fetch_book, leg2_wait=LEG2_WAIT):
    """Run the three legs of a plan. Returns a Result and never raises.

    Outcomes: NO_FILL / REJECTED (leg 1 did nothing, no cost), DONE, LEG_REJECTED (a later
    leg was refused; SOL sold back), HOLDING_SOL / HOLDING_<B> (something could not be
    converted back), UNKNOWN (an order's outcome could not be confirmed)."""
    res = Result()
    try:
        f1 = place_limit(client, rules[plan.sa], 'BUY', plan.q1, plan.p1, 'FOK', '1')
    except BinanceAPIException as e:
        res.outcome, res.code = 'REJECTED', e.code
        res.notes.append(f'leg 1 rejected: {e.message} ({e.code})')
        return res
    except OrderUnknown as e:
        res.outcome = 'UNKNOWN'
        res.notes.append(str(e))
        return res
    res.fills.append(f1)
    if f1.qty == 0:
        res.outcome = 'NO_FILL'
        res.notes.append('leg 1 did not fill (price moved), nothing traded')
        return res
    try:
        finish(client, plan, rules, fees, fetch_book, leg2_wait, res, f1)
    except OrderUnknown as e:
        res.outcome = 'UNKNOWN'
        res.notes.append(str(e))
    except Exception as e:   # we own SOL now: anything unexpected must stop the engine
        res.outcome = 'UNKNOWN'
        res.notes.append(f'unexpected error during the trade: {e!r}')
    return res


def finish(client, plan, rules, fees, fetch_book, leg2_wait, res, f1):
    """Legs 2 and 3 (and the unwind) after leg 1 bought SOL. Updates res as it goes."""
    ra, rb, rs = rules[plan.sa], rules[plan.sb], rules[plan.stable]
    f1r, f2r, f3r = fees.get(plan.sa, 'BUY'), fees.get(plan.sb, 'SELL'), fees.get(plan.stable, plan.side3)
    res.a_spent = f1.quote + f1.commission.get(plan.a, ZERO)
    bought = f1.net_base(COIN)
    res.sol_left = bought
    bnb_fee = f1.quote * f1r if 'BNB' in f1.commission else ZERO
    b_to_a = plan.p3 * (ONE - f3r) if plan.side3 == 'SELL' else (ONE - f3r) / plan.p3
    breakeven = (res.a_spent + bnb_fee) / (bought * (ONE - f2r) * b_to_a)

    # leg 2: sell SOL for B, first at the planned price, then never below break-even
    price = plan.p2
    try:
        for attempt in range(3):
            q = floor_to(res.sol_left, rb.step)
            if q < rb.min_qty or q * price < rb.min_notional:
                break
            f2 = place_limit(client, rb, 'SELL', q, price, 'IOC', f'2{attempt}')
            res.fills.append(f2)
            res.sol_left -= f2.qty
            res.b_left += f2.net_quote(plan.b)
            if 'BNB' in f2.commission:
                bnb_fee += f2.quote * f2r * b_to_a
            if floor_to(res.sol_left, rb.step) < rb.min_qty:
                break
            res.notes.append(f'leg 2 filled {f2.qty} of {q} SOL')
            price, deadline = None, time.monotonic() + leg2_wait
            floor_price = rb.price('SELL', breakeven)
            while price is None and time.monotonic() < deadline:
                try:
                    bids = fetch_book(plan.sb)['bids']
                except Exception:
                    bids = []
                if bids and bids[0][0] >= floor_price:
                    price = max(walk(bids, floor_to(res.sol_left, rb.step))[2], floor_price)
                else:
                    time.sleep(0.2)
            if price is None:
                break
    except BinanceAPIException as e:
        res.leg_rejected = True
        res.notes.append(f'leg 2 rejected: {e.message} ({e.code})')
        if e.status_code in (418, 429):
            time.sleep(1)

    # leftover SOL: sell it back on SOL/A, at most UNWIND_MAX_LOSS below what it cost
    q_left = floor_to(res.sol_left, ra.step)
    floor_px = ra.price('SELL', res.a_spent / bought * (ONE - UNWIND_MAX_LOSS))
    if q_left >= ra.min_qty and q_left * floor_px >= ra.min_notional:
        try:
            fu = place_limit(client, ra, 'SELL', q_left, floor_px, 'IOC', 'u')
            res.fills.append(fu)
            res.sol_left -= fu.qty
            res.a_received += fu.net_quote(plan.a)
            if 'BNB' in fu.commission:
                bnb_fee += fu.quote * fees.get(plan.sa, 'SELL')
            res.notes.append(f'sold {fu.qty} leftover SOL back on {plan.sa}')
        except BinanceAPIException as e:
            res.leg_rejected = True
            res.notes.append(f'selling leftover SOL rejected: {e.message} ({e.code})')

    # leg 3: B back to A at the planned price, in whole units
    try:
        for attempt in range(2):
            b = res.b_left
            q3 = floor_to(b, rs.step) if plan.side3 == 'SELL' else floor_to(b / plan.p3, rs.step)
            if q3 < rs.min_qty or q3 * plan.p3 < rs.min_notional:
                break
            f3 = place_limit(client, rs, plan.side3, q3, plan.p3, 'IOC', f'3{attempt}')
            res.fills.append(f3)
            if plan.side3 == 'SELL':
                res.b_left -= f3.qty
                res.a_received += f3.net_quote(plan.a)
            else:
                res.b_left -= f3.quote
                res.a_received += f3.net_base(plan.a)
            if 'BNB' in f3.commission:
                bnb_fee += (f3.quote if plan.side3 == 'SELL' else f3.qty) * f3r
            if f3.status == 'FILLED':
                break
            time.sleep(0.3)
    except BinanceAPIException as e:
        res.leg_rejected = True
        res.notes.append(f'leg 3 rejected: {e.message} ({e.code})')

    # P&L in A: what came back, plus leftovers at today's prices, minus what was spent
    sol_px, b_px = current_values(plan, fees, fetch_book, res)
    res.pnl = res.a_received + res.sol_left * sol_px + res.b_left * b_px - res.a_spent - bnb_fee
    sellable = floor_to(res.sol_left, ra.step)
    if sellable >= ra.min_qty and sellable * plan.p1 >= ra.min_notional:
        res.outcome = 'HOLDING_SOL'
    elif res.b_left * b_px > ONE:
        res.outcome = 'HOLDING_' + plan.b
    elif res.leg_rejected:
        res.outcome = 'LEG_REJECTED'
    else:
        res.outcome = 'DONE'


def current_values(plan, fees, fetch_book, res):
    """What one leftover SOL and one leftover B are worth in A right now (after fees).
    If the books cannot be read, assume 10% / 2% haircuts on the planned prices."""
    f3r = fees.get(plan.stable, plan.side3)
    sol_px = plan.p1 * D('0.9')
    b_px = (plan.p3 * (ONE - f3r) if plan.side3 == 'SELL' else (ONE - f3r) / plan.p3) * D('0.98')
    try:
        if res.sol_left > 0:
            bids = fetch_book(plan.sa)['bids']
            sol_px = bids[0][0] * (ONE - fees.get(plan.sa, 'SELL')) if bids else ZERO
        if res.b_left > 0:
            bk = fetch_book(plan.stable)
            if plan.side3 == 'SELL':
                b_px = bk['bids'][0][0] * (ONE - f3r) if bk['bids'] else ZERO
            else:
                b_px = (ONE - f3r) / bk['asks'][0][0] if bk['asks'] else ZERO
    except Exception:
        pass
    return sol_px, b_px


def paper_fill(plan, books, rules, fees):
    """Dry run: what the planned orders would have done against `books`, which were fetched
    after the plan (so they include one round trip of price movement). Mirrors execute()."""
    ra, rs = rules[plan.sa], rules[plan.stable]
    f1r, f2r, f3r = fees.get(plan.sa, 'BUY'), fees.get(plan.sb, 'SELL'), fees.get(plan.stable, plan.side3)
    asks = [(p, q) for p, q in books[plan.sa]['asks'] if p <= plan.p1]
    got, cost, _ = walk(asks, plan.q1)
    if got < plan.q1:
        return 'NO_FILL', ZERO
    bought = plan.q1 * (ONE - f1r)
    sold, value, _ = walk([(p, q) for p, q in books[plan.sb]['bids'] if p >= plan.p2], plan.q2)
    sol, b, a_back = bought - sold, value * (ONE - f2r), ZERO
    if floor_to(sol, ra.step) >= ra.min_qty:      # sell the rest back, as execute() does
        floor_px = ra.price('SELL', cost / bought * (ONE - UNWIND_MAX_LOSS))
        u, u_value, _ = walk([(p, q) for p, q in books[plan.sa]['bids'] if p >= floor_px], floor_to(sol, ra.step))
        sol -= u
        a_back += u_value * (ONE - fees.get(plan.sa, 'SELL'))
    if plan.side3 == 'SELL':
        conv, got3, _ = walk([(p, q) for p, q in books[plan.stable]['bids'] if p >= plan.p3], floor_to(b, rs.step))
        a_back, b = a_back + got3 * (ONE - f3r), b - conv
        b_px = books[plan.stable]['bids'][0][0] * (ONE - f3r) if books[plan.stable]['bids'] else ZERO
    else:
        conv, cost3, _ = walk([(p, q) for p, q in books[plan.stable]['asks'] if p <= plan.p3], floor_to(b / plan.p3, rs.step))
        a_back, b = a_back + conv * (ONE - f3r), b - cost3
        b_px = (ONE - f3r) / books[plan.stable]['asks'][0][0] if books[plan.stable]['asks'] else ZERO
    bids_a = books[plan.sa]['bids']
    sol_px = bids_a[0][0] * (ONE - fees.get(plan.sa, 'SELL')) if bids_a else ZERO
    pnl = a_back + sol * sol_px + b * b_px - cost
    if floor_to(sol, ra.step) >= ra.min_qty and floor_to(sol, ra.step) * plan.p1 >= ra.min_notional:
        return 'HOLDING_SOL', pnl
    if b * b_px > ONE:
        return 'HOLDING_' + plan.b, pnl
    return 'DONE', pnl


# ----------------------------------------------------------------------------- engine
class _Client(Client):
    def ping(self):   # older python-binance pings api.binance.com in the constructor
        return {}


def make_client(api_key=None, api_secret=None, timeout=10):
    try:
        c = _Client(api_key, api_secret, requests_params={'timeout': timeout}, ping=False)
    except TypeError:   # older python-binance without the ping argument
        c = _Client(api_key, api_secret, requests_params={'timeout': timeout})
    c.REQUEST_RECVWINDOW = 5000
    return c


def has_keys(api_key, api_secret):
    return bool(api_key and api_secret and ' ' not in api_key and len(api_key) >= 20)


HALT_OUTCOMES = ('UNKNOWN', 'LEG_REJECTED')   # plus every HOLDING_*


class Engine:
    """Runs in a background thread. Talks to the window only through `emit(kind, data)`."""

    def __init__(self, settings, emit, stop_event, api_key=None, api_secret=None,
                 market_data_url=None, client_factory=make_client, sleep=None):
        self.s = settings
        self.emit = emit
        self.stop_event = stop_event
        self.api_key, self.api_secret = api_key, api_secret
        self.market_data_url = market_data_url
        self.client_factory = client_factory
        self.sleep = sleep or stop_event.wait
        self.pub = self.trd = None
        self.local = threading.local()      # one client per thread: Client is not thread safe
        self.rules, self.fees = {}, Fees(D(str(settings['fallback_fee'])) / HUNDRED)
        self.balances = {}
        self.pool = ThreadPoolExecutor(max_workers=3)
        self.stats = {'checks': 0, 'candidates': 0, 'trades': 0, 'wins': 0, 'losses': 0,
                      'pnl': ZERO, 'best_pct': None}
        self.cooldown_until = 0.0
        self.skip_until = {}   # route -> time; a route that failed its re-check rests for 5 s
        now = time.monotonic()
        self.last = {'fees': now, 'balances': now, 'clock': now, 'rules': now}
        self.halted = None

    # ---- helpers
    def log(self, text, level='info'):
        self.emit('log', (level, text))

    def halt(self, reason):
        if not self.halted:
            self.halted = reason
            self.log('Stopped: ' + reason, 'error')
            self.emit('halt', reason)
        self.stop_event.set()

    def live(self):
        return not self.s['dry_run'] and self.trd is not None

    def stats_out(self):
        return dict(self.stats, pnl=float(self.stats['pnl']))

    # ---- setup
    def connect(self):
        self.pub = self.client_factory(timeout=5)
        if self.market_data_url:
            self.pub.API_URL = self.market_data_url
        else:
            try:
                self.pub._get('ping', version='v3')
            except Exception:
                self.pub.API_URL = DATA_API
                self.log('api.binance.com is not reachable for market data, using data-api.binance.vision')
        self.local.client = self.pub
        self.load_rules()

        status = {'market': self.pub.API_URL, 'keys': False, 'trading': 'dry run only (no API keys)'}
        if has_keys(self.api_key, self.api_secret):
            try:
                self.trd = self.client_factory(self.api_key, self.api_secret, timeout=10)
                self.sync_clock()
                self.refresh_balances()
                self.refresh_fees()
                status.update(keys=True, trading='ready')
            except BinanceAPIException as e:
                self.trd = None
                status['trading'] = f'unavailable: {e.message}'
                self.log(f'API keys could not be used ({e.status_code} {e.message}). Dry run only.', 'error')
            except Exception as e:
                self.trd = None
                status['trading'] = 'unavailable: could not reach Binance'
                self.log(f'Could not reach Binance with your keys ({e!r}). Dry run only.', 'error')
        else:
            self.log('No API keys in key.py: running on public market data, dry run only', 'warn')
        status['fees'] = self.fees.source
        self.emit('status', status)
        if not self.s['dry_run'] and self.trd is None:
            self.halt('live trading needs working API keys')

    def load_rules(self):
        info = self.pub._get('exchangeInfo', version='v3', data={'symbols': json.dumps(SYMBOLS, separators=(',', ':'))})
        rules = {s['symbol']: Rules(s) for s in info['symbols']}
        for s, r in rules.items():
            if r.status != 'TRADING' and (s not in self.rules or self.rules[s].status == 'TRADING'):
                self.log(f'{s} is {r.status}, routes using it are skipped', 'warn')
        self.rules = rules
        self.last['rules'] = time.monotonic()

    def sync_clock(self):
        best = None
        for _ in range(3):
            t0 = time.time() * 1000
            server = self.trd.get_server_time()['serverTime']
            t1 = time.time() * 1000
            if best is None or t1 - t0 < best[0]:
                best = (t1 - t0, server - (t0 + t1) / 2)
        self.trd.timestamp_offset = int(best[1]) - 250
        self.last['clock'] = time.monotonic()

    def refresh_fees(self):
        try:
            self.fees.load_from_account(self.trd)
            self.log('Loaded your real fees: ' + ', '.join(
                f'{s} {self.fees.get(s, "BUY") * 100:.3f}%' for s in SYMBOLS))
        except Exception as e:
            if self.fees.rates:
                self.log(f'Could not re-read your fees ({e}); keeping the last ones read', 'warn')
            else:
                self.log(f'Could not read your fees ({e}); using {self.fees.fallback * 100}% per leg. '
                         'Live trades wait until real fees are read.', 'warn')
            self.last['fees'] = time.monotonic() - FEE_REFRESH + 30    # retry in 30 s
        else:
            self.last['fees'] = time.monotonic()
        self.emit('fees', {'source': self.fees.source,
                           'rates': {s: (self.fees.get(s, 'BUY'), self.fees.get(s, 'SELL')) for s in SYMBOLS}})

    def refresh_balances(self):
        acct = self.trd.get_account()
        self.balances = {b['asset']: D(b['free']) for b in acct['balances'] if b['asset'] in ASSETS}
        self.last['balances'] = time.monotonic()
        self.emit('balances', dict(self.balances))

    def housekeeping(self):
        """Periodic re-reads. A failure is logged and retried later; market scanning goes on."""
        now = time.monotonic()
        jobs = [('rules', 600, self.load_rules)]
        if self.trd is not None:
            jobs += [('clock', CLOCK_REFRESH, self.sync_clock), ('fees', FEE_REFRESH, self.refresh_fees),
                     ('balances', BALANCE_REFRESH, self.refresh_balances)]
        for name, every, job in jobs:
            if now - self.last[name] > every:
                try:
                    job()
                except Exception as e:
                    self.last[name] = now - every + 30
                    self.log(f'Could not refresh {name}: {getattr(e, "message", e)}', 'warn')
                    if getattr(e, 'code', None) == -1021 and name != 'clock':
                        self.last['clock'] = 0

    # ---- market data
    def tickers(self):
        return self.pub._get('ticker/bookTicker', version='v3',
                             data={'symbols': json.dumps(SYMBOLS, separators=(',', ':'))})

    def depth(self, symbol):
        c = getattr(self.local, 'client', None)
        if c is None:
            c = self.local.client = self.client_factory(timeout=5)
            c.API_URL = self.pub.API_URL
        b = c.get_order_book(symbol=symbol, limit=BOOK_LIMIT)
        return {'bids': levels(b['bids']), 'asks': levels(b['asks'])}

    def depths(self, symbols):
        return dict(zip(symbols, self.pool.map(self.depth, symbols)))

    # ---- main loop
    def run(self):
        try:
            self.connect()
        except Exception as e:
            self.log(f'Could not start: {getattr(e, "message", None) or e!r}', 'error')
            self.halted = self.halted or 'could not connect to Binance'
            self.stop_event.set()
        try:
            while not self.stop_event.is_set():
                started = time.monotonic()
                try:
                    self.step()
                except BinanceAPIException as e:
                    if e.status_code in (418, 429):
                        wait = 60
                        try:
                            wait = int(e.response.headers.get('Retry-After', 60))
                        except Exception:
                            pass
                        self.log(f'Binance rate limit hit, pausing {wait}s', 'error')
                        self.sleep(wait)
                    elif e.code in FATAL_CODES or e.status_code == 451:
                        self.halt(f'Binance refused the request: {e.message}')
                    else:
                        self.log(f'Binance error: {e.message} ({e.code})', 'error')
                except (requests.exceptions.RequestException, BinanceRequestException) as e:
                    self.log(f'Network error: {e.__class__.__name__}, retrying', 'error')
                self.sleep(max(0.0, self.s['interval'] - (time.monotonic() - started)))
        except Exception as e:
            self.log(traceback.format_exc(), 'error')
            self.halt(f'unexpected error: {e!r}')
        finally:
            self.pool.shutdown(wait=False)
            self.emit('stopped', self.halted)

    def step(self):
        self.housekeeping()
        t0 = time.monotonic()
        rows = self.tickers()
        latency = (time.monotonic() - t0) * 1000
        top = books_from_tickers(rows)
        priced = books_from_tickers(rows, deep=True)
        size, min_profit = D(str(self.s['size'])), D(str(self.s['min_profit']))
        self.stats['checks'] += 1

        routes = []
        for a, b in ROUTES:
            plan, why = plan_route(a, b, size, priced, self.rules, self.fees)
            thin = plan is not None and plan_route(a, b, size, top, self.rules, self.fees)[0] is None
            routes.append({'a': a, 'b': b, 'name': route_name(a, b),
                           'pct': float(plan.pct) if plan else None,
                           'fees_pct': float(self.fees.route_total(a, b) * HUNDRED),
                           'thin': thin, 'why': why})
        valid = [r for r in routes if r['pct'] is not None]
        best = max(valid, key=lambda r: r['pct']) if valid else None
        self.stats['best_pct'] = best['pct'] if best else None
        self.emit('tick', {'books': {s: top.get(s) for s in SYMBOLS}, 'routes': routes, 'latency': latency,
                           'stats': self.stats_out(), 'cooldown': max(0.0, self.cooldown_until - time.monotonic())})

        if self.halted or time.monotonic() < self.cooldown_until:
            return
        if self.stats['trades'] >= int(self.s['max_trades']):
            return
        for r in sorted(valid, key=lambda r: -r['pct']):
            if r['pct'] < float(min_profit):
                break
            if time.monotonic() < self.skip_until.get(r['name'], 0):
                continue
            if self.try_route(r['a'], r['b'], size, min_profit):
                break
            self.skip_until[r['name']] = time.monotonic() + 5

    def try_route(self, a, b, size, min_profit):
        """Re-check a promising route on fresh depth, then trade it (or paper trade it)."""
        self.stats['candidates'] += 1
        stable, _ = stable_leg(a, b)
        books = self.depths([COIN + a, COIN + b, stable])
        plan, why = plan_route(a, b, size, books, self.rules, self.fees, cushion=DEPTH_CUSHION)
        if plan is None or plan.pct < min_profit:
            self.log(f'{route_name(a, b)} looked good but failed the depth re-check: '
                     + (why or f'{plan.pct:+.3f}% worst case'), 'info')
            return False

        if not self.live():
            after = self.depths([COIN + a, COIN + b, stable])
            outcome, pnl = paper_fill(plan, after, self.rules, self.fees)
            self.log(f'DRY RUN {plan.name}: planned {plan.pct:+.3f}%, '
                     f'paper fill {outcome} {pnl:+.4f} {a}', 'trade')
            self.record(plan, outcome, pnl, live=False, fills=0)
            return True

        if self.fees.source != 'account':
            self.log(f'{plan.name} {plan.pct:+.3f}%: not trading until your real fees are read', 'warn')
            return False
        if time.monotonic() - self.last['balances'] > 3 * BALANCE_REFRESH:
            self.log(f'{plan.name} {plan.pct:+.3f}%: balances are out of date, not trading', 'warn')
            return False
        if self.balances.get(a, ZERO) < plan.spend:
            self.log(f'{plan.name} {plan.pct:+.3f}%: not enough {a} (need {plan.spend:.2f})', 'warn')
            return False

        self.log(f'Trading {plan.name}, planned {plan.pct:+.3f}% ({plan.cash_profit:+.4f} {a})', 'trade')
        try:
            res = execute(self.trd, plan, self.rules, self.fees, self.depth)
        except Exception as e:      # execute() should never raise; stop if it somehow does
            self.record(plan, 'UNKNOWN', ZERO, live=True, fills=0)
            self.halt(f'error during a live trade ({e!r}), check your Binance balances')
            return True
        for n in res.notes:
            self.log(n, 'warn')
        self.record(plan, res.outcome, res.pnl, live=True, fills=len(res.fills))
        try:
            self.refresh_balances()
        except Exception:
            self.last['balances'] = 0
        if res.outcome in HALT_OUTCOMES or res.outcome.startswith('HOLDING_'):
            left = []
            if res.sol_left >= rules_min(self.rules, plan.sa):
                left.append(f'{res.sol_left:.4f} SOL')
            if res.b_left > ONE:
                left.append(f'{res.b_left:.2f} {plan.b}')
            self.halt(f'trade ended {res.outcome}' + (f', holding {" and ".join(left)}' if left else '')
                      + '; check your Binance balances and orders')
        elif res.outcome == 'REJECTED' and res.code in FATAL_CODES:
            self.halt(f'Binance refused the order: {res.notes[-1]} (check the key has Spot trading enabled)')
        return True

    def record(self, plan, outcome, pnl, live, fills):
        traded = outcome not in ('NO_FILL', 'REJECTED')
        if traded:
            self.stats['trades'] += 1
            if outcome != 'UNKNOWN':
                self.stats['pnl'] += pnl
                self.stats['wins' if pnl >= 0 else 'losses'] += 1
        self.cooldown_until = time.monotonic() + (self.s['cooldown'] if traded else 10)
        self.emit('trade', {'time': time.strftime('%H:%M:%S'), 'route': plan.name, 'live': live,
                            'planned': float(plan.pct), 'outcome': outcome,
                            'pnl': None if outcome == 'UNKNOWN' else float(pnl),
                            'asset': plan.a, 'size': float(plan.spend), 'fills': fills,
                            'stats': self.stats_out()})
        if self.stats['pnl'] <= -D(str(self.s['max_loss'])):
            self.halt(f'session loss limit reached ({self.stats["pnl"]:.2f})')
        elif self.stats['trades'] >= int(self.s['max_trades']):
            self.halt(f'reached {int(self.s["max_trades"])} trades this session')


def rules_min(rules, symbol):
    return rules[symbol].min_qty if symbol in rules else D('0.001')

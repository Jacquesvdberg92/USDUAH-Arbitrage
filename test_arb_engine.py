"""Offline tests for arb_engine.py: no network, no API keys.

Run:  python -m unittest -v test_arb_engine

FakeExchange matches LIMIT IOC/FOK orders against an order book, keeps balances and charges
commission like Binance does, so whole trades can be run end to end.
"""
import json
import threading
import time
import unittest
from decimal import Decimal as D

import requests
from binance.exceptions import BinanceAPIException

import arb_engine as E

E.RECONCILE_DELAY = 0.01   # keep the order look-up retries fast in tests


def sym_info(symbol, base, quote, tick, step, status='TRADING'):
    return {'symbol': symbol, 'status': status, 'baseAsset': base, 'quoteAsset': quote, 'filters': [
        {'filterType': 'PRICE_FILTER', 'tickSize': tick, 'minPrice': tick, 'maxPrice': '10000'},
        {'filterType': 'LOT_SIZE', 'stepSize': step, 'minQty': step, 'maxQty': '9000000'},
        {'filterType': 'NOTIONAL', 'minNotional': '5.00000000'}]}


# the real filters of the six symbols (October 2026)
EXCHANGE_INFO = [
    sym_info('SOLUSDT', 'SOL', 'USDT', '0.01000000', '0.00100000'),
    sym_info('SOLFDUSD', 'SOL', 'FDUSD', '0.01000000', '0.00100000'),
    sym_info('SOLUSDC', 'SOL', 'USDC', '0.01000000', '0.00100000'),
    sym_info('FDUSDUSDT', 'FDUSD', 'USDT', '0.00010000', '1.00000000'),
    sym_info('USDCUSDT', 'USDC', 'USDT', '0.00001000', '1.00000000'),
    sym_info('FDUSDUSDC', 'FDUSD', 'USDC', '0.00010000', '1.00000000'),
]

# one real order-book snapshot (top 5 levels) recorded from Binance on 2026-10-07
REAL = {"SOLUSDT": {"bids": [["118.81", "506.361"], ["118.8", "285.102"], ["118.79", "310.842"], ["118.78", "244.033"], ["118.77", "287.753"]], "asks": [["118.82", "315.4"], ["118.83", "385.831"], ["118.84", "542.603"], ["118.85", "385.976"], ["118.86", "251.543"]]}, "SOLFDUSD": {"bids": [["118.91", "3.36"], ["118.9", "116.149"], ["118.89", "30.582"], ["118.88", "107.223"], ["118.87", "123.879"]], "asks": [["118.92", "2.942"], ["118.93", "0.064"], ["118.94", "5.322"], ["118.95", "8.341"], ["118.96", "6.645"]]}, "SOLUSDC": {"bids": [["118.78", "90.553"], ["118.77", "118.644"], ["118.76", "40.507"], ["118.75", "82.866"], ["118.74", "163.408"]], "asks": [["118.79", "26.061"], ["118.8", "50.212"], ["118.81", "36.915"], ["118.82", "31.852"], ["118.83", "128.824"]]}, "FDUSDUSDT": {"bids": [["0.9989", "547263.0"], ["0.9988", "307548.0"], ["0.9987", "603456.0"], ["0.9986", "424121.0"], ["0.9985", "406488.0"]], "asks": [["0.999", "1814318.0"], ["0.9991", "93899.0"], ["0.9992", "1904304.0"], ["0.9993", "2084975.0"], ["0.9994", "417627.0"]]}, "USDCUSDT": {"bids": [["1.00023", "695615.0"], ["1.00022", "672824.0"], ["1.00021", "3484125.0"], ["1.0002", "2026198.0"], ["1.00019", "4336769.0"]], "asks": [["1.00024", "746019.0"], ["1.00025", "64343.0"], ["1.00026", "5120692.0"], ["1.00027", "245258.0"], ["1.00028", "60382.0"]]}, "FDUSDUSDC": {"bids": [["0.9987", "163284.0"], ["0.9986", "232434.0"], ["0.9985", "195180.0"], ["0.9984", "101422.0"], ["0.9983", "1228.0"]], "asks": [["0.9988", "42421.0"], ["0.9989", "70090.0"], ["0.999", "9653.0"], ["0.9991", "51744.0"], ["0.9992", "9894.0"]]}}


def make_books(sol_bids, depth='50'):
    """SOL pairs with the given best bid (ask one tick above, five levels each side)
    and realistic, deep stablecoin pairs."""
    tick = D('0.01')
    books = {}
    for q, bid in sol_bids.items():
        bid = D(bid)
        books['SOL' + q] = {'bids': [[str(bid - tick * i), depth] for i in range(5)],
                            'asks': [[str(bid + tick * (i + 1)), depth] for i in range(5)]}
    for s, (b, a) in {'FDUSDUSDT': ('0.9990', '0.9991'), 'USDCUSDT': ('1.00000', '1.00001'),
                      'FDUSDUSDC': ('0.9990', '0.9991')}.items():
        books[s] = {'bids': [[b, '1000000']], 'asks': [[a, '1000000']]}
    return books


# fair: FDUSD trades at ~0.999 USDT, so SOL costs ~0.1% more FDUSD than USDT
FAIR = make_books({'USDT': '118.80', 'FDUSD': '118.92', 'USDC': '118.80'})
# SOL is ~1% dearer in FDUSD: buy with USDT/USDC, sell for FDUSD
MISPRICED = make_books({'USDT': '118.80', 'FDUSD': '120.00', 'USDC': '118.80'})
# SOL is ~1% dearer in USDT: FDUSD > SOL > USDT > FDUSD buys FDUSD back on FDUSDUSDT (BUY side)
MISPRICED_USDT = make_books({'USDT': '120.20', 'FDUSD': '118.92', 'USDC': '118.80'})


def as_levels(books):
    return {s: {'bids': E.levels(b['bids']), 'asks': E.levels(b['asks'])} for s, b in books.items()}


def api_error(code, msg, status=400):
    return BinanceAPIException(None, status, json.dumps({'code': code, 'msg': msg}))


def rules():
    return {s['symbol']: E.Rules(s) for s in EXCHANGE_INFO}


def fees(rate='0.001'):
    return E.Fees(D(rate))


class FakeExchange:
    """Just enough of python-binance's Client, with a tiny matching engine."""

    def __init__(self, books, fee='0.001', balances=None):
        self.books = {s: {k: [[D(p), D(q)] for p, q in b[k]] for k in ('bids', 'asks')} for s, b in books.items()}
        self.fee = D(fee)
        self.bal = {a: D(v) for a, v in (balances or {'USDT': '1000', 'FDUSD': '1000', 'USDC': '1000',
                                                      'SOL': '0', 'BNB': '0'}).items()}
        self.info = {s['symbol']: s for s in EXCHANGE_INFO}
        self.orders, self.by_coid, self.next_id = [], {}, 1
        self.fail_next = None      # (exception, executed_anyway) for the next create_order
        self.reject = {}           # symbol -> exception raised (definitely) by every order on it
        self.faults = {}           # method name -> list of exceptions raised by its next calls
        self.before_order = None   # callback(params) run before matching, e.g. to move the market
        self.commission_error = None
        self.API_URL, self.timestamp_offset, self.lock = 'fake', 0, threading.Lock()

    # public market data
    def fault(self, name):
        if self.faults.get(name):
            raise self.faults[name].pop(0)

    def _get(self, path, signed=False, version=None, **kw):
        data = kw.get('data', {})
        assert signed or version == 'v3', 'public calls must ask for /api/v3 (old python-binance defaults to v1)'
        if path == 'ping':
            return {}
        if path == 'exchangeInfo':
            return {'symbols': [self.info[s] for s in json.loads(data['symbols'])]}
        if path == 'ticker/bookTicker':
            return [self.ticker(s) for s in json.loads(data['symbols'])]
        if path == 'account/commission':
            if self.commission_error:
                raise self.commission_error
            f, z = str(self.fee), '0'
            return {'symbol': data['symbol'], 'standardCommission': {'maker': z, 'taker': f, 'buyer': z, 'seller': z},
                    'taxCommission': {'maker': z, 'taker': z, 'buyer': z, 'seller': z}}
        raise AssertionError('unexpected endpoint ' + path)

    def ticker(self, s):
        b = self.books[s]
        bid = b['bids'][0] if b['bids'] else [D(0), D(0)]
        ask = b['asks'][0] if b['asks'] else [D(0), D(0)]
        return {'symbol': s, 'bidPrice': str(bid[0]), 'bidQty': str(bid[1]), 'askPrice': str(ask[0]), 'askQty': str(ask[1])}

    def get_order_book(self, symbol, limit=100):
        self.fault('get_order_book')
        with self.lock:
            b = self.books[symbol]
            return {k: [[str(p), str(q)] for p, q in b[k][:limit]] for k in ('bids', 'asks')}

    def get_server_time(self):
        return {'serverTime': int(time.time() * 1000)}

    def get_account(self):
        return {'balances': [{'asset': a, 'free': str(v), 'locked': '0'} for a, v in self.bal.items()]}

    # trading
    def create_order(self, **p):
        self.orders.append(p)
        if self.before_order:
            self.before_order(p)
        if p['symbol'] in self.reject:
            raise self.reject[p['symbol']]
        if self.fail_next:
            exc, executed = self.fail_next
            self.fail_next = None
            if executed:
                self.match(p)
            raise exc
        return self.match(p)

    def match(self, p):
        with self.lock:
            info = self.info[p['symbol']]
            base, quote = info['baseAsset'], info['quoteAsset']
            side, qty, price = p['side'], D(p['quantity']), D(p['price'])
            assert p['type'] == 'LIMIT' and p['timeInForce'] in ('IOC', 'FOK')
            assert 'E' not in p['quantity'] and 'E' not in p['price']
            if side == 'BUY' and self.bal[quote] < qty * price or side == 'SELL' and self.bal[base] < qty:
                raise api_error(-2010, 'Account has insufficient balance for requested action.')
            key = 'asks' if side == 'BUY' else 'bids'
            crosses = (lambda lp: lp <= price) if side == 'BUY' else (lambda lp: lp >= price)
            levels = self.books[p['symbol']][key]
            fills, left = [], qty
            if not (p['timeInForce'] == 'FOK' and sum(q for lp, q in levels if crosses(lp)) < qty):
                for lv in levels:
                    if left <= 0 or not crosses(lv[0]):
                        break
                    take = min(left, lv[1])
                    lv[1] -= take
                    left -= take
                    fills.append((lv[0], take))
                self.books[p['symbol']][key] = [lv for lv in levels if lv[1] > 0]
            executed = sum((q for _, q in fills), D(0))
            value = sum((fp * fq for fp, fq in fills), D(0))
            out = []
            for fp, fq in fills:
                comm, asset = (fq * self.fee, base) if side == 'BUY' else (fp * fq * self.fee, quote)
                out.append({'price': str(fp), 'qty': str(fq), 'commission': str(comm), 'commissionAsset': asset})
            if side == 'BUY':
                self.bal[quote] -= value
                self.bal[base] += executed * (1 - self.fee)
            else:
                self.bal[base] -= executed
                self.bal[quote] += value * (1 - self.fee)
            resp = {'symbol': p['symbol'], 'side': side, 'orderId': self.next_id, 'clientOrderId': p['newClientOrderId'],
                    'status': 'FILLED' if executed == qty else 'EXPIRED', 'origQty': p['quantity'],
                    'executedQty': str(executed), 'cummulativeQuoteQty': str(value), 'fills': out}
            self.next_id += 1
            self.by_coid[p['newClientOrderId']] = resp
            return resp

    def get_order(self, symbol, origClientOrderId):
        self.fault('get_order')
        if origClientOrderId not in self.by_coid:
            raise api_error(-2013, 'Order does not exist.')
        r = dict(self.by_coid[origClientOrderId])
        r.pop('fills')
        return r

    def get_my_trades(self, symbol, orderId):
        self.fault('get_my_trades')
        return [f for r in self.by_coid.values() if r['orderId'] == orderId for f in r['fills']]

    def fetch_book(self, symbol):
        b = self.get_order_book(symbol)
        return {'bids': E.levels(b['bids']), 'asks': E.levels(b['asks'])}


# ---------------------------------------------------------------------------- tests
class Numbers(unittest.TestCase):
    def test_rounding(self):
        self.assertEqual(E.floor_to(D('1.23456'), D('0.001')), D('1.234'))
        self.assertEqual(E.ceil_to(D('1.23401'), D('0.001')), D('1.235'))
        self.assertEqual(E.floor_to(D('99.99'), D('1')), D('99'))

    def test_format_is_plain(self):
        self.assertEqual(E.fmt(D('1E+1'), D('1.00000000')), '10')
        self.assertEqual(E.fmt(D('0.8370000'), D('0.00100000')), '0.837')
        self.assertEqual(E.fmt(D('118.6'), D('0.01000000')), '118.60')
        self.assertEqual(E.fmt(D('1.000245'), D('0.00001000')), '1.00024')

    def test_limit_price_rounds_against_us(self):
        r = rules()['SOLUSDT']
        self.assertEqual(r.price('BUY', D('118.809')), D('118.80'))
        self.assertEqual(r.price('SELL', D('118.801')), D('118.81'))

    def test_filters(self):
        r = rules()
        self.assertIsNone(r['SOLUSDT'].check(D('0.837'), D('118.80')))
        self.assertIn('LOT_SIZE', r['FDUSDUSDT'].check(D('99.5'), D('0.999')))
        self.assertIn('minimum', r['SOLUSDT'].check(D('0.01'), D('118.80')))
        self.assertIn('PRICE_FILTER', r['SOLUSDT'].check(D('1'), D('118.805')))


class FeesAndRoutes(unittest.TestCase):
    def test_reads_commission_endpoint(self):
        fake = FakeExchange(FAIR, fee='0.00095')
        f = fees()
        f.load_from_account(fake)
        self.assertEqual(f.source, 'account')
        self.assertEqual(f.get('SOLUSDC', 'BUY'), D('0.00095'))

    def test_sums_standard_tax_and_buyer_seller(self):
        class C:
            def _get(self, path, signed, data):
                return {'standardCommission': {'taker': '0.001', 'buyer': '0.0001', 'seller': '0'},
                        'taxCommission': {'taker': '0.0002', 'buyer': '0', 'seller': '0.0003'},
                        'specialCommission': {'taker': '0', 'buyer': '0', 'seller': '0'}}
        f = fees()
        f.load_from_account(C())
        self.assertEqual(f.get('SOLUSDT', 'BUY'), D('0.0013'))
        self.assertEqual(f.get('SOLUSDT', 'SELL'), D('0.0015'))

    def test_stable_leg_direction(self):
        self.assertEqual(E.stable_leg('USDT', 'FDUSD'), ('FDUSDUSDT', 'SELL'))   # sell FDUSD for USDT
        self.assertEqual(E.stable_leg('FDUSD', 'USDT'), ('FDUSDUSDT', 'BUY'))    # buy FDUSD with USDT
        self.assertEqual(E.stable_leg('USDC', 'FDUSD'), ('FDUSDUSDC', 'SELL'))
        self.assertEqual(E.stable_leg('FDUSD', 'USDC'), ('FDUSDUSDC', 'BUY'))
        self.assertEqual(len(E.ROUTES), 6)


class Planning(unittest.TestCase):
    def check_plan_invariants(self, plan, budget, f=D('0.001')):
        self.assertLessEqual(plan.spend, budget)
        self.assertGreaterEqual(plan.q1 * (1 - f), plan.q2)              # SOL left after the fee covers leg 2
        self.assertEqual(plan.q3, plan.q3.to_integral_value())           # stable leg in whole units
        r = rules()
        for sym, q, p in ((plan.sa, plan.q1, plan.p1), (plan.sb, plan.q2, plan.p2), (plan.stable, plan.q3, plan.p3)):
            self.assertIsNone(r[sym].check(q, p))

    def test_fair_market_loses_about_the_fees(self):
        for a, b in E.ROUTES:
            plan, why = E.plan_route(a, b, D(100), as_levels(FAIR), rules(), fees())
            self.assertIsNone(why)
            self.check_plan_invariants(plan, D(100))
            self.assertLess(plan.pct, D('-0.25'), plan.name)
            self.assertGreater(plan.pct, D('-0.45'), plan.name)

    def test_real_snapshot_never_triggers(self):
        for a, b in E.ROUTES:
            plan, _ = E.plan_route(a, b, D(100), as_levels(REAL), rules(), fees())
            if plan:
                self.assertLess(plan.pct, D(E.DEFAULTS['min_profit']), plan.name)
                self.assertLess(plan.pct, 0, plan.name)

    def test_real_snapshot_zero_fees_is_tiny(self):
        # with no fees at all the real gaps are a few hundredths of a percent
        for a, b in E.ROUTES:
            plan, _ = E.plan_route(a, b, D(100), as_levels(REAL), rules(), fees('0'))
            if plan:
                self.assertLess(abs(plan.pct), D('0.08'), plan.name)

    def test_mispricing_is_found_and_is_exact(self):
        plan, why = E.plan_route('USDT', 'FDUSD', D(100), as_levels(MISPRICED), rules(), fees())
        self.assertIsNone(why)
        self.check_plan_invariants(plan, D(100))
        # independent recomputation of the worst case
        f = D('0.001')
        b_got = plan.q2 * plan.p2 * (1 - f)
        a_back = plan.q3 * plan.p3 * (1 - f)
        dust = (plan.q1 * (1 - f) - plan.q2) * plan.p2 * (1 - f) * plan.p3 * (1 - f) + (b_got - plan.q3) * plan.p3 * (1 - f)
        self.assertAlmostEqual(float(plan.profit), float(a_back + dust - plan.q1 * plan.p1), places=9)
        self.assertAlmostEqual(float(plan.cash_profit), float(a_back - plan.q1 * plan.p1), places=9)
        self.assertEqual(plan.pct, plan.cash_profit / plan.spend * 100)    # gate ignores dust
        self.assertLessEqual(plan.pct, plan.pct_with_dust)
        self.assertGreater(plan.pct, D('0.45'))
        self.assertLess(plan.pct, D('0.7'))

    def test_thin_stable_book_rejected(self):
        books = as_levels(MISPRICED)
        books['FDUSDUSDT']['bids'] = [(D('0.9990'), D('10'))]
        plan, why = E.plan_route('USDT', 'FDUSD', D(100), books, rules(), fees())
        self.assertIsNone(plan)
        self.assertIn('FDUSDUSDT', why)

    def test_dust_is_small(self):
        plan, _ = E.plan_route('USDT', 'FDUSD', D(100), as_levels(MISPRICED), rules(), fees())
        b_left = plan.q2 * plan.p2 * D('0.999') - plan.q3
        self.assertLess(b_left, D('0.2'))   # without the shave it can be up to ~1 FDUSD

    def test_thin_bids_rejected_with_cushion(self):
        thin = make_books({'USDT': '118.80', 'FDUSD': '120.00', 'USDC': '118.80'}, depth='0.3')
        plan, why = E.plan_route('USDT', 'FDUSD', D(100), as_levels(thin), rules(), fees(), cushion=E.DEPTH_CUSHION)
        self.assertIsNone(plan)
        self.assertIn('thin', why)

    def test_halted_symbol_skipped(self):
        r = rules()
        r['SOLFDUSD'].status = 'HALT'
        plan, why = E.plan_route('USDT', 'FDUSD', D(100), as_levels(MISPRICED), r, fees())
        self.assertIsNone(plan)
        self.assertIn('HALT', why)


class Execution(unittest.TestCase):
    def setUp(self):
        self.fake = FakeExchange(MISPRICED)
        self.rules, self.fees = rules(), fees()
        self.plan, _ = E.plan_route('USDT', 'FDUSD', D(100), as_levels(MISPRICED), self.rules, self.fees,
                                    cushion=E.DEPTH_CUSHION)
        self.start = dict(self.fake.bal)

    def run_plan(self):
        return E.execute(self.fake, self.plan, self.rules, self.fees, self.fake.fetch_book, leg2_wait=0.3)

    def test_full_cycle_matches_plan(self):
        res = self.run_plan()
        self.assertEqual(res.outcome, 'DONE', res.notes)
        self.assertEqual([o['symbol'] for o in self.fake.orders], ['SOLUSDT', 'SOLFDUSD', 'FDUSDUSDT'])
        self.assertEqual(self.fake.orders[0]['timeInForce'], 'FOK')
        self.assertAlmostEqual(float(res.pnl), float(self.plan.profit), delta=0.01)
        self.assertGreaterEqual(res.a_received - res.a_spent, self.plan.cash_profit)
        # the account really gained what we say it did
        self.assertEqual(self.fake.bal['USDT'] - self.start['USDT'], res.a_received - res.a_spent)
        self.assertGreater(self.fake.bal['USDT'], self.start['USDT'])

    def test_orders_are_well_formed(self):
        self.run_plan()
        o1, o2, o3 = self.fake.orders
        self.assertRegex(o1['quantity'], r'^\d+\.\d{3}$')
        self.assertRegex(o1['price'], r'^\d+\.\d{2}$')
        self.assertRegex(o3['quantity'], r'^\d+$')            # whole FDUSD
        self.assertRegex(o3['price'], r'^0\.\d{4}$')
        self.assertTrue(all(o['newClientOrderId'].startswith('arb') and len(o['newClientOrderId']) <= 36
                            for o in self.fake.orders))

    def test_price_moved_before_leg1_costs_nothing(self):
        def jump(p):
            if p['symbol'] == 'SOLUSDT' and p['side'] == 'BUY':
                for lv in self.fake.books['SOLUSDT']['asks']:
                    lv[0] += 1
        self.fake.before_order = jump
        res = self.run_plan()
        self.assertEqual(res.outcome, 'NO_FILL')
        self.assertEqual(len(self.fake.orders), 1)
        self.assertEqual(self.fake.bal, self.start)
        self.assertEqual(res.pnl, 0)

    def test_leg1_rejected_costs_nothing(self):
        self.fake.fail_next = (api_error(-2010, 'Account has insufficient balance for requested action.'), False)
        res = self.run_plan()
        self.assertEqual(res.outcome, 'REJECTED')
        self.assertEqual(self.fake.bal, self.start)

    def test_lost_response_is_reconciled_not_resent(self):
        self.fake.fail_next = (requests.exceptions.ReadTimeout('timed out'), True)
        res = self.run_plan()
        self.assertEqual(res.outcome, 'DONE', res.notes)
        self.assertEqual(len(self.fake.orders), 3)      # leg 1 was looked up, not sent twice
        self.assertAlmostEqual(float(res.pnl), float(self.plan.profit), delta=0.01)

    def test_order_never_arrived_is_unknown(self):
        self.fake.fail_next = (requests.exceptions.ConnectionError('reset'), False)
        res = self.run_plan()
        self.assertEqual(res.outcome, 'UNKNOWN')
        self.assertEqual(len(self.fake.orders), 1)
        self.assertEqual(self.fake.bal, self.start)

    def test_server_error_is_reconciled(self):
        self.fake.fail_next = (api_error(-1007, 'Timeout waiting for response', status=503), True)
        res = self.run_plan()
        self.assertEqual(res.outcome, 'DONE', res.notes)

    def test_leg2_partial_fill_unwinds_with_capped_loss(self):
        # someone takes most of the FDUSD bids between leg 1 and leg 2
        def drain(p):
            if p['symbol'] == 'SOLFDUSD':
                self.fake.books['SOLFDUSD']['bids'] = [[D('120.00'), D('0.3')], [D('110.00'), D('100')]]
        self.fake.before_order = drain
        res = self.run_plan()
        sells_back = [o for o in self.fake.orders if o['symbol'] == 'SOLUSDT' and o['side'] == 'SELL']
        self.assertEqual(len(sells_back), 1)
        self.assertEqual(res.outcome, 'DONE', res.notes)
        self.assertLess(res.sol_left, D('0.001'))
        # 0.3 SOL sold at +1%, the rest sold back at about cost: still a small profit
        self.assertGreater(res.pnl, D('-0.5'))
        # never sold below the break-even floor on SOL/FDUSD
        for o in self.fake.orders:
            if o['symbol'] == 'SOLFDUSD':
                self.assertGreaterEqual(D(o['price']), D('118.9'))

    def test_leg2_and_unwind_impossible_holds_sol(self):
        def crash(p):
            if p['symbol'] == 'SOLFDUSD':
                self.fake.books['SOLFDUSD']['bids'] = [[D('100.00'), D('100')]]
                self.fake.books['SOLUSDT']['bids'] = [[D('100.00'), D('100')]]
        self.fake.before_order = crash
        res = self.run_plan()
        self.assertEqual(res.outcome, 'HOLDING_SOL')
        self.assertGreater(res.sol_left, D('0.8'))
        self.assertLess(res.pnl, D('-10'))     # valued at today's crashed price, not the plan
        # nothing was dumped at the crashed price
        self.assertFalse(any(D(o['price']) < D('118') for o in self.fake.orders if o['side'] == 'SELL'))

    def test_leg3_unfilled_keeps_stablecoin(self):
        def drop(p):
            if p['symbol'] == 'FDUSDUSDT':
                self.fake.books['FDUSDUSDT']['bids'] = [[D('0.9900'), D('1000000')]]
        self.fake.before_order = drop
        res = self.run_plan()
        self.assertEqual(res.outcome, 'HOLDING_FDUSD')
        self.assertGreater(res.b_left, D('99'))
        self.assertLess(res.pnl, self.plan.profit - D('0.5'))   # FDUSD valued at the lower price
        self.assertEqual(sum(1 for o in self.fake.orders if o['symbol'] == 'FDUSDUSDT'), 2)


    def test_lookup_rate_limited_after_fill_is_unknown(self):
        self.fake.fail_next = (requests.exceptions.ReadTimeout('timed out'), True)
        self.fake.faults['get_order'] = [api_error(-1003, 'Too many requests', status=429)] * 10
        res = self.run_plan()
        self.assertEqual(res.outcome, 'UNKNOWN')      # not REJECTED: the buy really happened
        self.assertEqual(len(self.fake.orders), 1)

    def test_trades_lookup_failing_is_unknown(self):
        self.fake.fail_next = (requests.exceptions.ReadTimeout('timed out'), True)
        self.fake.faults['get_my_trades'] = [requests.exceptions.ReadTimeout('timed out')] * 10
        res = self.run_plan()
        self.assertEqual(res.outcome, 'UNKNOWN')

    def test_lost_leg2_response_is_reconciled(self):
        def lose_leg2(p):
            if p['symbol'] == 'SOLFDUSD':
                self.fake.fail_next = (requests.exceptions.ReadTimeout('timed out'), True)
        self.fake.before_order = lose_leg2
        res = self.run_plan()
        self.assertEqual(res.outcome, 'DONE', res.notes)
        self.assertEqual(len(self.fake.orders), 3)

    def test_book_errors_during_leg2_still_unwind(self):
        def drain(p):
            if p['symbol'] == 'SOLFDUSD':
                self.fake.books['SOLFDUSD']['bids'] = [[D('120.00'), D('0.3')], [D('110.00'), D('100')]]
                self.fake.faults['get_order_book'] = [requests.exceptions.ConnectionError('reset')] * 100
        self.fake.before_order = drain
        res = self.run_plan()
        self.assertNotEqual(res.outcome, 'UNKNOWN', res.notes)
        self.assertTrue(any(o['symbol'] == 'SOLUSDT' and o['side'] == 'SELL' for o in self.fake.orders))
        self.assertLess(res.sol_left, D('0.001'))

    def test_leg2_rejected_sells_sol_back(self):
        self.fake.reject['SOLFDUSD'] = api_error(-2010, 'This symbol is not permitted for this account.')
        res = self.run_plan()
        self.assertEqual(res.outcome, 'LEG_REJECTED')
        self.assertTrue(any(o['symbol'] == 'SOLUSDT' and o['side'] == 'SELL' for o in self.fake.orders))
        self.assertLess(res.sol_left, D('0.001'))
        self.assertGreater(res.pnl, D('-0.6'))     # bounded by the unwind loss cap and fees

    def test_buy_side_leg3(self):
        fake = FakeExchange(MISPRICED_USDT)
        plan, why = E.plan_route('FDUSD', 'USDT', D(100), as_levels(MISPRICED_USDT), self.rules, self.fees,
                                 cushion=E.DEPTH_CUSHION)
        self.assertIsNone(why)
        self.assertEqual((plan.stable, plan.side3), ('FDUSDUSDT', 'BUY'))
        start = dict(fake.bal)
        res = E.execute(fake, plan, self.rules, self.fees, fake.fetch_book, leg2_wait=0.3)
        self.assertEqual(res.outcome, 'DONE', res.notes)
        o3 = fake.orders[2]
        self.assertEqual((o3['symbol'], o3['side']), ('FDUSDUSDT', 'BUY'))
        self.assertRegex(o3['quantity'], r'^\d+$')
        self.assertEqual(fake.bal['FDUSD'] - start['FDUSD'], res.a_received - res.a_spent)
        self.assertGreater(fake.bal['FDUSD'], start['FDUSD'])
        self.assertAlmostEqual(float(res.pnl), float(plan.profit), delta=0.01)


class PaperFill(unittest.TestCase):
    def setUp(self):
        self.rules, self.fees = rules(), fees()
        self.plan, _ = E.plan_route('USDT', 'FDUSD', D(100), as_levels(MISPRICED), self.rules, self.fees)

    def test_unchanged_market(self):
        outcome, pnl = E.paper_fill(self.plan, as_levels(MISPRICED), self.rules, self.fees)
        self.assertEqual(outcome, 'DONE')
        self.assertAlmostEqual(float(pnl), float(self.plan.profit), delta=0.02)

    def test_crash_after_plan_shows_the_loss(self):
        crashed = as_levels(MISPRICED)
        crashed['SOLFDUSD']['bids'] = [(D('100.00'), D('100'))]
        crashed['SOLUSDT']['bids'] = [(D('100.00'), D('100'))]
        outcome, pnl = E.paper_fill(self.plan, crashed, self.rules, self.fees)
        self.assertEqual(outcome, 'HOLDING_SOL')
        self.assertLess(pnl, D('-10'))

    def test_moved_market(self):
        moved = make_books({'USDT': '119.50', 'FDUSD': '120.00', 'USDC': '118.80'})
        outcome, pnl = E.paper_fill(self.plan, as_levels(moved), self.rules, self.fees)
        self.assertEqual(outcome, 'NO_FILL')
        self.assertEqual(pnl, 0)


def run_engine(fake, seconds=0.8, keys=True, **settings):
    s = dict(E.DEFAULTS, interval=0.05)
    s.update(settings)
    events, stop = [], threading.Event()
    eng = E.Engine(s, lambda k, d: events.append((k, d)), stop,
                   api_key='k' * 24 if keys else None, api_secret='s' * 24 if keys else None,
                   market_data_url='fake', client_factory=lambda *a, **k: fake)
    t = threading.Thread(target=eng.run)
    t.start()
    stop.wait(seconds)
    stop.set()
    t.join(10)
    return eng, events


def of(events, kind):
    return [d for k, d in events if k == kind]


class EngineLoop(unittest.TestCase):
    def test_fair_market_does_nothing(self):
        fake = FakeExchange(FAIR)
        eng, ev = run_engine(fake, dry_run=False)
        self.assertEqual(fake.orders, [])
        self.assertEqual(of(ev, 'trade'), [])
        ticks = of(ev, 'tick')
        self.assertGreater(len(ticks), 3)
        self.assertTrue(all(r['pct'] < 0 for r in ticks[-1]['routes']))
        self.assertEqual(eng.fees.source, 'account')

    def test_dry_run_never_places_orders(self):
        fake = FakeExchange(MISPRICED)
        eng, ev = run_engine(fake, dry_run=True)
        self.assertEqual(fake.orders, [])
        trades = of(ev, 'trade')
        self.assertEqual(len(trades), 1)                 # then the cooldown holds
        self.assertFalse(trades[0]['live'])
        self.assertGreater(trades[0]['pnl'], 0)

    def test_live_trades_once_then_cools_down(self):
        fake = FakeExchange(MISPRICED)
        eng, ev = run_engine(fake, dry_run=False)
        trades = of(ev, 'trade')
        self.assertEqual(len(trades), 1)
        self.assertTrue(trades[0]['live'])
        self.assertEqual(trades[0]['outcome'], 'DONE')
        self.assertGreater(trades[0]['pnl'], 0)
        self.assertEqual(len(fake.orders), 3)
        self.assertEqual(fake.orders[0]['side'], 'BUY')

    def test_live_without_keys_refuses(self):
        fake = FakeExchange(MISPRICED)
        eng, ev = run_engine(fake, keys=False, dry_run=False)
        self.assertEqual(fake.orders, [])
        self.assertIn('API keys', of(ev, 'stopped')[0])

    def test_not_enough_balance_skips(self):
        fake = FakeExchange(MISPRICED, balances={'USDT': '10', 'FDUSD': '10', 'USDC': '10', 'SOL': '0', 'BNB': '0'})
        eng, ev = run_engine(fake, dry_run=False)
        self.assertEqual(fake.orders, [])
        self.assertTrue(any('not enough' in d[1] for d in of(ev, 'log')))

    def test_max_trades_stops_the_session(self):
        fake = FakeExchange(MISPRICED)
        eng, ev = run_engine(fake, dry_run=True, cooldown=0, max_trades=2)
        self.assertEqual(len(of(ev, 'trade')), 2)
        self.assertIn('2 trades', of(ev, 'stopped')[0])

    def test_holding_stablecoin_stops_the_session(self):
        fake = FakeExchange(MISPRICED)
        def drop(p):
            if p['symbol'] == 'FDUSDUSDT':
                fake.books['FDUSDUSDT']['bids'] = [[D('0.9800'), D('1000000')]]
        fake.before_order = drop
        eng, ev = run_engine(fake, dry_run=False, cooldown=0)
        self.assertEqual(len(of(ev, 'trade')), 1)
        self.assertIn('HOLDING_FDUSD', of(ev, 'stopped')[0])

    def test_unknown_order_stops_the_session(self):
        fake = FakeExchange(MISPRICED)
        fake.fail_next = (requests.exceptions.ConnectionError('reset'), False)
        eng, ev = run_engine(fake, dry_run=False, cooldown=0)
        self.assertIn('UNKNOWN', of(ev, 'stopped')[0])
        self.assertEqual(len(fake.orders), 1)
        self.assertIsNone(of(ev, 'trade')[0]['pnl'])

    def test_bad_key_on_order_stops_the_session(self):
        fake = FakeExchange(MISPRICED)
        fake.reject['SOLUSDT'] = api_error(-2015, 'Invalid API-key, IP, or permissions for action.', 401)
        fake.reject['SOLUSDC'] = fake.reject['SOLUSDT']
        eng, ev = run_engine(fake, dry_run=False, cooldown=0)
        self.assertEqual(len(fake.orders), 1)
        self.assertIn('refused', of(ev, 'stopped')[0])

    def test_no_live_trades_without_real_fees(self):
        fake = FakeExchange(MISPRICED)
        fake.commission_error = api_error(-2015, 'Invalid API-key, IP, or permissions for action.', 401)
        eng, ev = run_engine(fake, dry_run=False)
        self.assertEqual(fake.orders, [])
        self.assertEqual(eng.fees.source, 'fallback')
        self.assertTrue(any('real fees' in d[1] for d in of(ev, 'log')))

    def test_missing_commission_block_is_not_zero_fee(self):
        class C:
            def _get(self, path, signed, data):
                return {'taxCommission': {'taker': '0', 'buyer': '0', 'seller': '0'}}
        f = fees()
        with self.assertRaises(KeyError):
            f.load_from_account(C())
        self.assertEqual(f.source, 'fallback')

    def test_trade_event_carries_updated_stats(self):
        fake = FakeExchange(MISPRICED)
        eng, ev = run_engine(fake, dry_run=False)
        tr = of(ev, 'trade')[0]
        self.assertEqual(tr['stats']['trades'], 1)
        self.assertAlmostEqual(tr['stats']['pnl'], tr['pnl'])

    def test_loss_limit_stops_the_session(self):
        eng = E.Engine(dict(E.DEFAULTS), lambda k, d: None, threading.Event())
        plan, _ = E.plan_route('USDT', 'FDUSD', D(100), as_levels(MISPRICED), rules(), fees())
        eng.record(plan, 'DONE', D('-0.9'), live=True, fills=3)
        self.assertIsNone(eng.halted)
        eng.record(plan, 'DONE', D('-0.7'), live=True, fills=3)
        self.assertIn('loss limit', eng.halted)

    def test_failed_leg1_is_not_counted_as_a_trade(self):
        eng = E.Engine(dict(E.DEFAULTS), lambda k, d: None, threading.Event())
        plan, _ = E.plan_route('USDT', 'FDUSD', D(100), as_levels(MISPRICED), rules(), fees())
        eng.record(plan, 'NO_FILL', D(0), live=True, fills=1)
        self.assertEqual(eng.stats['trades'], 0)


if __name__ == '__main__':
    unittest.main()

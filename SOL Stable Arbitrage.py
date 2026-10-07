from binance.client import Client
from decimal import Decimal, ROUND_DOWN
import time
import key

# API key/secret are required for user data endpoints
client = Client(key.api_key, key.api_secret)

#settings (add these to key.py to override the defaults)
COIN = getattr(key, 'coin', 'SOL')
QUOTES = getattr(key, 'quotes', ['USDT', 'FDUSD', 'USDC'])
SIZE = getattr(key, 'size', 100)                      # trade size, in the starting stablecoin
FEE = getattr(key, 'fee', 0.001)                      # taker fee per trade (0.1%)
FEE_OVERRIDES = getattr(key, 'fee_overrides', {})     # e.g. {'SOLFDUSD': 0.0}
MIN_PROFIT = getattr(key, 'min_profit', 0.0005)       # 0.05% net profit after fees
DRY_RUN = getattr(key, 'dry_run', True)               # True = only print, never place orders
SLEEP = getattr(key, 'sleep', 5)

#logging
log = open("log.txt", "a")

#exchange info: which symbols exist, and their lot sizes
symbols = {}
for s in client.get_exchange_info()['symbols']:
    if s['status'] == 'TRADING':
        symbols[s['symbol']] = s

def fee(symbol):
    return FEE_OVERRIDES.get(symbol, FEE)

def step_size(symbol):
    for f in symbols[symbol]['filters']:
        if f['filterType'] == 'LOT_SIZE':
            return Decimal(f['stepSize'])
    return Decimal('0.00000001')

def round_qty(symbol, qty):
    return Decimal(str(qty)).quantize(step_size(symbol).normalize(), rounding=ROUND_DOWN)

def round_quote(symbol, qty):
    digits = symbols[symbol]['quoteAssetPrecision']
    return Decimal(str(qty)).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_DOWN)

def top(symbol):
    #best bid/ask price and size, or None if the book is empty
    book = client.get_order_book(symbol=symbol, limit=5)
    if not book['bids'] or not book['asks']:
        return None
    return {'bid': float(book['bids'][0][0]), 'bid_qty': float(book['bids'][0][1]),
            'ask': float(book['asks'][0][0]), 'ask_qty': float(book['asks'][0][1])}

def stable_pair(frm, to):
    #returns (symbol, side) to convert frm -> to, or None if Binance has no pair
    if frm + to in symbols:
        return frm + to, 'SELL'    # frm is the base: sell it
    if to + frm in symbols:
        return to + frm, 'BUY'     # to is the base: buy it with frm
    return None

def convert_rate(frm, to, books):
    #how much 'to' you get for 1 'frm', after fees
    pair = stable_pair(frm, to)
    if pair is None or books.get(pair[0]) is None:
        return None
    symbol, side = pair
    b = books[symbol]
    rate = b['bid'] if side == 'SELL' else 1 / b['ask']
    return rate * (1 - fee(symbol))

def fills_net(order, asset):
    #commission paid in 'asset' on this order
    return sum(float(f['commission']) for f in order.get('fills', []) if f['commissionAsset'] == asset)

def market(symbol, side, qty=None, quote_qty=None):
    if quote_qty is not None:
        order = client.create_order(symbol=symbol, side=side, type='MARKET',
                                    quoteOrderQty=str(round_quote(symbol, quote_qty)))
    else:
        order = client.create_order(symbol=symbol, side=side, type='MARKET',
                                    quantity=str(round_qty(symbol, qty)))
    log.write(str(order) + "\n")
    log.flush()
    return order

def execute(a, b):
    #1. buy COIN with a
    buy_sym = COIN + a
    o1 = market(buy_sym, 'BUY', quote_qty=SIZE)
    coin_qty = float(o1['executedQty']) - fills_net(o1, COIN)

    #2. sell COIN for b
    sell_sym = COIN + b
    o2 = market(sell_sym, 'SELL', qty=coin_qty)
    b_qty = float(o2['cummulativeQuoteQty']) - fills_net(o2, b)

    #3. convert b back to a
    symbol, side = stable_pair(b, a)
    if side == 'SELL':
        market(symbol, 'SELL', qty=b_qty)
    else:
        market(symbol, 'BUY', quote_qty=b_qty)

#which symbols we need to watch
watch = []
for q in QUOTES:
    if COIN + q in symbols:
        watch.append(COIN + q)
    else:
        print("Warning: " + COIN + q + " is not trading on Binance, skipping")
for a in QUOTES:
    for b in QUOTES:
        if a != b:
            pair = stable_pair(a, b)
            if pair is None:
                print("Warning: no " + a + "/" + b + " pair to convert between them")
            elif pair[0] not in watch:
                watch.append(pair[0])

while(key.loop == 'true'):
    books = {}
    for s in watch:
        books[s] = top(s)
        if books[s] is None:
            print(s + ": empty order book")
        else:
            print(s + "  Bid: " + str(books[s]['bid']) + "  Ask: " + str(books[s]['ask']))

    best = None
    for a in QUOTES:
        for b in QUOTES:
            if a == b:
                continue
            buy, sell = books.get(COIN + a), books.get(COIN + b)
            back = convert_rate(b, a, books)
            if buy is None or sell is None or back is None:
                continue

            # start with SIZE of a -> COIN -> b -> back to a
            coin_qty = SIZE / buy['ask'] * (1 - fee(COIN + a))
            b_qty = coin_qty * sell['bid'] * (1 - fee(COIN + b))
            end = b_qty * back
            profit = end / SIZE - 1

            # top of book must be deep enough, otherwise the market order slips
            deep = coin_qty <= buy['ask_qty'] and coin_qty <= sell['bid_qty']

            print("%s -> %s -> %s -> %s: %+.4f%%%s" % (a, COIN, b, a, profit * 100,
                  "" if deep else " (not enough depth)"))
            if deep and (best is None or profit > best[2]):
                best = (a, b, profit)

    if best is not None and best[2] > MIN_PROFIT:
        a, b, profit = best
        free = float(client.get_asset_balance(asset=a)['free'])
        if DRY_RUN:
            print("DRY RUN: would trade %s -> %s -> %s -> %s for %+.4f%%" % (a, COIN, b, a, profit * 100))
        elif free < SIZE:
            print("Opportunity found, but only " + str(free) + " " + a + " available")
        else:
            print("Executing " + a + " -> " + COIN + " -> " + b + " -> " + a)
            execute(a, b)
    print("____________________________________________________________________________")
    time.sleep(SLEEP)

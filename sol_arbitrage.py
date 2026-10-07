from binance.client import Client
from decimal import Decimal, ROUND_DOWN
from tkinter import ttk, messagebox, scrolledtext
import queue
import threading
import time
import tkinter as tk
import key

#Coin to arbitrage, and the stablecoins it is quoted in
COIN = 'SOL'
QUOTES = ['USDT', 'FDUSD', 'USDC']

#Per-symbol fee overrides, e.g. fee_overrides = {'SOLFDUSD': 0.0} in key.py
FEE_OVERRIDES = getattr(key, 'fee_overrides', {})


class Arbitrage:
    """Prices, route maths and order placing. No UI code in here."""

    def __init__(self, client):
        self.client = client
        self.symbols = {s['symbol']: s for s in client.get_exchange_info()['symbols']
                        if s['status'] == 'TRADING'}

        #SOL pairs, plus the stablecoin pairs needed to convert between quotes
        self.watch = [COIN + q for q in QUOTES if COIN + q in self.symbols]
        for a in QUOTES:
            for b in QUOTES:
                pair = self.stable_pair(a, b) if a != b else None
                if pair and pair[0] not in self.watch:
                    self.watch.append(pair[0])

    def stable_pair(self, frm, to):
        #(symbol, side) to convert frm -> to, or None if Binance has no such pair
        if frm + to in self.symbols:
            return frm + to, 'SELL'
        if to + frm in self.symbols:
            return to + frm, 'BUY'
        return None

    def fee(self, symbol, settings):
        return FEE_OVERRIDES.get(symbol, settings['fee'])

    def books(self):
        #best bid/ask per symbol, None if the book is empty
        books = {}
        for s in self.watch:
            t = self.client.get_orderbook_ticker(symbol=s)
            bid, ask = float(t['bidPrice']), float(t['askPrice'])
            if bid <= 0 or ask <= 0:
                books[s] = None
            else:
                books[s] = {'bid': bid, 'bid_qty': float(t['bidQty']),
                            'ask': ask, 'ask_qty': float(t['askQty'])}
        return books

    def routes(self, books, settings):
        #every route a -> SOL -> b -> back to a, with net profit after fees and spread
        size = settings['size']
        routes = []
        for a in QUOTES:
            for b in QUOTES:
                if a == b:
                    continue
                buy, sell = books.get(COIN + a), books.get(COIN + b)
                pair = self.stable_pair(b, a)
                if buy is None or sell is None or pair is None or books.get(pair[0]) is None:
                    continue
                conv = books[pair[0]]
                rate = conv['bid'] if pair[1] == 'SELL' else 1 / conv['ask']

                coin_qty = size / buy['ask'] * (1 - self.fee(COIN + a, settings))
                b_qty = coin_qty * sell['bid'] * (1 - self.fee(COIN + b, settings))
                end = b_qty * rate * (1 - self.fee(pair[0], settings))

                #top of book must hold the whole trade, otherwise the market order slips
                deep = coin_qty <= buy['ask_qty'] and coin_qty <= sell['bid_qty']
                routes.append({'a': a, 'b': b, 'profit': end / size - 1, 'deep': deep})
        return routes

    def balance(self, asset):
        return float(self.client.get_asset_balance(asset=asset)['free'])

    def round_qty(self, symbol, qty):
        step = Decimal('0.00000001')
        for f in self.symbols[symbol]['filters']:
            if f['filterType'] == 'LOT_SIZE':
                step = Decimal(f['stepSize']).normalize()
        return str(Decimal(str(qty)).quantize(step, rounding=ROUND_DOWN))

    def round_quote(self, symbol, qty):
        digits = self.symbols[symbol]['quoteAssetPrecision']
        return str(Decimal(str(qty)).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_DOWN))

    def market(self, symbol, side, qty=None, quote_qty=None):
        if quote_qty is not None:
            return self.client.create_order(symbol=symbol, side=side, type='MARKET',
                                            quoteOrderQty=self.round_quote(symbol, quote_qty))
        return self.client.create_order(symbol=symbol, side=side, type='MARKET',
                                        quantity=self.round_qty(symbol, qty))

    def execute(self, a, b, size):
        #three market orders: a -> SOL, SOL -> b, b -> a. Returns the order responses.
        def commission(order, asset):
            return sum(float(f['commission']) for f in order.get('fills', [])
                       if f['commissionAsset'] == asset)

        o1 = self.market(COIN + a, 'BUY', quote_qty=size)
        coin_qty = float(o1['executedQty']) - commission(o1, COIN)

        o2 = self.market(COIN + b, 'SELL', qty=coin_qty)
        b_qty = float(o2['cummulativeQuoteQty']) - commission(o2, b)

        symbol, side = self.stable_pair(b, a)
        if side == 'SELL':
            o3 = self.market(symbol, 'SELL', qty=b_qty)
        else:
            o3 = self.market(symbol, 'BUY', quote_qty=b_qty)
        return [o1, o2, o3]


class App:
    def __init__(self, root):
        self.root = root
        self.arb = None
        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.thread = None
        self.logfile = open('log.txt', 'a')

        root.title(COIN + ' Arbitrage')
        root.geometry('640x620')

        #settings
        box = ttk.LabelFrame(root, text='Settings', padding=8)
        box.pack(fill='x', padx=8, pady=(8, 4))
        self.size = tk.StringVar(value=str(getattr(key, 'size', 100)))
        self.fee = tk.StringVar(value=str(getattr(key, 'fee', 0.001) * 100))
        self.min_profit = tk.StringVar(value=str(getattr(key, 'min_profit', 0.0005) * 100))
        self.interval = tk.StringVar(value=str(getattr(key, 'sleep', 5)))
        self.dry_run = tk.BooleanVar(value=True)
        fields = [('Trade size', self.size), ('Fee % per trade', self.fee),
                  ('Min profit %', self.min_profit), ('Check every (s)', self.interval)]
        for i, (label, var) in enumerate(fields):
            ttk.Label(box, text=label).grid(row=0, column=i, sticky='w', padx=4)
            ttk.Entry(box, textvariable=var, width=12).grid(row=1, column=i, padx=4)
        ttk.Checkbutton(box, text='Dry run (no real orders)', variable=self.dry_run).grid(
            row=2, column=0, columnspan=2, sticky='w', padx=4, pady=(6, 0))
        self.start_btn = ttk.Button(box, text='Start', command=self.start)
        self.start_btn.grid(row=2, column=2, pady=(6, 0))
        self.stop_btn = ttk.Button(box, text='Stop', command=self.stop, state='disabled')
        self.stop_btn.grid(row=2, column=3, pady=(6, 0))

        #prices
        box = ttk.LabelFrame(root, text='Prices', padding=4)
        box.pack(fill='x', padx=8, pady=4)
        self.prices = ttk.Treeview(box, columns=('bid', 'ask'), height=6)
        self.prices.heading('#0', text='Pair')
        self.prices.heading('bid', text='Bid')
        self.prices.heading('ask', text='Ask')
        self.prices.pack(fill='x')

        #routes
        box = ttk.LabelFrame(root, text='Routes (net, after fees and spread)', padding=4)
        box.pack(fill='x', padx=8, pady=4)
        self.routes = ttk.Treeview(box, columns=('profit', 'note'), height=6)
        self.routes.heading('#0', text='Route')
        self.routes.heading('profit', text='Profit')
        self.routes.heading('note', text='Note')
        self.routes.column('#0', width=260)
        self.routes.tag_configure('win', foreground='green')
        self.routes.tag_configure('loss', foreground='red')
        self.routes.pack(fill='x')

        #log
        box = ttk.LabelFrame(root, text='Log', padding=4)
        box.pack(fill='both', expand=True, padx=8, pady=(4, 8))
        self.log_box = scrolledtext.ScrolledText(box, height=8, state='disabled')
        self.log_box.pack(fill='both', expand=True)

        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(200, self.poll)

    def log(self, text):
        line = time.strftime('%H:%M:%S ') + text
        self.log_box.configure(state='normal')
        self.log_box.insert('end', line + '\n')
        self.log_box.see('end')
        self.log_box.configure(state='disabled')
        self.logfile.write(line + '\n')
        self.logfile.flush()

    def start(self):
        try:
            settings = {'size': float(self.size.get()),
                        'fee': float(self.fee.get()) / 100,
                        'min_profit': float(self.min_profit.get()) / 100,
                        'interval': float(self.interval.get()),
                        'dry_run': self.dry_run.get()}
        except ValueError:
            messagebox.showerror('Settings', 'All settings must be numbers.')
            return
        if not settings['dry_run'] and not messagebox.askyesno(
                'Live trading', 'Dry run is off. This will place REAL market orders. Continue?'):
            return
        self.stop_event.clear()
        self.start_btn.configure(state='disabled')
        self.stop_btn.configure(state='normal')
        self.log('Started (%s)' % ('dry run' if settings['dry_run'] else 'LIVE'))
        self.thread = threading.Thread(target=self.worker, args=(settings,), daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.stop_btn.configure(state='disabled')

    def close(self):
        self.stop_event.set()
        self.root.destroy()

    def worker(self, settings):
        #runs in a background thread; talks to the UI only through self.events
        post = lambda kind, data: self.events.put((kind, data))
        if self.arb is None:
            try:
                post('log', 'Connecting to Binance...')
                self.arb = Arbitrage(Client(key.api_key, key.api_secret))
                post('log', 'Watching: ' + ', '.join(self.arb.watch))
            except Exception as e:
                post('log', 'Could not connect: ' + str(e))
                post('stopped', None)
                return

        while not self.stop_event.is_set():
            try:
                books = self.arb.books()
                routes = self.arb.routes(books, settings)
                post('update', (books, routes))

                usable = [r for r in routes if r['deep']]
                best = max(usable, key=lambda r: r['profit']) if usable else None
                if best and best['profit'] > settings['min_profit']:
                    a, b = best['a'], best['b']
                    route = '%s -> %s -> %s -> %s (%+.4f%%)' % (a, COIN, b, a, best['profit'] * 100)
                    if settings['dry_run']:
                        post('log', 'DRY RUN: would trade ' + route)
                    elif self.arb.balance(a) < settings['size']:
                        post('log', 'Opportunity %s, but not enough %s' % (route, a))
                    else:
                        post('log', 'Executing ' + route)
                        for order in self.arb.execute(a, b, settings['size']):
                            post('log', '%s %s qty=%s quote=%s' % (
                                order['symbol'], order['side'], order['executedQty'],
                                order['cummulativeQuoteQty']))
            except Exception as e:
                post('log', 'Error: ' + str(e))
            self.stop_event.wait(settings['interval'])
        post('stopped', None)

    def poll(self):
        #apply worker results to the UI (Tkinter must only be touched from this thread)
        while not self.events.empty():
            kind, data = self.events.get()
            if kind == 'log':
                self.log(data)
            elif kind == 'stopped':
                self.log('Stopped')
                self.start_btn.configure(state='normal')
                self.stop_btn.configure(state='disabled')
            elif kind == 'update':
                books, routes = data
                self.prices.delete(*self.prices.get_children())
                for s, b in books.items():
                    if b is None:
                        self.prices.insert('', 'end', text=s, values=('empty', 'empty'))
                    else:
                        self.prices.insert('', 'end', text=s, values=(b['bid'], b['ask']))
                self.routes.delete(*self.routes.get_children())
                for r in sorted(routes, key=lambda r: -r['profit']):
                    self.routes.insert('', 'end', text='%s -> %s -> %s -> %s' % (r['a'], COIN, r['b'], r['a']),
                                       values=('%+.4f%%' % (r['profit'] * 100),
                                               '' if r['deep'] else 'not enough depth'),
                                       tags=('win' if r['profit'] > 0 else 'loss',))
        self.root.after(200, self.poll)


if __name__ == '__main__':
    root = tk.Tk()
    App(root)
    root.mainloop()

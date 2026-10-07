"""SOL Arbitrage: a window around arb_engine.py.

Run:  python sol_arbitrage.py
Put your Binance API key/secret in key.py for real fees, balances and live trading.
Without keys it runs on public market data in dry run (paper trading) mode.
"""
import math
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, font as tkfont

import arb_engine as E
import key

# colours (dark theme, Solana accents)
BG, PANEL, FIELD, BORDER = '#0d1117', '#161b22', '#0b0f14', '#2a313c'
TEXT, MUTED, DIM = '#e6edf3', '#8b949e', '#5b636d'
PURPLE, PURPLE_HI, GREEN, RED, AMBER, BLUE = '#9945ff', '#ad6bff', '#3fb950', '#f85149', '#d29922', '#58a6ff'

FIELDS = [  # key, label, hint
    ('size', 'Trade size', 'Amount of the starting stablecoin spent per trade'),
    ('min_profit', 'Min profit %', 'Only trade if the route still makes this much when every\n'
                                   'order fills at its worst allowed price, after all fees'),
    ('interval', 'Check every (s)', 'How often prices are read'),
    ('fallback_fee', 'Fallback fee %', 'Taker fee per order, used when your real fees\n'
                                       'cannot be read (no API keys)'),
    ('cooldown', 'Cooldown (s)', 'Pause after each trade'),
    ('max_loss', 'Stop at loss ($)', 'Stop the session once it has lost this much'),
    ('max_trades', 'Max trades', 'Stop the session after this many trades'),
]
HISTORY = 300   # points on the chart


class Tooltip:
    """Small hint shown while the mouse is over a widget."""

    def __init__(self, widget, text, font):
        self.widget, self.text, self.font, self.tip = widget, text, font, None
        widget.bind('<Enter>', self.show, add='+')
        widget.bind('<Leave>', self.hide, add='+')

    def show(self, _):
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f'+{x}+{y}')
        tk.Label(self.tip, text=self.text, bg='#2d333b', fg=TEXT, font=self.font, padx=8, pady=4).pack()

    def hide(self, _):
        if self.tip:
            self.tip.destroy()
            self.tip = None


def pick_font(*names):
    available = set(tkfont.families())
    return next((n for n in names if n in available), 'TkDefaultFont')


class App:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.stop_event = None
        self.running = False
        self.history = []
        self.logfile = open('log.txt', 'a', encoding='utf-8')
        self.min_profit = E.DEFAULTS['min_profit']
        self.fees = {}   # symbol -> (buy, sell) taker rate the engine is using
        self.thread = None
        self.closing = False
        self.last_tick = 0.0
        self.interval = E.DEFAULTS['interval']
        self.halt_reason = None

        root.title('SOL Arbitrage')
        root.configure(bg=BG)
        height = max(640, min(860, root.winfo_screenheight() - 80))
        root.geometry(f'1240x{height}')
        root.minsize(1000, 640)
        self.ui = pick_font('Segoe UI', 'SF Pro Text', 'Helvetica Neue', 'DejaVu Sans', 'Arial')
        self.mono = pick_font('Cascadia Mono', 'Consolas', 'Menlo', 'DejaVu Sans Mono', 'Courier New')
        self.style()

        self.build_header()
        self.banner = tk.Label(root, text='', bg='#3d1214', fg='#ffb4ae', font=(self.ui, 10, 'bold'),
                               anchor='w', padx=14, pady=8, wraplength=1150, justify='left')
        self.build_cards()
        body = tk.Frame(root, bg=BG)
        body.pack(fill='both', expand=True, padx=16, pady=(0, 16))
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        left = tk.Frame(body, bg=BG)
        left.grid(row=0, column=0, sticky='ns', padx=(0, 12))
        right = tk.Frame(body, bg=BG)
        right.grid(row=0, column=1, sticky='nsew')
        self.build_settings(left)
        self.build_account(left)
        self.build_routes(right)
        self.build_chart(right)
        self.build_bottom(right)

        root.protocol('WM_DELETE_WINDOW', self.close)
        self.log('Ready. Press Start to watch the market (dry run by default).', 'info')
        root.after(100, self.poll)

    # ------------------------------------------------------------------ styling
    def style(self):
        s = ttk.Style()
        s.theme_use('clam')
        s.configure('.', background=PANEL, foreground=TEXT, font=(self.ui, 10), bordercolor=BORDER,
                    troughcolor=FIELD, focuscolor=PURPLE)
        s.configure('Treeview', background=PANEL, fieldbackground=PANEL, foreground=TEXT, rowheight=26,
                    borderwidth=0, font=(self.ui, 10))
        s.configure('Treeview.Heading', background=FIELD, foreground=MUTED, relief='flat',
                    font=(self.ui, 9, 'bold'), padding=(8, 6))
        s.map('Treeview', background=[('selected', '#262c36')], foreground=[('selected', TEXT)])
        s.map('Treeview.Heading', background=[('active', FIELD)])
        s.layout('Treeview', [('Treeview.treearea', {'sticky': 'nswe'})])
        s.configure('TEntry', fieldbackground=FIELD, foreground=TEXT, insertcolor=TEXT, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, padding=(6, 4))
        s.map('TEntry', bordercolor=[('focus', PURPLE)], lightcolor=[('focus', PURPLE)],
              fieldbackground=[('disabled', PANEL)], foreground=[('disabled', MUTED)])
        s.configure('Start.TButton', background=PURPLE, foreground='white', font=(self.ui, 11, 'bold'),
                    padding=(10, 9), borderwidth=0)
        s.map('Start.TButton', background=[('disabled', '#3a2d55'), ('active', PURPLE_HI)],
              foreground=[('disabled', MUTED)])
        s.configure('Stop.TButton', background='#30363d', foreground=TEXT, font=(self.ui, 11, 'bold'),
                    padding=(10, 9), borderwidth=0)
        s.map('Stop.TButton', background=[('disabled', '#1f242b'), ('active', '#3d444d')],
              foreground=[('disabled', DIM)])
        s.configure('Link.TButton', background=PANEL, foreground=MUTED, borderwidth=0, padding=(0, 2),
                    font=(self.ui, 9, 'underline'))
        s.map('Link.TButton', background=[('active', PANEL)], foreground=[('active', TEXT)])
        s.configure('TRadiobutton', background=PANEL, foreground=TEXT, indicatorbackground=FIELD,
                    indicatorforeground=PURPLE, upperbordercolor=BORDER, lowerbordercolor=BORDER,
                    indicatormargin=(0, 0, 8, 0), font=(self.ui, 10))
        s.map('TRadiobutton', background=[('active', PANEL)],
              indicatorbackground=[('disabled', PANEL), ('pressed', PANEL)],
              indicatorforeground=[('disabled', DIM)], foreground=[('disabled', MUTED)])
        s.configure('TNotebook', background=BG, borderwidth=1, tabmargins=0, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER)
        s.configure('TNotebook.Tab', background=BG, foreground=MUTED, padding=(14, 6), borderwidth=1,
                    font=(self.ui, 10, 'bold'), bordercolor=BORDER, lightcolor=BG, darkcolor=BORDER)
        s.map('TNotebook.Tab', background=[('selected', PANEL)], foreground=[('selected', TEXT)],
              lightcolor=[('selected', PANEL)])
        s.configure('Vertical.TScrollbar', background=BORDER, troughcolor=PANEL, bordercolor=PANEL,
                    arrowcolor=MUTED, lightcolor=BORDER, darkcolor=BORDER, gripcount=0)
        s.map('Vertical.TScrollbar', background=[('active', '#3d444d'), ('!active', BORDER)])

    def card(self, parent, title, **pack):
        outer = tk.Frame(parent, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        outer.pack(**pack)
        if title:
            tk.Label(outer, text=title.upper(), bg=PANEL, fg=MUTED, font=(self.ui, 9, 'bold')).pack(
                anchor='w', padx=14, pady=(12, 6))
        return outer

    def pill(self, parent, text, colour):
        p = tk.Label(parent, text=text, bg=PANEL, fg=colour, font=(self.ui, 9, 'bold'), padx=10, pady=4,
                     highlightthickness=1, highlightbackground=BORDER)
        p.pack(side='right', padx=(8, 0))
        return p

    def set_pill(self, p, text, colour):
        p.configure(text=text, fg=colour)

    # ------------------------------------------------------------------ layout
    def build_header(self):
        h = self.header = tk.Frame(self.root, bg=BG)
        h.pack(fill='x', padx=16, pady=(14, 10))
        logo = tk.Canvas(h, width=34, height=34, bg=BG, highlightthickness=0)
        logo.pack(side='left')
        for i, c in enumerate((GREEN, BLUE, PURPLE)):
            y = 8 + i * 8
            logo.create_polygon(8, y + 5, 12, y, 30, y, 26, y + 5, fill=c, outline='')
        t = tk.Frame(h, bg=BG)
        t.pack(side='left', padx=(8, 0))
        tk.Label(t, text='SOL Arbitrage', bg=BG, fg=TEXT, font=(self.ui, 17, 'bold')).pack(anchor='w')
        tk.Label(t, text='USDT · FDUSD · USDC triangle on Binance spot', bg=BG, fg=MUTED,
                 font=(self.ui, 10)).pack(anchor='w')
        pills = tk.Frame(h, bg=BG)
        pills.pack(side='right')
        self.p_data = self.pill(pills, 'NO DATA', DIM)
        self.p_fees = self.pill(pills, 'FEES: —', DIM)
        self.p_state = self.pill(pills, '○ STOPPED', MUTED)
        self.p_mode = self.pill(pills, 'DRY RUN', AMBER)

    def build_cards(self):
        row = tk.Frame(self.root, bg=BG)
        row.pack(fill='x', padx=16, pady=(0, 12))
        self.cards = {}
        for i, (k, title) in enumerate((('best', 'Best route now'), ('trigger', 'Trigger at'),
                                        ('checks', 'Price checks'), ('trades', 'Trades'),
                                        ('pnl', 'Session P&L'))):
            row.columnconfigure(i, weight=1, uniform='cards')
            c = tk.Frame(row, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
            c.grid(row=0, column=i, sticky='nsew', padx=(0 if i == 0 else 10, 0))
            tk.Label(c, text=title.upper(), bg=PANEL, fg=MUTED, font=(self.ui, 9, 'bold')).pack(
                anchor='w', padx=14, pady=(12, 0))
            v = tk.Label(c, text='—', bg=PANEL, fg=TEXT, font=(self.ui, 20, 'bold'))
            v.pack(anchor='w', padx=14)
            sub = tk.Label(c, text=' ', bg=PANEL, fg=MUTED, font=(self.ui, 9))
            sub.pack(anchor='w', padx=14, pady=(0, 12))
            self.cards[k] = (v, sub)
        self.set_card('trigger', f'+{self.min_profit:.2f}%', 'worst-case net profit')
        self.set_card('trades', '0', 'none yet')
        self.set_card('pnl', '$0.00', 'paper trading')

    def set_card(self, k, value, sub=None, colour=TEXT):
        v, s = self.cards[k]
        v.configure(text=value, fg=colour)
        if sub is not None:
            s.configure(text=sub)

    def build_settings(self, parent):
        c = self.card(parent, 'Settings', fill='x')
        grid = tk.Frame(c, bg=PANEL)
        grid.pack(fill='x', padx=14)
        self.vars, self.entries = {}, []
        for i, (k, label, hint) in enumerate(FIELDS):
            lbl = tk.Label(grid, text=label + '  ⓘ', bg=PANEL, fg=TEXT, font=(self.ui, 10))
            lbl.grid(row=i, column=0, sticky='w')
            v = tk.StringVar(value=str(getattr(key, k, E.DEFAULTS[k])))
            e = ttk.Entry(grid, textvariable=v, width=10, justify='right', font=(self.mono, 10))
            e.grid(row=i, column=1, sticky='e', padx=(12, 0), pady=3)
            Tooltip(lbl, hint, (self.ui, 9))
            Tooltip(e, hint, (self.ui, 9))
            self.vars[k] = v
            self.entries.append(e)
        grid.columnconfigure(0, weight=1)

        tk.Frame(c, bg=BORDER, height=1).pack(fill='x', padx=14, pady=10)
        self.dry_run = tk.BooleanVar(value=bool(getattr(key, 'dry_run', True)))
        self.radios = [ttk.Radiobutton(c, text='Dry run  (paper trades only)', variable=self.dry_run, value=True,
                                       command=self.mode_changed),
                       ttk.Radiobutton(c, text='Live trading  (real orders)', variable=self.dry_run, value=False,
                                       command=self.mode_changed)]
        for r in self.radios:
            r.pack(anchor='w', padx=14, pady=1)

        btns = tk.Frame(c, bg=PANEL)
        btns.pack(fill='x', padx=14, pady=(12, 4))
        btns.columnconfigure(0, weight=1)
        btns.columnconfigure(1, weight=1)
        self.start_btn = ttk.Button(btns, text='▶  Start', style='Start.TButton', command=self.start)
        self.start_btn.grid(row=0, column=0, sticky='ew', padx=(0, 4))
        self.stop_btn = ttk.Button(btns, text='■  Stop', style='Stop.TButton', command=self.stop, state='disabled')
        self.stop_btn.grid(row=0, column=1, sticky='ew', padx=(4, 0))
        self.reset_btn = ttk.Button(c, text='Reset settings to defaults', style='Link.TButton', command=self.reset)
        self.reset_btn.pack(anchor='w', padx=14, pady=(2, 12))
        self.mode_changed()

    def build_account(self, parent):
        c = self.card(parent, 'Account', fill='x', pady=(12, 0))
        self.acct_status = tk.Label(c, text='Not connected', bg=PANEL, fg=MUTED, font=(self.ui, 9),
                                    wraplength=250, justify='left')
        self.acct_status.pack(anchor='w', padx=14)
        grid = tk.Frame(c, bg=PANEL)
        grid.pack(fill='x', padx=14, pady=(8, 12))
        grid.columnconfigure(1, weight=1)
        grid.columnconfigure(3, weight=1)
        self.bal = {}
        for i, a in enumerate(E.ASSETS):   # two columns to save height
            row, col = i // 2, (i % 2) * 2
            tk.Label(grid, text=a, bg=PANEL, fg=MUTED, font=(self.ui, 9)).grid(
                row=row, column=col, sticky='w', padx=(0 if col == 0 else 14, 6))
            v = tk.Label(grid, text='—', bg=PANEL, fg=TEXT, font=(self.mono, 9))
            v.grid(row=row, column=col + 1, sticky='e')
            self.bal[a] = v

    def table(self, parent, cols, height):
        frame = tk.Frame(parent, bg=PANEL)
        frame.pack(fill='both', expand=True, padx=1, pady=(0, 1))
        t = ttk.Treeview(frame, columns=[c[0] for c in cols], show='headings', height=height, selectmode='none')
        for cid, label, width, anchor in cols:
            t.heading(cid, text=label, anchor=anchor)
            t.column(cid, width=width, anchor=anchor, stretch=cid in ('route', 'pair'))
        t.pack(fill='both', expand=True)
        for tag, colour in (('go', GREEN), ('near', AMBER), ('far', TEXT), ('na', DIM), ('bad', RED),
                            ('muted', MUTED)):
            t.tag_configure(tag, foreground=colour)
        return t

    def build_routes(self, parent):
        c = self.card(parent, 'Routes  ·  net profit after fees and spread, at current prices', fill='x')
        self.routes = self.table(c, [('route', 'Route', 230, 'w'), ('net', 'Net', 90, 'e'),
                                     ('fees', 'Fees', 80, 'e'), ('meter', 'Distance to trigger', 250, 'w'),
                                     ('note', 'Note', 140, 'w')], 6)

    def build_chart(self, parent):
        c = self.card(parent, 'Best route over time', fill='x', pady=(12, 0))
        self.chart = tk.Canvas(c, height=130, bg=PANEL, highlightthickness=0)
        self.chart.pack(fill='x', padx=14, pady=(0, 12))
        self.chart.bind('<Configure>', lambda e: self.draw_chart())

    def build_bottom(self, parent):
        nb = ttk.Notebook(parent)
        nb.pack(fill='both', expand=True, pady=(12, 0))

        prices = tk.Frame(nb, bg=PANEL)
        self.prices = self.table(prices, [('pair', 'Pair', 120, 'w'), ('bid', 'Bid', 110, 'e'),
                                          ('ask', 'Ask', 110, 'e'), ('spread', 'Spread', 90, 'e'),
                                          ('depth', 'Top depth', 110, 'e'), ('fee', 'Your fee', 90, 'e')], 6)

        log = tk.Frame(nb, bg=PANEL)
        self.log_box = tk.Text(log, bg=PANEL, fg=TEXT, font=(self.mono, 9), relief='flat', wrap='word',
                               padx=12, pady=8, height=8, state='disabled', insertbackground=TEXT,
                               highlightthickness=0)
        sb = ttk.Scrollbar(log, command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        self.log_box.pack(fill='both', expand=True)
        for tag, colour in (('time', DIM), ('info', MUTED), ('warn', AMBER), ('error', RED), ('trade', GREEN)):
            self.log_box.tag_configure(tag, foreground=colour)

        trades = tk.Frame(nb, bg=PANEL)
        self.trades = self.table(trades, [('time', 'Time', 80, 'w'), ('route', 'Route', 230, 'w'),
                                          ('mode', 'Mode', 70, 'w'), ('planned', 'Planned', 90, 'e'),
                                          ('outcome', 'Outcome', 120, 'w'), ('pnl', 'P&L', 110, 'e')], 6)

        nb.add(log, text='Activity')
        nb.add(prices, text='Prices')
        nb.add(trades, text='Trades')
        self.trades_tab, self.nb = trades, nb

    # ------------------------------------------------------------------ actions
    def mode_changed(self):
        if self.dry_run.get():
            self.set_pill(self.p_mode, 'DRY RUN', AMBER)
        else:
            self.set_pill(self.p_mode, '● LIVE', RED)

    def reset(self):
        for k, v in self.vars.items():
            v.set(str(E.DEFAULTS[k]))
        self.log('Settings reset to defaults.', 'info')

    def read_settings(self):
        s = {}
        for k, label, _ in FIELDS:
            try:
                s[k] = float(self.vars[k].get())
            except ValueError:
                raise ValueError(f'"{label}" must be a number')
            if not math.isfinite(s[k]) or s[k] < 0:
                raise ValueError(f'"{label}" must be a positive number')
        if s['size'] < 20:
            raise ValueError('Trade size should be at least 20: Binance needs 5 per order and the '
                             'stablecoin pair only trades whole units')
        if s['interval'] < 0.5:
            raise ValueError('Check every should be at least 0.5 s (Binance rate limits)')
        if s['min_profit'] < 0.05:
            raise ValueError('Min profit below 0.05% leaves no room for price movement between orders')
        if s['max_trades'] < 1 or s['max_trades'] != int(s['max_trades']):
            raise ValueError('"Max trades" must be a whole number of at least 1')
        if s['max_loss'] <= 0:
            raise ValueError('"Stop at loss" must be more than 0')
        s['max_trades'] = int(s['max_trades'])
        s['dry_run'] = self.dry_run.get()
        return s

    def start(self):
        try:
            s = self.read_settings()
        except ValueError as e:
            messagebox.showerror('Settings', str(e))
            return
        if not s['dry_run'] and self.halt_reason and not messagebox.askyesno(
                'Last session was halted',
                f'The last session stopped because:\n\n{self.halt_reason}\n\n'
                'Have you checked your Binance balances and open orders?', icon='warning'):
            return
        if not s['dry_run'] and not messagebox.askyesno(
                'Start live trading?',
                'This places REAL orders on Binance with your API keys.\n\n'
                f'• Up to {s["size"]:g} per trade, at most {s["max_trades"]} trades\n'
                f'• Stops after a session loss of ${s["max_loss"]:g}\n'
                '• Each trade is 3 orders; prices can move between them\n\n'
                'Start live trading?', icon='warning'):
            return
        self.min_profit = s['min_profit']
        self.set_card('trigger', f'+{s["min_profit"]:.2f}%', 'worst-case net profit')
        self.set_card('pnl', '$0.00', 'paper trading' if s['dry_run'] else 'live trading')
        self.set_card('trades', '0', 'none yet')
        self.history = []
        self.interval = s['interval']
        self.halt_reason = None
        self.banner.pack_forget()
        self.stop_event = threading.Event()
        engine = E.Engine(s, lambda k, d: self.events.put((k, d)), self.stop_event,
                          api_key=getattr(key, 'api_key', None), api_secret=getattr(key, 'api_secret', None),
                          market_data_url=getattr(key, 'market_data_url', None))
        # not a daemon: closing the window lets a trade in progress finish its legs
        self.thread = threading.Thread(target=engine.run, daemon=False)
        self.thread.start()
        self.running = True
        self.set_running(True)
        self.log(f'Started in {"DRY RUN" if s["dry_run"] else "LIVE"} mode: size {s["size"]:g}, '
                 f'trigger at {s["min_profit"]:g}% worst-case profit, every {s["interval"]:g}s.', 'info')

    def stop(self):
        if self.stop_event:
            self.stop_event.set()
        self.stop_btn.configure(state='disabled')
        self.set_pill(self.p_state, '… STOPPING', MUTED)

    def set_running(self, on):
        self.start_btn.configure(state='disabled' if on else 'normal')
        self.stop_btn.configure(state='normal' if on else 'disabled')
        for w in self.entries + self.radios + [self.reset_btn]:
            w.configure(state='disabled' if on else 'normal')
        self.set_pill(self.p_state, '● RUNNING' if on else '○ STOPPED', GREEN if on else MUTED)

    def close(self):
        if self.running and not self.dry_run.get() and not messagebox.askyesno(
                'Quit?', 'Live trading is running. A trade in progress will finish first. Quit?'):
            return
        if self.stop_event:
            self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.closing = time.monotonic()
            self.root.title('SOL Arbitrage (stopping…)')
            self.log('Closing: waiting for the engine to finish…', 'info')
        else:
            self.root.destroy()

    def log(self, text, level='info'):
        stamp = time.strftime('%H:%M:%S')
        self.log_box.configure(state='normal')
        self.log_box.insert('end', stamp + '  ', 'time')
        self.log_box.insert('end', text + '\n', level)
        if int(self.log_box.index('end-1c').split('.')[0]) > 2000:
            self.log_box.delete('1.0', '500.0')
        self.log_box.see('end')
        self.log_box.configure(state='disabled')
        try:
            self.logfile.write(f'{time.strftime("%Y-%m-%d")} {stamp} [{level}] {text}\n')
            self.logfile.flush()
        except OSError:
            pass

    # ------------------------------------------------------------------ engine events
    def poll(self):
        try:
            for _ in range(200):
                kind, data = self.events.get_nowait()
                try:
                    getattr(self, 'on_' + kind)(data)
                except Exception as e:   # a display bug must never freeze the window
                    print(f'UI error handling {kind}: {e!r}', file=sys.stderr)
        except queue.Empty:
            pass
        if self.closing and (not self.thread.is_alive() or time.monotonic() - self.closing > 30):
            self.root.destroy()
            return
        if self.running and self.last_tick and time.monotonic() - self.last_tick > max(5, 4 * self.interval):
            age = time.monotonic() - self.last_tick
            self.set_pill(self.p_data, f'NO DATA {age:.0f}s', RED)
            self.set_card('best', '—', 'no live prices', DIM)
        self.root.after(100, self.poll)

    def on_log(self, data):
        self.log(data[1], data[0])

    def on_status(self, st):
        host = 'data-api.binance.vision' if 'vision' in st['market'] else 'api.binance.com'
        self.set_pill(self.p_data, host, BLUE)
        self.acct_status.configure(text=f'Trading: {st["trading"]}', fg=GREEN if st['trading'] == 'ready' else AMBER)
        self.set_fees_pill(st['fees'])

    def set_fees_pill(self, source):
        if source == 'account':
            self.set_pill(self.p_fees, 'FEES: YOUR ACCOUNT', GREEN)
        else:
            self.set_pill(self.p_fees, f'FEES: FALLBACK {self.vars["fallback_fee"].get()}%', AMBER)

    def on_fees(self, data):
        self.fees = data['rates']
        self.set_fees_pill(data['source'])

    def on_balances(self, bal):
        for a, w in self.bal.items():
            v = bal.get(a)
            w.configure(text='—' if v is None else f'{v:,.4f}' if a in ('SOL', 'BNB') else f'{v:,.2f}')
            w.configure(fg=TEXT if v else DIM)

    def on_tick(self, t):
        self.last_tick = time.monotonic()
        st = t['stats']
        lat = t['latency']
        self.set_pill(self.p_data, f'{time.strftime("%H:%M:%S")} · {lat:.0f} ms', GREEN if lat < 500 else AMBER)

        # routes
        self.routes.delete(*self.routes.get_children())
        for r in sorted(t['routes'], key=lambda r: -(r['pct'] if r['pct'] is not None else -99)):
            if r['pct'] is None:
                self.routes.insert('', 'end', values=(r['name'], '—', f'{r["fees_pct"]:.3f}%', '', r['why'] or ''),
                                   tags=('na',))
                continue
            gap = self.min_profit - r['pct']
            tag = 'go' if gap <= 0 else 'near' if gap < 0.1 else 'far'
            self.routes.insert('', 'end', values=(
                r['name'], f'{r["pct"]:+.3f}%', f'{r["fees_pct"]:.3f}%', self.meter(r['pct']),
                'thin top of book' if r['thin'] else ''), tags=(tag,))

        # prices
        self.prices.delete(*self.prices.get_children())
        fees = self.fees
        for s, b in t['books'].items():
            if b is None:
                self.prices.insert('', 'end', values=(s, 'no book', '', '', '', ''), tags=('bad',))
                continue
            (bid, bq), (ask, aq) = b['bids'][0], b['asks'][0]
            spread = (ask - bid) / ask * 10000
            depth = min(bid * bq, ask * aq)
            fee = fees.get(s)
            places = 2 if bid > 10 else 5
            self.prices.insert('', 'end', values=(
                s, f'{bid:.{places}f}', f'{ask:.{places}f}', f'{spread:.2f} bps', f'${depth:,.0f}',
                f'{fee[0] * 100:.3f}%' if fee else f'{float(self.vars["fallback_fee"].get()):.3f}%'),
                tags=('far',))

        # cards
        best = max((r for r in t['routes'] if r['pct'] is not None), key=lambda r: r['pct'], default=None)
        if best:
            gap = self.min_profit - best['pct']
            self.set_card('best', f'{best["pct"]:+.3f}%', best['name'],
                          GREEN if gap <= 0 else AMBER if gap < 0.1 else TEXT)
            self.set_card('trigger', f'+{self.min_profit:.2f}%',
                          'triggering now' if gap <= 0 else f'best route is {gap:.3f}% away')
            self.history.append(best['pct'])
            del self.history[:-HISTORY]
            self.draw_chart()
        cd = t['cooldown']
        self.set_card('checks', f'{st["checks"]:,}',
                      f'cooling down {math.ceil(cd)}s' if cd > 0.05 else f'{st["candidates"]} re-checked on depth')
        self.update_stats(st)

    def update_stats(self, st):
        self.set_card('trades', str(st['trades']),
                      f'{st["wins"]} profitable · {st["losses"]} losing' if st['trades'] else 'none yet')
        pnl = st['pnl']
        self.set_card('pnl', f'{"-" if pnl < 0 else ""}${abs(pnl):,.4f}',
                      'paper trading' if self.dry_run.get() else 'live trading',
                      GREEN if pnl > 0 else RED if pnl < 0 else TEXT)

    def meter(self, pct, low=-0.5):
        """Text bar: empty at `low`% net, full at the trigger."""
        frac = (pct - low) / (self.min_profit - low)
        frac = max(0.0, min(1.0, frac))
        n = round(frac * 10)
        gap = self.min_profit - pct
        return '▰' * n + '▱' * (10 - n) + ('   GO' if gap <= 0 else f'   {gap:.3f}% to go')

    def on_trade(self, tr):
        pnl = tr['pnl']
        tag = 'bad' if pnl is None or pnl < 0 else 'go' if pnl > 0 else 'muted'
        self.trades.insert('', 0, values=(tr['time'], tr['route'], 'LIVE' if tr['live'] else 'paper',
                                          f'{tr["planned"]:+.3f}%', tr['outcome'],
                                          'unknown' if pnl is None else f'{pnl:+.4f} {tr["asset"]}'), tags=(tag,))
        self.update_stats(tr['stats'])
        self.nb.select(self.trades_tab)
        self.root.bell()

    def on_halt(self, reason):
        self.set_pill(self.p_state, '■ HALTED', RED)

    def on_stopped(self, reason):
        self.running = False
        self.set_running(False)
        self.set_pill(self.p_data, 'NO DATA', DIM)
        if reason:
            if not self.dry_run.get():
                self.halt_reason = reason
            self.set_pill(self.p_state, '■ HALTED', RED)
            self.banner.configure(text='■ Stopped: ' + reason)
            self.banner.pack(after=self.header, fill='x', padx=16, pady=(0, 10))
            if not self.dry_run.get() and not self.closing:
                messagebox.showwarning('Trading stopped', reason)
        self.log('Stopped.', 'info')

    # ------------------------------------------------------------------ chart
    def draw_chart(self):
        c = self.chart
        c.delete('all')
        w, h = max(c.winfo_width(), 200), int(c['height'])
        left, right, top, bottom = 56, 10, 8, 20
        data = self.history
        lo = min([*data, -0.4, 0]) if data else -0.4
        hi = max([*data, self.min_profit + 0.05]) if data else self.min_profit + 0.05
        span = hi - lo or 1

        def y(v):
            return top + (hi - v) / span * (h - top - bottom)

        for v, colour, label, dash in ((self.min_profit, GREEN, f'+{self.min_profit:.2f}% trigger', (4, 3)),
                                       (0, DIM, '0%', (2, 4))):
            c.create_line(left, y(v), w - right, y(v), fill=colour, dash=dash)
            c.create_text(left - 6, y(v), text=label.split()[0], fill=colour, anchor='e', font=(self.mono, 8))
        c.create_text(left - 6, y(lo), text=f'{lo:+.2f}%', fill=DIM, anchor='e', font=(self.mono, 8))
        if hi > self.min_profit + 0.1:
            c.create_text(left - 6, y(hi), text=f'{hi:+.2f}%', fill=DIM, anchor='e', font=(self.mono, 8))
        c.create_text(w - right, h - 4, text='now', fill=DIM, anchor='se', font=(self.ui, 8))
        c.create_text(left, h - 4, text=f'last {len(data)} checks', fill=DIM, anchor='sw', font=(self.ui, 8))
        if len(data) < 2:
            c.create_text((left + w - right) / 2, h / 2, text='Waiting for prices…', fill=DIM, font=(self.ui, 10))
            return
        step = (w - left - right) / (HISTORY - 1)
        x0 = w - right - (len(data) - 1) * step
        pts = []
        for i, v in enumerate(data):
            pts += [x0 + i * step, y(v)]
        c.create_line(*pts, fill=PURPLE_HI, width=2, smooth=False)
        c.create_oval(pts[-2] - 3, pts[-1] - 3, pts[-2] + 3, pts[-1] + 3, fill=PURPLE_HI, outline='')


if __name__ == '__main__':
    root = tk.Tk()
    App(root)
    root.mainloop()

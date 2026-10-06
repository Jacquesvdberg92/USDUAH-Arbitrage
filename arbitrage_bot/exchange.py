"""Binance REST client plus live and paper (simulated) exchanges.

Both exchanges expose the same interface, so the bot and executor cannot tell
whether they are trading real money or a simulation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlencode

import requests

from .market import (
    BUY,
    SELL,
    ZERO,
    D,
    OrderBook,
    SymbolRules,
    Ticker,
    fill_buy,
    fill_sell,
    fmt,
    max_buy_qty,
)

log = logging.getLogger(__name__)

LIVE_URL = "https://api.binance.com"
TESTNET_URL = "https://testnet.binance.vision"
# Public market-data-only mirror: no keys, and reachable from more places.
PUBLIC_DATA_URL = "https://data-api.binance.vision"


class BinanceAPIError(Exception):
    def __init__(self, status: int, code: int, message: str, retry_after: Optional[float] = None):
        super().__init__(f"HTTP {status} / code {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after


class OrderStatusUnknown(Exception):
    """An order may or may not have executed, and Binance couldn't tell us which.

    Never retry or unwind on this: a human has to look at the account.
    """

    def __init__(self, params: Mapping[str, str], cause: Exception):
        super().__init__(
            f"{params.get('side')} {params.get('symbol')} order {params.get('newClientOrderId')} may have executed "
            f"({cause}) - check your Binance account"
        )
        self.params = dict(params)


# Binance: HTTP 5xx and these codes mean "execution status unknown" - the order may have filled.
UNKNOWN_OUTCOME_CODES = (-1006, -1007)
FINAL_ORDER_STATUSES = ("FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED")


def is_definite_rejection(err: Exception) -> bool:
    """True when we know for certain that a failed order did NOT execute."""
    if isinstance(err, BinanceAPIError):
        return err.status < 500 and err.code not in UNKNOWN_OUTCOME_CODES
    # Couldn't even connect, so the order never left this machine.
    return isinstance(err, requests.ConnectTimeout)


def sign(secret: str, query: str) -> str:
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class OrderResult:
    symbol: str
    side: str
    order_type: str
    status: str
    executed_qty: Decimal  # base asset traded
    quote_qty: Decimal  # quote asset traded
    commissions: Dict[str, Decimal] = field(default_factory=dict)
    raw: dict = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_api(cls, raw: dict) -> "OrderResult":
        commissions: Dict[str, Decimal] = {}
        for f in raw.get("fills", []):
            asset = f["commissionAsset"]
            commissions[asset] = commissions.get(asset, ZERO) + D(f["commission"])
        return cls(
            symbol=raw["symbol"],
            side=raw["side"],
            order_type=raw["type"],
            status=raw["status"],
            executed_qty=D(raw["executedQty"]),
            quote_qty=D(raw["cummulativeQuoteQty"]),
            commissions=commissions,
            raw=raw,
        )

    def flows(self, rules: SymbolRules):
        """(spent, received, fees paid in other assets e.g. BNB) for this order."""
        if self.side == BUY:
            spent, got, got_asset = self.quote_qty, self.executed_qty, rules.base
        else:
            spent, got, got_asset = self.executed_qty, self.quote_qty, rules.quote
        received = got - self.commissions.get(got_asset, ZERO)
        other = {a: v for a, v in self.commissions.items() if a != got_asset and v}
        return spent, received, other


class BinanceClient:
    """Minimal Binance spot REST client (only what the bot needs)."""

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        timeout: float = 10.0,
        recv_window: int = 5000,
        session: Optional[requests.Session] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_secret = api_secret
        self.timeout = timeout
        self.recv_window = recv_window
        self.session = session or requests.Session()
        if api_key:
            self.session.headers["X-MBX-APIKEY"] = api_key
        self.time_offset_ms = 0

    # -- plumbing -------------------------------------------------------
    def _request(self, method: str, path: str, params: Optional[dict] = None, signed: bool = False):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if signed:
            if not self.api_secret:
                raise BinanceAPIError(0, 0, "this call needs an API key and secret")
            params["recvWindow"] = self.recv_window
            params["timestamp"] = int(time.time() * 1000) + self.time_offset_ms
        query = urlencode(params)
        if signed:
            query += ("&" if query else "") + "signature=" + sign(self.api_secret, query)
        url = self.base_url + path + ("?" + query if query else "")
        response = self.session.request(method, url, timeout=self.timeout)
        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = {}
            retry = response.headers.get("Retry-After")
            raise BinanceAPIError(
                response.status_code,
                int(body.get("code", 0)),
                body.get("msg", response.text[:200]),
                float(retry) if retry else None,
            )
        return response.json()

    # -- public ---------------------------------------------------------
    def sync_time(self) -> None:
        before = time.time() * 1000
        server = self._request("GET", "/api/v3/time")["serverTime"]
        after = time.time() * 1000
        self.time_offset_ms = int(server - (before + after) / 2)

    def exchange_info(self) -> List[SymbolRules]:
        data = self._request("GET", "/api/v3/exchangeInfo")
        return [SymbolRules.from_exchange_info(s) for s in data["symbols"]]

    def book_tickers(self, symbols: Iterable[str]) -> Dict[str, Ticker]:
        symbols = sorted(set(symbols))
        data = self._request(
            "GET", "/api/v3/ticker/bookTicker", {"symbols": json.dumps(symbols, separators=(",", ":"))}
        )
        return {row["symbol"]: Ticker.from_api(row) for row in data}

    def order_book(self, symbol: str, limit: int = 20) -> OrderBook:
        return OrderBook.from_api(symbol, self._request("GET", "/api/v3/depth", {"symbol": symbol, "limit": limit}))

    # -- signed ---------------------------------------------------------
    def balances(self) -> Dict[str, Decimal]:
        data = self._request("GET", "/api/v3/account", {"omitZeroBalances": "true"}, signed=True)
        return {b["asset"]: D(b["free"]) for b in data["balances"]}

    def taker_commission(self, symbol: str) -> Decimal:
        """Effective taker fee rate for this account (ignores the BNB discount)."""
        data = self._request("GET", "/api/v3/account/commission", {"symbol": symbol}, signed=True)
        return sum(
            (D(data.get(kind, {}).get("taker", "0")) for kind in ("standardCommission", "taxCommission", "specialCommission")),
            ZERO,
        )

    def new_order(self, params: Mapping[str, str]) -> OrderResult:
        params = dict(params, newOrderRespType="FULL")
        return OrderResult.from_api(self._request("POST", "/api/v3/order", params, signed=True))

    def get_order(self, symbol: str, client_order_id: str) -> dict:
        return self._request("GET", "/api/v3/order", {"symbol": symbol, "origClientOrderId": client_order_id}, signed=True)

    def my_trades(self, symbol: str, order_id: int) -> List[dict]:
        return self._request("GET", "/api/v3/myTrades", {"symbol": symbol, "orderId": order_id}, signed=True)


class Exchange:
    """Shared interface used by the bot; subclasses implement ``_place``."""

    name = "exchange"

    def __init__(self, market: BinanceClient, rules: Mapping[str, SymbolRules], depth_limit: int = 20):
        self.market = market
        self.rules = rules
        self.depth_limit = depth_limit
        self._pool = ThreadPoolExecutor(max_workers=3)

    def book_tickers(self, symbols: Iterable[str]) -> Dict[str, Ticker]:
        return self.market.book_tickers(symbols)

    def order_books(self, symbols: Iterable[str]) -> Dict[str, OrderBook]:
        """Fetch the books concurrently - latency matters more than anything here."""
        symbols = list(dict.fromkeys(symbols))
        books = self._pool.map(lambda s: self.market.order_book(s, self.depth_limit), symbols)
        return dict(zip(symbols, books))

    def balances(self) -> Dict[str, Decimal]:
        raise NotImplementedError

    def limit_ioc(self, symbol: str, side: str, qty: Decimal, price: Decimal) -> OrderResult:
        return self._place(
            {"symbol": symbol, "side": side, "type": "LIMIT", "timeInForce": "IOC", "quantity": fmt(qty), "price": fmt(price)}
        )

    def market_sell(self, symbol: str, qty: Decimal) -> OrderResult:
        return self._place({"symbol": symbol, "side": SELL, "type": "MARKET", "quantity": fmt(qty)})

    def market_buy_quote(self, symbol: str, quote_amount: Decimal) -> OrderResult:
        return self._place({"symbol": symbol, "side": BUY, "type": "MARKET", "quoteOrderQty": fmt(quote_amount)})

    def _place(self, params: Dict[str, str]) -> OrderResult:
        raise NotImplementedError


class LiveExchange(Exchange):
    """Real orders (Binance live or Spot Testnet, depending on the client's URL).

    Every order carries our own client order id. If the request fails in a way
    that doesn't prove the order was rejected (timeout, HTTP 5xx, -1006/-1007),
    we look the order up by that id and use its real fills. Only if Binance
    still can't tell us do we raise OrderStatusUnknown, which stops the bot.
    """

    name = "live"

    def __init__(
        self,
        market: BinanceClient,
        rules: Mapping[str, SymbolRules],
        depth_limit: int = 20,
        reconcile_delays: Sequence[float] = (0.5, 1, 2, 4, 8),
        sleep: Callable[[float], None] = time.sleep,
    ):
        super().__init__(market, rules, depth_limit)
        self.reconcile_delays = tuple(reconcile_delays)
        self.sleep = sleep

    def balances(self) -> Dict[str, Decimal]:
        return self.market.balances()

    def _place(self, params: Dict[str, str]) -> OrderResult:
        params = dict(params, newClientOrderId="arb-" + uuid.uuid4().hex)  # 36 chars, Binance's maximum
        log.debug("placing %s", params)
        try:
            result = self.market.new_order(params)
        except (BinanceAPIError, requests.RequestException) as err:
            if is_definite_rejection(err):
                raise
            log.warning("order %s: outcome unknown (%s) - asking Binance what happened", params["newClientOrderId"], err)
            result = self._reconcile(params, err)
        log.info(
            "    %s %s %s (order %s / %s): %s, filled %s for %s",
            params["type"], params["side"], params["symbol"], result.raw.get("orderId"), params["newClientOrderId"],
            result.status, result.executed_qty, result.quote_qty,
        )
        return result

    def _reconcile(self, params: Dict[str, str], cause: Exception) -> OrderResult:
        not_found = 0
        last: Exception = cause
        for delay in self.reconcile_delays:
            self.sleep(delay)
            try:
                order = self.market.get_order(params["symbol"], params["newClientOrderId"])
                if order["status"] not in FINAL_ORDER_STATUSES:
                    continue  # IOC and market orders finish almost instantly; ask again
                trades = self.market.my_trades(params["symbol"], order["orderId"]) if D(order["executedQty"]) > 0 else []
            except BinanceAPIError as err:
                last = err
                not_found += err.code == -2013  # "Order does not exist"
                continue
            except requests.RequestException as err:
                last = err
                continue
            if sum((D(t["qty"]) for t in trades), ZERO) != D(order["executedQty"]):
                continue  # trade list not complete yet
            order["fills"] = [
                {"price": t["price"], "qty": t["qty"], "commission": t["commission"], "commissionAsset": t["commissionAsset"]}
                for t in trades
            ]
            log.warning("order %s found: %s, executed %s", params["newClientOrderId"], order["status"], order["executedQty"])
            return OrderResult.from_api(order)
        if not_found == len(self.reconcile_delays):
            # Binance consistently has no record of it, so it was never placed.
            raise BinanceAPIError(400, -2013, f"order never reached Binance ({cause})")
        raise OrderStatusUnknown(params, last)


class PaperExchange(Exchange):
    """Simulated fills against the *live* order book with simulated balances.

    Each order re-fetches the book, so the price moves that happen while our
    three legs are in flight show up in paper results too. It also enforces
    Binance's filters and balance checks, using the exact strings that would be
    sent.

    Liquidity we "took" is remembered and subtracted from the public book (for
    ``depletion_ttl`` seconds, or until that price level disappears), so the
    simulation can't trade the same resting order twice. It cannot model being
    beaten to the liquidity by faster traders, so real results will be worse
    than paper results.
    """

    name = "paper"

    def __init__(
        self,
        market: BinanceClient,
        rules: Mapping[str, SymbolRules],
        fees: Mapping[str, Decimal],
        balances: Mapping[str, Decimal],
        depth_limit: int = 20,
        depletion_ttl: float = 60.0,
        now: Callable[[], float] = time.monotonic,
    ):
        super().__init__(market, rules, depth_limit)
        self.fees = fees
        self.depletion_ttl = depletion_ttl
        self._now = now
        self._balances: Dict[str, Decimal] = {a: D(v) for a, v in balances.items()}
        self._lock = threading.Lock()
        self._order_id = 0
        # (symbol, "bids"|"asks") -> {price: (quantity we took, when)}
        self._taken: Dict[Tuple[str, str], Dict[Decimal, Tuple[Decimal, float]]] = {}

    def balances(self) -> Dict[str, Decimal]:
        with self._lock:
            return dict(self._balances)

    # -- liquidity ledger ----------------------------------------------------
    def _expire(self, symbol: str) -> bool:
        """Drop old records for ``symbol``; True if any are still active."""
        active = False
        for side in ("bids", "asks"):
            taken = self._taken.get((symbol, side), {})
            for price in [p for p, (_, at) in taken.items() if self._now() - at > self.depletion_ttl]:
                del taken[price]
            active = active or bool(taken)
        return active

    def _deplete(self, book: OrderBook) -> OrderBook:
        """The public book minus what our simulated orders already took."""
        with self._lock:
            self._expire(book.symbol)
            sides = {}
            for side in ("bids", "asks"):
                levels = getattr(book, side)
                taken = self._taken.get((book.symbol, side))
                if not taken:
                    sides[side] = levels
                    continue
                prices = {p for p, _ in levels}
                for price in [p for p in taken if p not in prices]:
                    del taken[price]  # level gone from the public book: whatever is there later is new
                remaining_levels = []
                for price, size in levels:
                    left = size - taken.get(price, (ZERO, 0))[0]
                    if left > 0:
                        remaining_levels.append((price, left))
                sides[side] = tuple(remaining_levels)
            return OrderBook(book.symbol, sides["bids"], sides["asks"])

    def _take(self, symbol: str, side: str, levels: Sequence[Tuple[Decimal, Decimal]], qty: Decimal) -> None:
        with self._lock:
            taken = self._taken.setdefault((symbol, side), {})
            remaining = qty
            for price, size in levels:
                if remaining <= 0:
                    break
                used = min(size, remaining)
                taken[price] = (taken.get(price, (ZERO, 0))[0] + used, self._now())
                remaining -= used

    def order_books(self, symbols: Iterable[str]) -> Dict[str, OrderBook]:
        return {s: self._deplete(b) for s, b in super().order_books(symbols).items()}

    def book_tickers(self, symbols: Iterable[str]) -> Dict[str, Ticker]:
        tickers = super().book_tickers(symbols)
        with self._lock:
            touched = [s for s in tickers if self._expire(s)]
        for symbol in touched:  # top of book may be liquidity we already took
            ticker = self._deplete(self.market.order_book(symbol, self.depth_limit)).ticker()
            if ticker is not None:
                tickers[symbol] = ticker
        return tickers

    def _reject(self, code: int, msg: str):
        raise BinanceAPIError(400, code, msg)

    def _place(self, params: Dict[str, str]) -> OrderResult:
        rules = self.rules[params["symbol"]]
        side, order_type = params["side"], params["type"]
        book = self._deplete(self.market.order_book(rules.symbol, self.depth_limit))
        bal = self._balances

        if order_type == "LIMIT":
            qty, price = D(params["quantity"]), D(params["price"])
            problem = rules.order_error(qty, price)
            if problem:
                self._reject(-1013, f"Filter failure: {problem}")
            need = qty * price if side == BUY else qty
            have_asset = rules.quote if side == BUY else rules.base
            if bal.get(have_asset, ZERO) < need:
                self._reject(-2010, "Account has insufficient balance for requested action.")
            fill = fill_buy(book.asks, qty, price) if side == BUY else fill_sell(book.bids, qty, price)
        elif side == SELL:
            qty = D(params["quantity"])
            ref = book.best_bid or ZERO
            problem = rules.order_error(qty, ref, market=True)
            if problem:
                self._reject(-1013, f"Filter failure: {problem}")
            if bal.get(rules.base, ZERO) < qty:
                self._reject(-2010, "Account has insufficient balance for requested action.")
            fill = fill_sell(book.bids, qty)
        else:
            quote = D(params["quoteOrderQty"])
            if bal.get(rules.quote, ZERO) < quote:
                self._reject(-2010, "Account has insufficient balance for requested action.")
            qty, _ = max_buy_qty(book.asks, quote, rules.step_size)
            problem = rules.order_error(qty, book.best_ask or ZERO, market=True)
            if problem:
                self._reject(-1013, f"Filter failure: {problem}")
            fill = fill_buy(book.asks, qty)

        fee_rate = self.fees[rules.symbol]
        if side == BUY:
            got_asset, got, paid_asset, paid = rules.base, fill.base_qty, rules.quote, fill.quote_qty
        else:
            got_asset, got, paid_asset, paid = rules.quote, fill.quote_qty, rules.base, fill.base_qty
        commission = got * fee_rate
        if fill.base_qty > 0:
            self._take(rules.symbol, "asks" if side == BUY else "bids", book.asks if side == BUY else book.bids, fill.base_qty)
        with self._lock:
            if fill.base_qty > 0:
                bal[paid_asset] = bal.get(paid_asset, ZERO) - paid
                bal[got_asset] = bal.get(got_asset, ZERO) + got - commission
            self._order_id += 1
            order_id = self._order_id

        if "quantity" in params:
            complete = fill.base_qty == D(params["quantity"])
        else:
            complete = fill.base_qty > 0
        # Binance reports an IOC that didn't fully fill as EXPIRED (executedQty may be > 0).
        status = "FILLED" if complete else "EXPIRED"
        raw = {
            "symbol": rules.symbol,
            "orderId": order_id,
            "side": side,
            "type": order_type,
            "status": status,
            "executedQty": fmt(fill.base_qty),
            "cummulativeQuoteQty": fmt(fill.quote_qty),
            "fills": [{"price": fmt(fill.avg_price), "qty": fmt(fill.base_qty), "commission": fmt(commission), "commissionAsset": got_asset}]
            if fill.base_qty > 0
            else [],
            "paper": True,
        }
        log.debug("paper %s %s -> %s", params, status, raw["executedQty"])
        return OrderResult.from_api(raw)

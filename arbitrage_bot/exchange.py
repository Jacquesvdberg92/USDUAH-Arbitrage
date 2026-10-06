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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Iterable, List, Mapping, Optional
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
    """Real orders (Binance live or Spot Testnet, depending on the client's URL)."""

    name = "live"

    def balances(self) -> Dict[str, Decimal]:
        return self.market.balances()

    def _place(self, params: Dict[str, str]) -> OrderResult:
        log.debug("placing %s", params)
        return self.market.new_order(params)


class PaperExchange(Exchange):
    """Simulated fills against the *live* order book with simulated balances.

    Each order re-fetches the book, so the price moves that happen while our
    three legs are in flight show up in paper results too. It also enforces
    Binance's filters and balance checks, using the exact strings that would be
    sent. It cannot model being beaten to the liquidity by faster traders, so
    real results will be worse than paper results.
    """

    name = "paper"

    def __init__(
        self,
        market: BinanceClient,
        rules: Mapping[str, SymbolRules],
        fees: Mapping[str, Decimal],
        balances: Mapping[str, Decimal],
        depth_limit: int = 20,
    ):
        super().__init__(market, rules, depth_limit)
        self.fees = fees
        self._balances: Dict[str, Decimal] = {a: D(v) for a, v in balances.items()}
        self._lock = threading.Lock()
        self._order_id = 0

    def balances(self) -> Dict[str, Decimal]:
        with self._lock:
            return dict(self._balances)

    def _reject(self, code: int, msg: str):
        raise BinanceAPIError(400, code, msg)

    def _place(self, params: Dict[str, str]) -> OrderResult:
        rules = self.rules[params["symbol"]]
        side, order_type = params["side"], params["type"]
        book = self.market.order_book(rules.symbol, self.depth_limit)
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

"""Exchange trading rules, order books and fill simulation.

All prices and quantities are ``Decimal`` so that what we simulate is exactly
what we send to the exchange (no float rounding surprises on step sizes).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Callable, Iterable, Optional, Sequence, Tuple

ZERO = Decimal("0")
ONE = Decimal("1")
BPS = Decimal("10000")

BUY = "BUY"
SELL = "SELL"

Level = Tuple[Decimal, Decimal]  # (price, quantity)


def D(value) -> Decimal:
    """Convert API strings / numbers to Decimal without float artefacts."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_UP) * step


def fmt(value: Decimal) -> str:
    """Plain decimal string (no exponent, no trailing zeros) for API params."""
    text = format(value.normalize(), "f")
    return text if text not in ("-0", "") else "0"


@dataclass(frozen=True)
class SymbolRules:
    """The subset of Binance symbol filters that matter for our orders."""

    symbol: str
    base: str
    quote: str
    status: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    market_max_qty: Decimal
    min_notional: Decimal
    apply_min_to_market: bool
    quote_precision: int
    quote_order_qty_market_allowed: bool

    @property
    def trading(self) -> bool:
        return self.status == "TRADING"

    @classmethod
    def from_exchange_info(cls, raw: dict) -> "SymbolRules":
        filters = {f["filterType"]: f for f in raw.get("filters", [])}
        price = filters.get("PRICE_FILTER", {})
        lot = filters.get("LOT_SIZE", {})
        market_lot = filters.get("MARKET_LOT_SIZE", {})
        notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}

        max_qty = D(lot.get("maxQty", "0"))
        market_max = D(market_lot.get("maxQty", "0"))
        return cls(
            symbol=raw["symbol"],
            base=raw["baseAsset"],
            quote=raw["quoteAsset"],
            status=raw.get("status", "TRADING"),
            tick_size=D(price.get("tickSize", "0")),
            step_size=D(lot.get("stepSize", "0")),
            min_qty=D(lot.get("minQty", "0")),
            max_qty=max_qty,
            market_max_qty=market_max if market_max > 0 else max_qty,
            min_notional=D(notional.get("minNotional", "0")),
            apply_min_to_market=bool(
                notional.get("applyMinToMarket", notional.get("applyToMarket", True))
            ),
            quote_precision=int(raw.get("quoteAssetPrecision", raw.get("quotePrecision", 8))),
            quote_order_qty_market_allowed=bool(raw.get("quoteOrderQtyMarketAllowed", True)),
        )

    def round_qty(self, qty: Decimal) -> Decimal:
        return floor_to_step(qty, self.step_size)

    def round_quote(self, amount: Decimal) -> Decimal:
        return floor_to_step(amount, Decimal(1).scaleb(-self.quote_precision))

    def price_down(self, price: Decimal) -> Decimal:
        return floor_to_step(price, self.tick_size)

    def price_up(self, price: Decimal) -> Decimal:
        return ceil_to_step(price, self.tick_size)

    def order_error(self, qty: Decimal, price: Decimal, market: bool = False) -> Optional[str]:
        """Return why Binance would reject this order, or None if it passes."""
        if qty <= 0:
            return "quantity is zero"
        if qty < self.min_qty:
            return f"LOT_SIZE: {fmt(qty)} < minQty {fmt(self.min_qty)}"
        max_qty = self.market_max_qty if market else self.max_qty
        if max_qty > 0 and qty > max_qty:
            return f"LOT_SIZE: {fmt(qty)} > maxQty {fmt(max_qty)}"
        if self.step_size > 0 and qty % self.step_size != 0:
            return f"LOT_SIZE: {fmt(qty)} not a multiple of {fmt(self.step_size)}"
        if not market and self.tick_size > 0 and price % self.tick_size != 0:
            return f"PRICE_FILTER: {fmt(price)} not a multiple of {fmt(self.tick_size)}"
        if (not market or self.apply_min_to_market) and qty * price < self.min_notional:
            return f"NOTIONAL: {fmt(qty * price)} < minNotional {fmt(self.min_notional)}"
        return None


@dataclass(frozen=True)
class Ticker:
    """Best bid/ask (Binance ``bookTicker``)."""

    bid: Decimal
    bid_qty: Decimal
    ask: Decimal
    ask_qty: Decimal

    @classmethod
    def from_api(cls, raw: dict) -> "Ticker":
        return cls(D(raw["bidPrice"]), D(raw["bidQty"]), D(raw["askPrice"]), D(raw["askQty"]))


@dataclass(frozen=True)
class OrderBook:
    symbol: str
    bids: Tuple[Level, ...]  # highest price first
    asks: Tuple[Level, ...]  # lowest price first

    @classmethod
    def from_api(cls, symbol: str, raw: dict) -> "OrderBook":
        return cls(
            symbol=symbol,
            bids=tuple((D(p), D(q)) for p, q in raw.get("bids", [])),
            asks=tuple((D(p), D(q)) for p, q in raw.get("asks", [])),
        )

    @property
    def best_bid(self) -> Optional[Decimal]:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Optional[Decimal]:
        return self.asks[0][0] if self.asks else None

    def ticker(self) -> Optional[Ticker]:
        if not self.bids or not self.asks:
            return None
        return Ticker(self.bids[0][0], self.bids[0][1], self.asks[0][0], self.asks[0][1])


@dataclass(frozen=True)
class Fill:
    """Result of walking an order book."""

    base_qty: Decimal
    quote_qty: Decimal
    worst_price: Decimal  # last (least favourable) level touched; ZERO if nothing filled

    @property
    def avg_price(self) -> Decimal:
        return self.quote_qty / self.base_qty if self.base_qty else ZERO


def _walk(levels: Iterable[Level], qty: Decimal, acceptable: Callable[[Decimal], bool]) -> Fill:
    remaining = qty
    base = quote = worst = ZERO
    for price, size in levels:
        if remaining <= 0 or not acceptable(price):
            break
        take = min(size, remaining)
        base += take
        quote += take * price
        worst = price
        remaining -= take
    return Fill(base, quote, worst)


def fill_buy(asks: Sequence[Level], qty: Decimal, limit: Optional[Decimal] = None) -> Fill:
    """Buy up to ``qty`` base, optionally not paying more than ``limit``."""
    return _walk(asks, qty, lambda p: limit is None or p <= limit)


def fill_sell(bids: Sequence[Level], qty: Decimal, limit: Optional[Decimal] = None) -> Fill:
    """Sell up to ``qty`` base, optionally not accepting less than ``limit``."""
    return _walk(bids, qty, lambda p: limit is None or p >= limit)


def max_buy_qty(
    asks: Sequence[Level], budget: Decimal, step: Decimal, limit: Optional[Decimal] = None
) -> Tuple[Decimal, bool]:
    """Largest step-multiple base quantity ``budget`` quote can buy.

    Returns ``(qty, budget_bound)``; ``budget_bound`` is False when the book
    (or the limit price) ran out before the budget did.
    """
    remaining = budget
    total = ZERO
    budget_bound = False
    for price, size in asks:
        if limit is not None and price > limit:
            break
        cost = price * size
        if cost >= remaining:
            total += remaining / price
            budget_bound = True
            break
        total += size
        remaining -= cost
    return floor_to_step(total, step), budget_bound

"""Triangular cycles and depth/fee/filter-aware profit planning.

A cycle starts and ends in the home asset, e.g. USDT -> USDC -> TRY -> USDT.
Each hop uses whichever symbol exists for the pair (USDCUSDT or USDTUSDC) and
is a BUY when we hold the symbol's quote asset and a SELL when we hold its base.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .market import (
    BPS,
    BUY,
    ONE,
    SELL,
    ZERO,
    OrderBook,
    SymbolRules,
    Ticker,
    fill_buy,
    fill_sell,
    max_buy_qty,
)


@dataclass(frozen=True)
class Leg:
    symbol: str
    side: str
    from_asset: str
    to_asset: str


@dataclass(frozen=True)
class Cycle:
    home: str
    legs: Tuple[Leg, ...]

    @property
    def path(self) -> str:
        return " > ".join([self.legs[0].from_asset] + [leg.to_asset for leg in self.legs])

    @property
    def symbols(self) -> Tuple[str, ...]:
        return tuple(leg.symbol for leg in self.legs)


class PairIndex:
    """Look up the tradable symbol for an unordered asset pair."""

    def __init__(self, rules: Iterable[SymbolRules]):
        self.by_symbol: Dict[str, SymbolRules] = {}
        self._by_pair: Dict[frozenset, SymbolRules] = {}
        self._inactive: Dict[frozenset, SymbolRules] = {}
        for r in rules:
            self.by_symbol[r.symbol] = r
            key = frozenset((r.base, r.quote))
            if r.trading:
                self._by_pair[key] = r
            else:
                self._inactive.setdefault(key, r)

    def find(self, a: str, b: str) -> Optional[SymbolRules]:
        return self._by_pair.get(frozenset((a, b)))

    def describe_missing(self, a: str, b: str) -> str:
        dead = self._inactive.get(frozenset((a, b)))
        if dead is not None:
            return f"{dead.symbol} exists but its status is {dead.status}"
        return f"no {a}/{b} market"


def make_leg(rules: SymbolRules, from_asset: str, to_asset: str) -> Leg:
    if rules.quote == from_asset and rules.base == to_asset:
        return Leg(rules.symbol, BUY, from_asset, to_asset)
    if rules.base == from_asset and rules.quote == to_asset:
        return Leg(rules.symbol, SELL, from_asset, to_asset)
    raise ValueError(f"{rules.symbol} does not trade {from_asset} -> {to_asset}")


class MissingMarketError(ValueError):
    pass


def build_cycles(home: str, assets: Sequence[str], index: PairIndex) -> List[Cycle]:
    """Both directions of the triangle ``home``/``a``/``b``."""
    others = [a for a in assets if a != home]
    if len(others) != 2 or len(set(others)) != 2 or home not in assets:
        raise ValueError(f"triangle {list(assets)} must be the home asset {home} plus two others")
    a, b = others
    problems = [index.describe_missing(x, y) for x, y in ((home, a), (a, b), (b, home)) if not index.find(x, y)]
    if problems:
        raise MissingMarketError("; ".join(problems))

    cycles = []
    for first, second in ((a, b), (b, a)):
        path = (home, first, second, home)
        legs = tuple(make_leg(index.find(x, y), x, y) for x, y in zip(path, path[1:]))
        cycles.append(Cycle(home, legs))
    return cycles


def quick_edge_bps(cycle: Cycle, tickers: Mapping[str, Ticker], fees: Mapping[str, Decimal]) -> Optional[Decimal]:
    """Top-of-book return of one unit around the cycle, net of taker fees.

    This ignores depth and lot-size rounding, both of which can only make a
    profitable cycle worse, so for any positive threshold it is a cheap upper
    bound used to decide whether a full order-book check is worth the API calls.
    """
    amount = ONE
    for leg in cycle.legs:
        t = tickers.get(leg.symbol)
        if t is None or t.ask <= 0 or t.bid <= 0:
            return None
        gross = amount / t.ask if leg.side == BUY else amount * t.bid
        amount = gross * (ONE - fees[leg.symbol])
    return (amount - ONE) * BPS


@dataclass(frozen=True)
class LegPlan:
    leg: Leg
    amount_in: Decimal  # from_asset available to this leg
    spent: Decimal  # from_asset actually consumed
    received: Decimal  # to_asset after the taker fee
    base_qty: Decimal
    quote_qty: Decimal
    limit_price: Decimal  # worst book level reached = IOC limit price
    complete: bool  # False when the book ran out before amount_in was used

    @property
    def leftover(self) -> Decimal:
        return self.amount_in - self.spent


@dataclass(frozen=True)
class CyclePlan:
    cycle: Cycle
    legs: Tuple[LegPlan, ...]
    dust_value: Decimal  # leftover intermediate assets, valued in the home asset

    @property
    def spent(self) -> Decimal:
        return self.legs[0].spent

    @property
    def received(self) -> Decimal:
        return self.legs[-1].received

    @property
    def profit(self) -> Decimal:
        return self.received + self.dust_value - self.spent

    @property
    def profit_bps(self) -> Decimal:
        return self.profit / self.spent * BPS if self.spent else ZERO


def simulate_leg(
    leg: Leg,
    rules: SymbolRules,
    book: OrderBook,
    amount_in: Decimal,
    fee: Decimal,
) -> Optional[LegPlan]:
    """What a taker order spending ``amount_in`` would get, or None if Binance would reject it."""
    if leg.side == BUY:
        qty, complete = max_buy_qty(book.asks, amount_in, rules.step_size)
        fill = fill_buy(book.asks, qty)
        # A LIMIT buy reserves qty x limit price (the worst level), not the
        # average cost, so the order has to fit the budget at that price.
        while fill.base_qty > 0 and fill.base_qty * fill.worst_price > amount_in:
            qty = rules.round_qty(amount_in / fill.worst_price)
            if qty >= fill.base_qty:  # Decimal rounding at the 28th digit: step down explicitly
                qty = fill.base_qty - rules.step_size if rules.step_size > 0 else ZERO
            fill = fill_buy(book.asks, qty)
        gross, spent = fill.base_qty, fill.quote_qty
    else:
        qty = rules.round_qty(amount_in)
        fill = fill_sell(book.bids, qty)
        complete = fill.base_qty == qty
        gross, spent = fill.quote_qty, fill.base_qty
    if fill.base_qty <= 0 or rules.order_error(fill.base_qty, fill.worst_price) is not None:
        return None
    return LegPlan(
        leg=leg,
        amount_in=amount_in,
        spent=spent,
        received=gross * (ONE - fee),
        base_qty=fill.base_qty,
        quote_qty=fill.quote_qty,
        limit_price=fill.worst_price,
        complete=complete,
    )


def value_at_top(rules: SymbolRules, book: OrderBook, asset: str, amount: Decimal, fee: Decimal) -> Decimal:
    """Value ``amount`` of ``asset`` in the symbol's other asset at top of book, after fee."""
    if amount <= 0:
        return ZERO
    if asset == rules.base and book.best_bid:
        return amount * book.best_bid * (ONE - fee)
    if asset == rules.quote and book.best_ask:
        return amount / book.best_ask * (ONE - fee)
    return ZERO


def plan_cycle(
    cycle: Cycle,
    start_amount: Decimal,
    books: Mapping[str, OrderBook],
    rules: Mapping[str, SymbolRules],
    fees: Mapping[str, Decimal],
) -> Optional[CyclePlan]:
    """Simulate taking liquidity around the cycle with ``start_amount`` of the home asset."""
    legs: List[LegPlan] = []
    amount = start_amount
    for i, leg in enumerate(cycle.legs):
        plan = simulate_leg(leg, rules[leg.symbol], books[leg.symbol], amount, fees[leg.symbol])
        if plan is None:
            return None
        # Leg 1 may be smaller than asked for (thin book) - that just shrinks the
        # cycle. Legs 2 and 3 must absorb everything, or we'd be left holding inventory.
        if i > 0 and not plan.complete:
            return None
        legs.append(plan)
        amount = plan.received

    # Rounding leaves small "dust" of the two intermediate assets. Each one has a
    # direct market with the home asset (legs 1 and 3), so value it there.
    first, last = cycle.legs[0], cycle.legs[2]
    dust = value_at_top(rules[first.symbol], books[first.symbol], first.to_asset, legs[1].leftover, fees[first.symbol])
    dust += value_at_top(rules[last.symbol], books[last.symbol], last.from_asset, legs[2].leftover, fees[last.symbol])
    return CyclePlan(cycle, tuple(legs), dust)


def size_grid(min_size: Decimal, max_size: Decimal, points: int) -> List[Decimal]:
    """Geometric grid of trade sizes between ``min_size`` and ``max_size``."""
    if max_size < min_size or max_size <= 0:
        return []
    if points <= 1 or max_size == min_size:
        return [max_size]
    lo, hi = float(min_size), float(max_size)
    sizes = {Decimal(f"{lo * (hi / lo) ** (i / (points - 1)):.8f}") for i in range(points)}
    return sorted(sizes)


def find_best_plan(
    cycle: Cycle,
    books: Mapping[str, OrderBook],
    rules: Mapping[str, SymbolRules],
    fees: Mapping[str, Decimal],
    min_size: Decimal,
    max_size: Decimal,
    min_profit_bps: Decimal,
    points: int = 12,
) -> Optional[CyclePlan]:
    """Pick the trade size with the largest absolute profit that still clears
    ``min_profit_bps``. If no size clears it, return the best-bps plan so the
    caller can log how close we came."""
    best: Optional[CyclePlan] = None
    for size in size_grid(min_size, max_size, points):
        plan = plan_cycle(cycle, size, books, rules, fees)
        if plan is None:
            continue
        if best is None:
            best = plan
            continue
        plan_ok = plan.profit_bps >= min_profit_bps
        best_ok = best.profit_bps >= min_profit_bps
        if plan_ok and (not best_ok or plan.profit > best.profit):
            best = plan
        elif not plan_ok and not best_ok and plan.profit_bps > best.profit_bps:
            best = plan
    return best

"""Turns a CyclePlan into orders.

Leg 1 is an IOC limit order at the planned price: if the opportunity has
gone, it simply doesn't fill and we hold nothing. Once we hold an
intermediate asset we *must* get back to the home asset, so legs 2 and 3 try
an IOC at the planned price first and then sweep any remainder with a market
order. Each leg is sized from what the previous leg actually returned (after
fees), never from a fixed quantity. If a later leg is rejected, the position
is unwound straight back to the home asset.

Only *definite* rejections reach the handlers here: an order whose outcome is
unknown is resolved by LiveExchange, or raises OrderStatusUnknown. Whatever
exception ends a cycle early carries the partial CycleResult as
``err.cycle_result``, so the bot can still record what was traded.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional

import requests

from .exchange import BinanceAPIError, Exchange, OrderResult, OrderStatusUnknown
from .market import BUY, SELL, ZERO
from .triangle import CyclePlan, Leg, PairIndex

log = logging.getLogger(__name__)

TRADING_ERRORS = (BinanceAPIError, requests.RequestException)


class StuckPositionError(RuntimeError):
    """We hold an intermediate asset and could not get back to the home asset."""

    def __init__(self, asset: str, amount: Decimal, cause: Exception):
        super().__init__(f"holding {amount} {asset} that could not be unwound: {cause}")
        self.asset = asset
        self.amount = amount


@dataclass
class LegExecution:
    leg: Leg
    orders: List[OrderResult]
    spent: Decimal
    received: Decimal


@dataclass
class CycleResult:
    plan: Optional[CyclePlan]  # None for housekeeping orders (dust sweeps)
    status: str  # completed | no_fill | unwound | stuck | unknown | interrupted
    legs: List[LegExecution] = field(default_factory=list)
    home_spent: Decimal = ZERO
    home_received: Decimal = ZERO
    dust: Dict[str, Decimal] = field(default_factory=dict)  # leftovers of intermediate assets
    other_fees: Dict[str, Decimal] = field(default_factory=dict)  # e.g. BNB
    elapsed_ms: float = 0.0
    error: Optional[str] = None

    @property
    def pnl(self) -> Decimal:
        """Realised change in the home asset (dust and BNB fees reported separately)."""
        return self.home_received - self.home_spent

    def to_dict(self) -> dict:
        plan = self.plan
        return {
            "cycle": plan.cycle.path if plan else None,
            "status": self.status,
            "planned_spent": str(plan.spent) if plan else None,
            "planned_profit": str(plan.profit) if plan else None,
            "planned_profit_bps": str(round(plan.profit_bps, 3)) if plan else None,
            "home_spent": str(self.home_spent),
            "home_received": str(self.home_received),
            "pnl": str(self.pnl),
            "dust": {k: str(v) for k, v in self.dust.items()},
            "other_fees": {k: str(v) for k, v in self.other_fees.items()},
            "elapsed_ms": round(self.elapsed_ms, 1),
            "error": self.error,
            "orders": [o.raw for leg in self.legs for o in leg.orders],
        }


def _add(totals: Dict[str, Decimal], asset: str, amount: Decimal) -> None:
    if amount:
        totals[asset] = totals.get(asset, ZERO) + amount


class CycleExecutor:
    def __init__(self, exchange: Exchange, index: PairIndex, home: str, complete_with_market: bool = True):
        self.exchange = exchange
        self.index = index
        self.rules = index.by_symbol
        self.home = home
        self.complete_with_market = complete_with_market

    def execute(self, plan: CyclePlan) -> CycleResult:
        started = time.monotonic()
        result = CycleResult(plan, "no_fill")
        try:
            self._run(plan, result)
        except BaseException as err:
            if isinstance(err, StuckPositionError):
                result.status = "stuck"
            elif isinstance(err, OrderStatusUnknown):
                result.status = "unknown"
            elif not isinstance(err, TRADING_ERRORS) or result.legs:
                result.status = "interrupted"
            if result.status != "no_fill":
                self._settle_from_orders(result)
            message = str(err) or type(err).__name__
            result.error = f"{result.error}; {message}" if result.error else message
            err.cycle_result = result  # type: ignore[attr-defined]
            raise
        finally:
            result.elapsed_ms = (time.monotonic() - started) * 1000
        return result

    def _run(self, plan: CyclePlan, result: CycleResult) -> None:
        first = plan.legs[0]
        # Entry: exactly the planned size and worst price. A definite rejection
        # here leaves us flat, so it propagates to the bot's error handling.
        order = self.exchange.limit_ioc(first.leg.symbol, first.leg.side, first.base_qty, first.limit_price)
        entry = self._summarise(first.leg, [order], result)
        result.home_spent = entry.spent
        if entry.received <= 0:
            return  # opportunity was gone - nothing filled

        held_asset, held = first.leg.to_asset, entry.received
        for leg_plan in plan.legs[1:]:
            leg_ex: Optional[LegExecution] = None
            try:
                leg_ex = self._complete_leg(leg_plan.leg, held, leg_plan.limit_price, result)
            except TRADING_ERRORS as err:
                result.error = f"{leg_plan.leg.symbol}: {err}"
                log.error("leg %s failed: %s", leg_plan.leg.symbol, err)
            if leg_ex is None or leg_ex.received <= 0:
                result.status = "unwound"
                spent = ZERO
                if self._tradable(held_asset, held):
                    spent, result.home_received = self.unwind(held_asset, held, result)
                # else: e.g. leg 1 only partly filled, below Binance's minimum order - keep it as dust
                _add(result.dust, held_asset, held - spent)
                return
            _add(result.dust, held_asset, held - leg_ex.spent)
            held_asset, held = leg_plan.leg.to_asset, leg_ex.received

        result.status = "completed"
        result.home_received = held

    def _complete_leg(self, leg: Leg, amount_in: Decimal, limit: Decimal, result: CycleResult) -> LegExecution:
        orders: List[OrderResult] = []
        try:
            self._place_leg(leg, amount_in, limit, orders)
        except BaseException:
            self._summarise(leg, orders, result)  # keep whatever already executed in the books
            raise
        return self._summarise(leg, orders, result)

    def _place_leg(self, leg: Leg, amount_in: Decimal, limit: Decimal, orders: List[OrderResult]) -> None:
        rules = self.rules[leg.symbol]
        if leg.side == SELL:
            qty = rules.round_qty(amount_in)
            if rules.order_error(qty, limit) is None:
                orders.append(self.exchange.limit_ioc(leg.symbol, SELL, qty, limit))
            remaining = rules.round_qty(qty - sum((o.executed_qty for o in orders), ZERO))
            if self.complete_with_market and remaining > 0 and rules.order_error(remaining, limit, market=True) is None:
                orders += self._fallback(self.exchange.market_sell, leg.symbol, remaining)
        else:
            qty = rules.round_qty(amount_in / limit)
            if rules.order_error(qty, limit) is None:
                orders.append(self.exchange.limit_ioc(leg.symbol, BUY, qty, limit))
            remaining = rules.round_quote(amount_in - sum((o.quote_qty for o in orders), ZERO))
            if (
                self.complete_with_market
                and remaining > 0
                and rules.quote_order_qty_market_allowed
                and rules.order_error(rules.round_qty(remaining / limit), limit, market=True) is None
            ):
                orders += self._fallback(self.exchange.market_buy_quote, leg.symbol, remaining)

    def _fallback(self, place, symbol: str, amount: Decimal) -> List[OrderResult]:
        """Market order for the remainder. A failure here is logged, not raised:
        the IOC part already traded, and the leftover gets swept later."""
        try:
            return [place(symbol, amount)]
        except TRADING_ERRORS as err:
            log.warning("market fallback on %s failed: %s", symbol, err)
            return []

    def _settle_from_orders(self, result: CycleResult) -> None:
        """Rebuild a cut-short cycle's books from the orders that actually executed:
        home asset in/out, and every other asset still held (an open position)."""
        net: Dict[str, Decimal] = {}
        for execution in result.legs:
            _add(net, execution.leg.from_asset, -execution.spent)
            _add(net, execution.leg.to_asset, execution.received)
        result.home_spent = sum((e.spent for e in result.legs if e.leg.from_asset == self.home), ZERO)
        result.home_received = sum((e.received for e in result.legs if e.leg.to_asset == self.home), ZERO)
        result.dust = {asset: amount for asset, amount in net.items() if asset != self.home and amount > 0}

    def _summarise(self, leg: Leg, orders: List[OrderResult], result: CycleResult) -> LegExecution:
        rules = self.rules[leg.symbol]
        spent = received = ZERO
        for o in orders:
            s, r, other = o.flows(rules)
            spent += s
            received += r
            for asset, amount in other.items():
                _add(result.other_fees, asset, amount)
        execution = LegExecution(leg, orders, spent, received)
        result.legs.append(execution)
        return execution

    def unwind(self, asset: str, amount: Decimal, result: Optional[CycleResult] = None):
        """Market-convert ``amount`` of ``asset`` straight back to the home asset.

        Returns (asset spent, home received). Raises StuckPositionError if the
        exchange refuses, so the bot can stop and a human can look.
        """
        rules = self.index.find(asset, self.home)
        if rules is None:
            raise StuckPositionError(asset, amount, ValueError(f"no {asset}/{self.home} market"))
        if asset == rules.base:
            leg, place, size = Leg(rules.symbol, SELL, asset, self.home), self.exchange.market_sell, rules.round_qty(amount)
        else:
            leg, place, size = Leg(rules.symbol, BUY, asset, self.home), self.exchange.market_buy_quote, rules.round_quote(amount)
        log.warning("unwinding %s %s via %s", size, asset, rules.symbol)
        try:
            order = place(rules.symbol, size)
        except TRADING_ERRORS as err:
            raise StuckPositionError(asset, amount, err) from err
        execution = self._summarise(leg, [order], result if result is not None else CycleResult(None, "unwound"))
        return execution.spent, execution.received

    def _tradable(self, asset: str, amount: Decimal) -> bool:
        try:
            return self.sweepable(asset, amount)
        except TRADING_ERRORS:
            return True  # can't tell right now - try the unwind anyway

    def sweepable(self, asset: str, amount: Decimal) -> bool:
        """Is this leftover big enough to trade back to the home asset?"""
        rules = self.index.find(asset, self.home)
        if rules is None or amount <= 0:
            return False
        ticker = self.exchange.book_tickers([rules.symbol]).get(rules.symbol)
        if ticker is None:
            return False
        if asset == rules.base:
            return ticker.bid > 0 and rules.order_error(rules.round_qty(amount), ticker.bid, market=True) is None
        if ticker.ask <= 0:
            return False  # empty book - e.g. the market is halted
        return rules.order_error(rules.round_qty(amount / ticker.ask), ticker.ask, market=True) is None

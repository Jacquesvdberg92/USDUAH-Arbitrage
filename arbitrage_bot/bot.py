"""Main loop: screen cheaply, confirm against depth, execute, and enforce risk limits."""

from __future__ import annotations

import json
import logging
import signal
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Dict, Iterator, List, Mapping, Optional

from .config import Config
from .exchange import BinanceAPIError, Exchange, OrderStatusUnknown
from .executor import TRADING_ERRORS, CycleExecutor, CycleResult, StuckPositionError
from .market import ONE, ZERO, Ticker
from .triangle import Cycle, CyclePlan, find_best_plan, quick_edge_bps

log = logging.getLogger(__name__)

# Binance: the API key is invalid, lacks spot-trading permission, or the IP isn't whitelisted.
BAD_KEY_CODES = (-2014, -2015)


class StopTrading(Exception):
    """Stop the bot. ``fatal`` stops mean something needs a human (exit code 2)."""

    def __init__(self, reason: str, fatal: bool = True):
        super().__init__(reason)
        self.fatal = fatal


@dataclass
class Stats:
    started: float = field(default_factory=time.time)
    scans: int = 0
    screened: int = 0  # cycles whose top-of-book edge cleared the threshold
    depth_checks: int = 0
    qualified: int = 0  # plans that cleared the threshold after depth/rounding
    cycles: int = 0
    no_fills: int = 0
    unwinds: int = 0
    unresolved: int = 0  # cut short by an unknown order outcome or a forced Ctrl+C
    wins: int = 0
    losses: int = 0
    pnl: Decimal = ZERO  # realised change in the home asset
    fees_value: Decimal = ZERO  # fees charged in other assets (e.g. BNB), valued in the home asset
    planned_pnl: Decimal = ZERO
    errors: int = 0
    best_edge_bps: Optional[Decimal] = None  # best top-of-book edge since the last STATS line
    best_edge_cycle: str = ""
    best_ever_bps: Optional[Decimal] = None  # ... and over the whole run
    best_ever_cycle: str = ""


class ArbitrageBot:
    def __init__(
        self,
        config: Config,
        exchange: Exchange,
        executor: CycleExecutor,
        cycles: List[Cycle],
        fees: Mapping[str, Decimal],
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = config
        self.exchange = exchange
        self.executor = executor
        self.cycles = cycles
        self.fees = fees
        self.sleep = sleep
        self.home = config.home_asset
        self.symbols = sorted({s for c in cycles for s in c.symbols})
        self.stats = Stats()
        self.dust: Dict[str, Decimal] = {}  # leftovers (and stuck positions) this bot created, by asset
        self.tickers: Dict[str, Ticker] = {}
        self._prices: Dict[str, Decimal] = {}  # last known home-asset price of fee assets
        self.home_balance = exchange.balances().get(self.home, ZERO)
        self.consecutive_losses = 0
        self.consecutive_errors = 0
        self.consecutive_order_errors = 0
        self.stop_requested = False
        self.open_position_warning: Optional[str] = None  # set when a cut-short cycle may have left a position

    # -- one scan ---------------------------------------------------------
    def step(self) -> Optional[CycleResult]:
        cfg = self.config
        self.stats.scans += 1
        self.tickers = self.exchange.book_tickers(self.symbols)

        ranked = []
        for cycle in self.cycles:
            edge = quick_edge_bps(cycle, self.tickers, self.fees)
            if edge is None:
                continue
            if self.stats.best_edge_bps is None or edge > self.stats.best_edge_bps:
                self.stats.best_edge_bps, self.stats.best_edge_cycle = edge, cycle.path
            if self.stats.best_ever_bps is None or edge > self.stats.best_ever_bps:
                self.stats.best_ever_bps, self.stats.best_ever_cycle = edge, cycle.path
            if edge >= cfg.min_profit_bps:
                ranked.append((edge, cycle))
        if not ranked:
            return None
        self.stats.screened += len(ranked)
        ranked.sort(key=lambda pair: pair[0], reverse=True)

        max_size = min(cfg.max_trade, self.home_balance * cfg.max_balance_fraction)
        if max_size < cfg.min_trade:
            log.warning("home balance %s %s is too small to trade (min_trade %s)", self.home_balance, self.home, cfg.min_trade)
            return None

        for edge, cycle in ranked[: cfg.max_depth_checks_per_scan]:
            books = self.exchange.order_books(cycle.symbols)
            self.stats.depth_checks += 1
            plan = find_best_plan(
                cycle, books, self.exchange.rules, self.fees, cfg.min_trade, max_size, cfg.min_profit_bps, cfg.size_points
            )
            if plan is None or plan.profit_bps < cfg.min_profit_bps or plan.profit < cfg.min_profit_abs:
                log.debug(
                    "%s: top-of-book %.2f bps, after depth/rounding %s",
                    cycle.path,
                    edge,
                    "not tradable" if plan is None else f"{plan.profit_bps:.2f} bps ({plan.profit:.4f} {self.home})",
                )
                continue
            self.stats.qualified += 1
            log.info(
                "OPPORTUNITY %s: spend %.4f %s, expect %+.4f (%.2f bps; top-of-book %.2f bps)",
                cycle.path, plan.spent, self.home, plan.profit, plan.profit_bps, edge,
            )
            with self._defer_interrupt():  # orders and their bookkeeping happen as one unit
                result = self._execute(plan)
                self._record(result)
            return result
        return None

    def _execute(self, plan: CyclePlan) -> CycleResult:
        try:
            result = self.executor.execute(plan)
        except TRADING_ERRORS as err:
            self._order_rejected(err)  # leg 1 was definitely rejected: we're still flat
            raise
        except (StuckPositionError, OrderStatusUnknown) as err:
            self._record_partial(err)
            kind = "STUCK POSITION" if isinstance(err, StuckPositionError) else "ORDER STATUS UNKNOWN"
            raise StopTrading(f"{kind} - check your Binance account: {err}") from err
        except BaseException as err:  # Ctrl+C pressed twice, or a bug: still record what traded
            self._record_partial(err)
            raise
        self.consecutive_order_errors = 0
        return result

    def _order_rejected(self, err: Exception) -> None:
        """Order-only errors don't show up on quiet scans, so count them separately."""
        self.consecutive_order_errors += 1
        if isinstance(err, BinanceAPIError) and err.code in BAD_KEY_CODES:
            raise StopTrading(f"Binance rejected the API key ({err}) - it needs spot trading enabled and your IP whitelisted") from err
        if self.consecutive_order_errors >= self.config.max_consecutive_errors:
            raise StopTrading(f"{self.consecutive_order_errors} rejected orders in a row, last: {err}") from err

    @contextmanager
    def _defer_interrupt(self) -> Iterator[None]:
        """Ctrl+C while orders are in flight finishes the cycle first; a second Ctrl+C forces it."""
        if threading.current_thread() is not threading.main_thread():
            yield
            return

        def handler(signum, frame):
            if self.stop_requested:
                raise KeyboardInterrupt
            self.stop_requested = True
            log.warning("Ctrl+C: finishing the current cycle first so no position is left open (press again to force)")

        previous = signal.signal(signal.SIGINT, handler)
        try:
            yield
        finally:
            signal.signal(signal.SIGINT, previous if previous is not None else signal.default_int_handler)

    # -- bookkeeping --------------------------------------------------------
    def value_in_home(self, asset: str, amount: Decimal) -> Decimal:
        """Rough top-of-book value of an intermediate asset, after fees."""
        if asset == self.home:
            return amount
        rules = self.executor.index.find(asset, self.home)
        t = self.tickers.get(rules.symbol) if rules else None
        if t is None or amount <= 0:
            return ZERO
        price = t.bid if asset == rules.base else t.ask
        if price <= 0:
            return ZERO  # empty book, e.g. the market is halted
        fee = ONE - self.fees.get(rules.symbol, ZERO)
        return amount * price * fee if asset == rules.base else amount / price * fee

    def dust_value(self, dust: Optional[Mapping[str, Decimal]] = None) -> Decimal:
        dust = self.dust if dust is None else dust
        return sum((self.value_in_home(a, v) for a, v in dust.items()), ZERO)

    def _price_in_home(self, asset: str) -> Decimal:
        """Mid-ish price of a fee asset (e.g. BNB) in the home asset; last known price if the lookup fails."""
        if asset == self.home:
            return ONE
        rules = self.executor.index.find(asset, self.home)
        if rules is None:
            log.warning("  can't value %s fees: no %s/%s market", asset, asset, self.home)
            return self._prices.get(asset, ZERO)
        try:
            t = self.tickers.get(rules.symbol) or self.exchange.book_tickers([rules.symbol]).get(rules.symbol)
        except TRADING_ERRORS as err:
            log.warning("  can't price %s fees right now (%s); using last known price", asset, err)
            t = None
        if t is not None and t.bid > 0 and t.ask > 0:
            self._prices[asset] = t.bid if asset == rules.base else ONE / t.ask
        return self._prices.get(asset, ZERO)

    def fees_value(self, fees: Mapping[str, Decimal]) -> Decimal:
        return sum((amount * self._price_in_home(asset) for asset, amount in fees.items()), ZERO)

    def _record(self, result: CycleResult, final: bool = False) -> None:
        """Book a cycle. ``final`` is for a cycle that ended the run: no sweeping, no limit checks."""
        stats = self.stats
        dust_value = self.dust_value(result.dust)
        fee_value = self.fees_value(result.other_fees)
        stats.fees_value += fee_value
        if result.status == "no_fill":
            stats.no_fills += 1
            log.info("  no fill on entry (%.0f ms) - the opportunity was already gone", result.elapsed_ms)
        else:
            stats.pnl += result.pnl
            for asset, amount in result.dust.items():
                self.dust[asset] = self.dust.get(asset, ZERO) + amount
        if result.status in ("unknown", "interrupted"):
            # We don't know how this cycle ended, so it is neither a win nor a loss.
            stats.unresolved += 1
            log.error(
                "  %s after %.0f ms: realised %+.6f %s so far, still holding %s - check your Binance account",
                result.status.upper(), result.elapsed_ms, result.pnl, self.home, result.dust or "nothing we know of",
            )
        elif result.status != "no_fill":
            stats.cycles += 1
            if result.status == "unwound":
                stats.unwinds += 1
            if result.plan is not None:
                stats.planned_pnl += result.plan.profit
            if result.pnl - fee_value + dust_value < 0:
                stats.losses += 1
                self.consecutive_losses += 1
            else:
                stats.wins += 1
                self.consecutive_losses = 0
            log.info(
                "  %s in %.0f ms: realised %+.6f %s | dust %+.6f | fees paid in other assets %+.6f | planned %+.6f",
                result.status.upper(), result.elapsed_ms, result.pnl, self.home, dust_value, -fee_value,
                result.plan.profit if result.plan else ZERO,
            )
        self._write_trade_log(result, dust_value, fee_value)
        if final:
            return

        try:
            self._sweep_dust()
        except TRADING_ERRORS as err:  # housekeeping only - never skip the risk checks below
            log.warning("  post-trade sweep/balance refresh failed: %s", err)
        self._check_limits()

    def _record_partial(self, err: BaseException) -> None:
        partial = getattr(err, "cycle_result", None)
        if partial is None:
            return
        if partial.status in ("stuck", "unknown", "interrupted"):
            self.open_position_warning = f"{partial.status} cycle, holding {partial.dust or 'unknown'}"
        try:
            self._record(partial, final=True)
        except Exception as record_err:  # never let bookkeeping hide the original problem
            log.error("  could not record the interrupted cycle: %s (orders: %s)", record_err,
                      [o.raw for leg in partial.legs for o in leg.orders])

    def equity_change(self) -> Decimal:
        """Realised P&L, minus fees paid in other assets, plus what our leftovers are worth."""
        return self.stats.pnl - self.stats.fees_value + self.dust_value()

    def _check_limits(self) -> None:
        cfg = self.config
        equity = self.equity_change()
        if equity <= -cfg.max_loss:
            raise StopTrading(f"loss limit hit: {equity:.4f} {self.home} <= -{cfg.max_loss}")
        if self.consecutive_losses >= cfg.max_consecutive_losses:
            raise StopTrading(f"{self.consecutive_losses} losing cycles in a row")
        if cfg.max_cycles and self.stats.cycles >= cfg.max_cycles:
            raise StopTrading(f"max_cycles ({cfg.max_cycles}) reached", fatal=False)

    def _sweep_dust(self) -> None:
        """Once leftovers are big enough to trade, convert them back to the home asset."""
        for asset, amount in list(self.dust.items()):
            if amount <= 0 or not self.executor.sweepable(asset, amount):
                continue
            sweep = CycleResult(None, "sweep")
            try:
                spent, received = self.executor.unwind(asset, amount, sweep)
            except StuckPositionError as err:
                log.warning("  could not sweep leftovers: %s", err)
                continue
            self.dust[asset] = amount - spent
            self.stats.pnl += received
            self.stats.fees_value += self.fees_value(sweep.other_fees)
            log.info("  swept %s %s of leftovers into %s %s", spent, asset, received, self.home)
        self.home_balance = self.exchange.balances().get(self.home, ZERO)

    def _write_trade_log(self, result: CycleResult, dust_value: Decimal, fee_value: Decimal) -> None:
        if not self.config.trade_log:
            return
        row = {
            "time": datetime.now(timezone.utc).isoformat(),
            "mode": self.config.mode,
            "dust_value": str(dust_value),
            "other_fees_value": str(fee_value),
        }
        row.update(result.to_dict())
        try:
            with open(self.config.trade_log, "a") as fh:
                fh.write(json.dumps(row) + "\n")
        except OSError as err:
            log.error("  could not write the trade log (%s): %s", err, json.dumps(row))

    def summary(self, whole_run: bool = False) -> str:
        s = self.stats
        minutes = (time.time() - s.started) / 60
        edge, cycle = (s.best_ever_bps, s.best_ever_cycle) if whole_run else (s.best_edge_bps, s.best_edge_cycle)
        best = f"{edge:.2f} bps ({cycle})" if edge is not None else "n/a"
        return (
            f"{minutes:.1f} min | scans {s.scans} | screened {s.screened} | depth checks {s.depth_checks} | "
            f"qualified {s.qualified} | cycles {s.cycles} (won {s.wins}, lost {s.losses}, unwound {s.unwinds}, "
            f"no-fill {s.no_fills}, unresolved {s.unresolved}) | realised P&L {s.pnl:+.6f} {self.home} (planned {s.planned_pnl:+.6f}) | "
            f"other-asset fees ~{s.fees_value:.6f} | dust ~{self.dust_value():.6f} | "
            f"net ~{self.equity_change():+.6f} {self.home} | errors {s.errors} | "
            f"best top-of-book edge{' (whole run)' if whole_run else ''} {best}"
        )

    # -- loop -----------------------------------------------------------------
    def run(self) -> int:
        """Trade until a stop condition. Returns the process exit code: 2 when a human should look."""
        cfg = self.config
        log.info(
            "watching %d cycles on %d symbols | min profit %s bps & %s %s | size %s-%s %s | balance %s %s",
            len(self.cycles), len(self.symbols), cfg.min_profit_bps, cfg.min_profit_abs, self.home,
            cfg.min_trade, cfg.max_trade, self.home, self.home_balance, self.home,
        )
        exit_code = 0
        last_stats = time.monotonic()
        try:
            while True:
                try:
                    result = self.step()
                    self.consecutive_errors = 0
                except (StuckPositionError, OrderStatusUnknown) as err:  # e.g. from a dust sweep
                    raise StopTrading(f"check your Binance account: {err}") from err
                except TRADING_ERRORS as err:
                    if self.stop_requested:  # Ctrl+C arrived while the rejected entry order was in flight
                        raise StopTrading("stopped by Ctrl+C (entry order was rejected, nothing held)", fatal=False) from err
                    self._handle_error(err)
                    continue
                if self.stop_requested:
                    raise StopTrading("stopped by Ctrl+C after finishing the cycle", fatal=False)
                if time.monotonic() - last_stats >= cfg.stats_interval_sec:
                    log.info("STATS %s", self.summary())
                    self.stats.best_edge_bps, self.stats.best_edge_cycle = None, ""
                    last_stats = time.monotonic()
                self.sleep(cfg.cooldown_sec if result else cfg.poll_interval_sec)
        except StopTrading as stop:
            if stop.fatal:
                log.error("STOPPING: %s", stop)
                exit_code = 2
            else:
                log.info("stopping: %s", stop)
        except KeyboardInterrupt:
            if self.open_position_warning:
                log.error("INTERRUPTED mid-cycle (%s) - check your Binance account", self.open_position_warning)
                exit_code = 2
            else:
                log.info("interrupted")
        finally:
            log.info("FINAL %s", self.summary(whole_run=True))
        return exit_code

    def _handle_error(self, err: Exception) -> None:
        self.stats.errors += 1
        self.consecutive_errors += 1
        retry_after = getattr(err, "retry_after", None)
        if isinstance(err, BinanceAPIError) and err.code == -1021 and hasattr(self.exchange.market, "sync_time"):
            try:
                self.exchange.market.sync_time()  # our clock drifted outside recvWindow
            except TRADING_ERRORS as sync_err:
                log.warning("time resync failed: %s", sync_err)
        delay = retry_after or min(60.0, 2.0 ** self.consecutive_errors)
        log.warning("error %d/%d: %s - retrying in %.0fs", self.consecutive_errors, self.config.max_consecutive_errors, err, delay)
        if self.consecutive_errors >= self.config.max_consecutive_errors:
            raise StopTrading(f"{self.consecutive_errors} errors in a row, last: {err}")
        self.sleep(delay)

    # -- diagnostics ----------------------------------------------------------
    def report(self) -> str:
        """Current edge of every cycle - top of book and after depth/rounding. Never trades."""
        cfg = self.config
        self.tickers = self.exchange.book_tickers(self.symbols)
        max_size = min(cfg.max_trade, max(self.home_balance * cfg.max_balance_fraction, cfg.min_trade))
        rows = [f"{'cycle':32} {'top-of-book':>12} {'best size':>11} {'net profit':>12} {'net bps':>9}"]
        for cycle in self.cycles:
            edge = quick_edge_bps(cycle, self.tickers, self.fees)
            books = self.exchange.order_books(cycle.symbols)
            plan = find_best_plan(cycle, books, self.exchange.rules, self.fees, cfg.min_trade, max_size, cfg.min_profit_bps, cfg.size_points)
            edge_text = f"{edge:.2f} bps" if edge is not None else "n/a"
            if plan is None:
                rows.append(f"{cycle.path:32} {edge_text:>12} {'-':>11} {'-':>12} {'-':>9}")
            else:
                rows.append(f"{cycle.path:32} {edge_text:>12} {plan.spent:>11.2f} {plan.profit:>12.6f} {plan.profit_bps:>9.2f}")
        rows.append(f"(fees included; trade threshold is {cfg.min_profit_bps} bps and {cfg.min_profit_abs} {self.home})")
        return "\n".join(rows)

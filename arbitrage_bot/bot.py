"""Main loop: screen cheaply, confirm against depth, execute, and enforce risk limits."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Dict, List, Mapping, Optional

from .config import Config
from .exchange import BinanceAPIError, Exchange
from .executor import TRADING_ERRORS, CycleExecutor, CycleResult, StuckPositionError
from .market import ONE, ZERO, Ticker
from .triangle import Cycle, find_best_plan, quick_edge_bps

log = logging.getLogger(__name__)


class StopTrading(Exception):
    pass


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
    wins: int = 0
    losses: int = 0
    pnl: Decimal = ZERO  # realised, in the home asset
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
        self.dust: Dict[str, Decimal] = {}  # leftovers this bot created, by asset
        self.tickers: Dict[str, Ticker] = {}
        self.home_balance = exchange.balances().get(self.home, ZERO)
        self.consecutive_losses = 0
        self.consecutive_errors = 0

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
            result = self.executor.execute(plan)
            self._record(result)
            return result
        return None

    # -- bookkeeping --------------------------------------------------------
    def value_in_home(self, asset: str, amount: Decimal) -> Decimal:
        """Rough top-of-book value of an intermediate asset, after fees."""
        if asset == self.home:
            return amount
        rules = self.executor.index.find(asset, self.home)
        t = self.tickers.get(rules.symbol) if rules else None
        if t is None or amount <= 0:
            return ZERO
        fee = ONE - self.fees.get(rules.symbol, ZERO)
        return amount * t.bid * fee if asset == rules.base else amount / t.ask * fee

    def dust_value(self, dust: Optional[Mapping[str, Decimal]] = None) -> Decimal:
        dust = self.dust if dust is None else dust
        return sum((self.value_in_home(a, v) for a, v in dust.items()), ZERO)

    def _record(self, result: CycleResult) -> None:
        cfg, stats = self.config, self.stats
        dust_value = self.dust_value(result.dust)
        if result.status == "no_fill":
            stats.no_fills += 1
            log.info("  no fill on entry (%.0f ms) - the opportunity was already gone", result.elapsed_ms)
        else:
            stats.cycles += 1
            if result.status == "unwound":
                stats.unwinds += 1
            stats.pnl += result.pnl
            stats.planned_pnl += result.plan.profit
            for asset, amount in result.dust.items():
                self.dust[asset] = self.dust.get(asset, ZERO) + amount
            if result.pnl + dust_value < 0:
                stats.losses += 1
                self.consecutive_losses += 1
            else:
                stats.wins += 1
                self.consecutive_losses = 0
            log.info(
                "  %s in %.0f ms: realised %+.6f %s (%+.6f in dust), planned %+.6f%s",
                result.status.upper(), result.elapsed_ms, result.pnl, self.home, dust_value, result.plan.profit,
                f" | other fees {result.other_fees}" if result.other_fees else "",
            )
        self._write_trade_log(result, dust_value)

        try:
            self._sweep_dust()
        except TRADING_ERRORS as err:  # housekeeping only - never skip the risk checks below
            log.warning("  post-trade sweep/balance refresh failed: %s", err)

        equity_change = stats.pnl + self.dust_value()
        if equity_change <= -cfg.max_loss:
            raise StopTrading(f"loss limit hit: {equity_change:.4f} {self.home} <= -{cfg.max_loss}")
        if self.consecutive_losses >= cfg.max_consecutive_losses:
            raise StopTrading(f"{self.consecutive_losses} losing cycles in a row")
        if cfg.max_cycles and stats.cycles >= cfg.max_cycles:
            raise StopTrading(f"max_cycles ({cfg.max_cycles}) reached")

    def _sweep_dust(self) -> None:
        """Once leftovers are big enough to trade, convert them back to the home asset."""
        for asset, amount in list(self.dust.items()):
            if amount <= 0 or not self.executor.sweepable(asset, amount):
                continue
            try:
                spent, received = self.executor.unwind(asset, amount)
            except StuckPositionError as err:
                log.warning("  could not sweep leftovers: %s", err)
                continue
            self.dust[asset] = amount - spent
            self.stats.pnl += received
            log.info("  swept %s %s of leftovers into %s %s", spent, asset, received, self.home)
        self.home_balance = self.exchange.balances().get(self.home, ZERO)

    def _write_trade_log(self, result: CycleResult, dust_value: Decimal) -> None:
        if not self.config.trade_log:
            return
        row = {"time": datetime.now(timezone.utc).isoformat(), "mode": self.config.mode, "dust_value": str(dust_value)}
        row.update(result.to_dict())
        with open(self.config.trade_log, "a") as fh:
            fh.write(json.dumps(row) + "\n")

    def summary(self, whole_run: bool = False) -> str:
        s = self.stats
        minutes = (time.time() - s.started) / 60
        edge, cycle = (s.best_ever_bps, s.best_ever_cycle) if whole_run else (s.best_edge_bps, s.best_edge_cycle)
        best = f"{edge:.2f} bps ({cycle})" if edge is not None else "n/a"
        return (
            f"{minutes:.1f} min | scans {s.scans} | screened {s.screened} | depth checks {s.depth_checks} | "
            f"qualified {s.qualified} | cycles {s.cycles} (won {s.wins}, lost {s.losses}, unwound {s.unwinds}, "
            f"no-fill {s.no_fills}) | realised P&L {s.pnl:+.6f} {self.home} (planned {s.planned_pnl:+.6f}) | "
            f"dust ~{self.dust_value():.6f} {self.home} | errors {s.errors} | "
            f"best top-of-book edge{' (whole run)' if whole_run else ''} {best}"
        )

    # -- loop -----------------------------------------------------------------
    def run(self) -> None:
        cfg = self.config
        log.info(
            "watching %d cycles on %d symbols | min profit %s bps & %s %s | size %s-%s %s | balance %s %s",
            len(self.cycles), len(self.symbols), cfg.min_profit_bps, cfg.min_profit_abs, self.home,
            cfg.min_trade, cfg.max_trade, self.home, self.home_balance, self.home,
        )
        last_stats = time.monotonic()
        try:
            while True:
                try:
                    result = self.step()
                    self.consecutive_errors = 0
                except StuckPositionError as err:
                    raise StopTrading(f"STUCK POSITION - check your account manually: {err}") from err
                except TRADING_ERRORS as err:
                    self._handle_error(err)
                    continue
                if time.monotonic() - last_stats >= cfg.stats_interval_sec:
                    log.info("STATS %s", self.summary())
                    self.stats.best_edge_bps, self.stats.best_edge_cycle = None, ""
                    last_stats = time.monotonic()
                self.sleep(cfg.cooldown_sec if result else cfg.poll_interval_sec)
        except StopTrading as stop:
            log.warning("stopping: %s", stop)
        except KeyboardInterrupt:
            log.info("interrupted")
        finally:
            log.info("FINAL %s", self.summary(whole_run=True))

    def _handle_error(self, err: Exception) -> None:
        self.stats.errors += 1
        self.consecutive_errors += 1
        retry_after = getattr(err, "retry_after", None)
        if isinstance(err, BinanceAPIError) and err.code == -1021 and hasattr(self.exchange.market, "sync_time"):
            self.exchange.market.sync_time()  # our clock drifted outside recvWindow
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

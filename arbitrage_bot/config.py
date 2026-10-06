"""Configuration: a JSON file for settings, environment variables for secrets."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from .market import BPS, D

MODES = ("paper", "testnet", "live")

# Fiat + two stablecoins, like the original UAH/USDT/BUSD idea (all UAH and
# BUSD markets on Binance are suspended now), plus a few stablecoin-only ones.
DEFAULT_TRIANGLES = [
    ["USDT", "USDC", "TRY"],
    ["USDT", "FDUSD", "TRY"],
    ["USDT", "USDC", "BRL"],
    ["USDT", "USDC", "EUR"],
    ["USDT", "USDC", "MXN"],
    ["USDT", "USDC", "IDR"],
    ["USDT", "FDUSD", "USDC"],
    ["USDT", "USD1", "USDC"],
]

_DECIMAL_FIELDS = {
    "min_trade",
    "max_trade",
    "max_balance_fraction",
    "min_profit_bps",
    "min_profit_abs",
    "taker_fee_bps",
    "max_loss",
}


@dataclass
class Config:
    mode: str = "paper"
    home_asset: str = "USDT"
    triangles: List[List[str]] = field(default_factory=lambda: [list(t) for t in DEFAULT_TRIANGLES])

    # Sizing, in the home asset.
    min_trade: Decimal = Decimal("20")
    max_trade: Decimal = Decimal("200")
    max_balance_fraction: Decimal = Decimal("0.9")  # never commit more than this share of the home balance
    size_points: int = 12  # trade sizes tried between min_trade and max_trade

    # Only trade when the depth-checked, after-fee profit clears BOTH of these.
    min_profit_bps: Decimal = Decimal("2")
    min_profit_abs: Decimal = Decimal("0.01")

    # Fees. In testnet/live mode (or paper mode with API keys set) the bot asks
    # Binance for your account's real taker fee per symbol instead.
    taker_fee_bps: Decimal = Decimal("10")
    fee_overrides_bps: Dict[str, Decimal] = field(default_factory=dict)
    use_account_fees: bool = True

    complete_with_market: bool = True
    depth_limit: int = 20
    poll_interval_sec: float = 1.0
    cooldown_sec: float = 2.0
    max_depth_checks_per_scan: int = 3

    # Risk limits - the bot stops when any of these is hit.
    max_cycles: int = 0  # 0 = unlimited
    max_loss: Decimal = Decimal("5")  # cumulative realised loss in the home asset
    max_consecutive_losses: int = 5
    max_consecutive_errors: int = 5

    paper_balances: Dict[str, Decimal] = field(default_factory=lambda: {"USDT": Decimal("1000")})
    paper_depletion_sec: float = 60.0  # how long paper mode remembers liquidity it already took
    market_data_url: Optional[str] = None  # override; paper mode defaults to data-api.binance.vision

    stats_interval_sec: float = 60.0
    log_file: str = "arbitrage.log"
    trade_log: str = "trades.jsonl"

    def fee_rate(self, symbol: str, account_rate: Optional[Decimal] = None) -> Decimal:
        if symbol in self.fee_overrides_bps:
            return self.fee_overrides_bps[symbol] / BPS
        if account_rate is not None:
            return account_rate
        return self.taker_fee_bps / BPS

    def validate(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {self.mode!r}")
        if self.min_trade <= 0 or self.max_trade < self.min_trade:
            raise ValueError("need 0 < min_trade <= max_trade")
        if not 0 < self.max_balance_fraction <= 1:
            raise ValueError("max_balance_fraction must be in (0, 1]")
        if not self.triangles:
            raise ValueError("no triangles configured")
        for t in self.triangles:
            if len(t) != 3 or self.home_asset not in t or len(set(t)) != 3:
                raise ValueError(f"triangle {t} must be 3 different assets including {self.home_asset}")


def load_config(path: Optional[str]) -> Config:
    raw: dict = {}
    if path:
        with open(path) as fh:
            raw = json.load(fh)
    known = {f.name for f in fields(Config)}
    unknown = set(raw) - known - {"_comment"}
    if unknown:
        raise ValueError(f"unknown config keys: {sorted(unknown)}")
    values = {k: v for k, v in raw.items() if k in known}
    for key in _DECIMAL_FIELDS & set(values):
        values[key] = D(values[key])
    if "fee_overrides_bps" in values:
        values["fee_overrides_bps"] = {s: D(v) for s, v in values["fee_overrides_bps"].items()}
    if "paper_balances" in values:
        values["paper_balances"] = {a: D(v) for a, v in values["paper_balances"].items()}
    return Config(**values)


def api_keys(mode: str) -> Tuple[Optional[str], Optional[str]]:
    """Keys come from the environment, never from a file in the repo."""
    if mode == "testnet":
        return os.environ.get("BINANCE_TESTNET_API_KEY"), os.environ.get("BINANCE_TESTNET_API_SECRET")
    return os.environ.get("BINANCE_API_KEY"), os.environ.get("BINANCE_API_SECRET")

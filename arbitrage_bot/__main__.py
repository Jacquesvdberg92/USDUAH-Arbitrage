"""Command line entry point: ``python -m arbitrage_bot``."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from decimal import Decimal
from typing import Dict, List, Optional

from .bot import ArbitrageBot
from .config import MODES, Config, api_keys, load_config
from .exchange import (
    LIVE_URL,
    PUBLIC_DATA_URL,
    TESTNET_URL,
    BinanceAPIError,
    BinanceClient,
    LiveExchange,
    PaperExchange,
)
from .executor import TRADING_ERRORS, CycleExecutor
from .market import BPS, fmt
from .triangle import MissingMarketError, PairIndex, build_cycles

log = logging.getLogger("arbitrage_bot")


def setup_logging(log_file: str, verbose: bool) -> None:
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=handlers,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def resolve_fees(config: Config, symbols: List[str], fee_client: Optional[BinanceClient]) -> Dict[str, Decimal]:
    account: Dict[str, Decimal] = {}
    if fee_client is not None and config.use_account_fees:
        try:
            for symbol in symbols:
                account[symbol] = fee_client.taker_commission(symbol)
        except TRADING_ERRORS as err:
            log.warning("could not read account fees (%s); using taker_fee_bps=%s", err, config.taker_fee_bps)
            account = {}
    fees = {s: config.fee_rate(s, account.get(s)) for s in symbols}
    source = "account" if account else "config"
    log.info("taker fees (%s): %s", source, ", ".join(f"{s} {fmt(fees[s] * BPS)}bps" for s in symbols))
    return fees


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m arbitrage_bot",
        description="Triangular arbitrage bot for Binance spot (paper / testnet / live).",
    )
    parser.add_argument("-c", "--config", help="JSON config file (default: config.json if it exists)")
    parser.add_argument("--mode", choices=MODES, help="override the mode from the config file")
    parser.add_argument("--once", action="store_true", help="print the current edge of every cycle and exit; never trades")
    parser.add_argument("--confirm-live", action="store_true", help="required to trade real money in live mode")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging (every depth check)")
    args = parser.parse_args(argv)

    config_path = args.config or ("config.json" if os.path.exists("config.json") else None)
    config = load_config(config_path)
    if args.mode:
        config.mode = args.mode
    config.validate()
    setup_logging(config.log_file, args.verbose)

    if config.mode == "live" and not args.once and not args.confirm_live:
        parser.error("live mode places real orders with real money - add --confirm-live if you mean it")

    key, secret = api_keys(config.mode)
    if config.mode == "paper":
        market = BinanceClient(config.market_data_url or PUBLIC_DATA_URL)
        # Optional: read your real fee tier, read-only, if live keys are in the environment.
        fee_client = BinanceClient(LIVE_URL, key, secret) if key and secret else None
    else:
        if not (key and secret):
            prefix = "BINANCE_TESTNET" if config.mode == "testnet" else "BINANCE"
            parser.error(f"{config.mode} mode needs {prefix}_API_KEY and {prefix}_API_SECRET in the environment")
        market = BinanceClient(TESTNET_URL if config.mode == "testnet" else LIVE_URL, key, secret)
        fee_client = market

    try:
        if config.mode != "paper":
            market.sync_time()
        index = PairIndex(market.exchange_info())
    except TRADING_ERRORS as err:
        if isinstance(err, BinanceAPIError) and err.status == 451:
            log.error("Binance refuses connections from this location (HTTP 451)")
        log.error("could not load exchange info: %s", err)
        return 1

    cycles = []
    for triangle in config.triangles:
        try:
            cycles += build_cycles(config.home_asset, triangle, index)
        except MissingMarketError as err:
            log.warning("skipping %s: %s", "/".join(triangle), err)
    if not cycles:
        log.error("none of the configured triangles can be traded")
        return 1
    symbols = sorted({s for c in cycles for s in c.symbols})
    fees = resolve_fees(config, symbols, fee_client)

    if config.mode == "paper":
        exchange = PaperExchange(market, index.by_symbol, fees, config.paper_balances, config.depth_limit, config.paper_depletion_sec)
    else:
        exchange = LiveExchange(market, index.by_symbol, config.depth_limit)
    executor = CycleExecutor(exchange, index, config.home_asset, config.complete_with_market)
    bot = ArbitrageBot(config, exchange, executor, cycles, fees)

    if args.once:
        print(bot.report())
        return 0

    if config.trade_log:
        try:
            open(config.trade_log, "a").close()  # fail now, not after the first real trade
        except OSError as err:
            log.error("can't write trade_log %r: %s", config.trade_log, err)
            return 1

    log.info("MODE: %s%s", config.mode.upper(), "  *** REAL MONEY ***" if config.mode == "live" else "")
    return bot.run()


if __name__ == "__main__":
    sys.exit(main())

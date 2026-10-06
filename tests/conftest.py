import os
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arbitrage_bot.config import Config  # noqa: E402
from arbitrage_bot.exchange import PaperExchange  # noqa: E402
from arbitrage_bot.market import OrderBook, SymbolRules  # noqa: E402
from arbitrage_bot.triangle import PairIndex, build_cycles  # noqa: E402


def make_rules(symbol, base, quote, step="0.01", tick="0.0001", min_qty="0.01", min_notional="5", status="TRADING"):
    return SymbolRules.from_exchange_info(
        {
            "symbol": symbol,
            "status": status,
            "baseAsset": base,
            "quoteAsset": quote,
            "quoteAssetPrecision": 8,
            "quoteOrderQtyMarketAllowed": True,
            "filters": [
                {"filterType": "PRICE_FILTER", "minPrice": "0.0001", "maxPrice": "1000", "tickSize": tick},
                {"filterType": "LOT_SIZE", "minQty": min_qty, "maxQty": "1000000", "stepSize": step},
                {"filterType": "NOTIONAL", "minNotional": min_notional, "applyMinToMarket": True},
            ],
        }
    )


def make_book(symbol, bids, asks):
    as_levels = lambda levels: tuple((Decimal(str(p)), Decimal(str(q))) for p, q in levels)  # noqa: E731
    return OrderBook(symbol, as_levels(bids), as_levels(asks))


class FakeMarket:
    """Stands in for BinanceClient: serves whatever books the test sets."""

    def __init__(self, books, rules=()):
        self.books = dict(books)
        self.rules = list(rules)
        self.depth_calls = []
        self.ticker_calls = 0

    def order_book(self, symbol, limit=20):
        self.depth_calls.append(symbol)
        return self.books[symbol]

    def book_tickers(self, symbols):
        self.ticker_calls += 1
        return {s: self.books[s].ticker() for s in symbols}

    def exchange_info(self):
        return self.rules


# A triangle with a +44.5 bps gross edge on USDT > USDC > EUR > USDT:
#   1 / 1.0000 (buy USDC) / 1.1001 (buy EUR with USDC) * 1.1050 (sell EUR for USDT)
EUR_RULES = [
    make_rules("USDCUSDT", "USDC", "USDT"),
    make_rules("EURUSDC", "EUR", "USDC"),
    make_rules("EURUSDT", "EUR", "USDT"),
]


def profitable_books():
    return {
        "USDCUSDT": make_book("USDCUSDT", [(0.9999, 5000)], [(1.0000, 5000)]),
        "EURUSDC": make_book("EURUSDC", [(1.1000, 5000)], [(1.1001, 5000)]),
        "EURUSDT": make_book("EURUSDT", [(1.1050, 5000)], [(1.1051, 5000)]),
    }


@pytest.fixture
def index():
    return PairIndex(EUR_RULES)


@pytest.fixture
def rules(index):
    return index.by_symbol


@pytest.fixture
def cycles(index):
    return build_cycles("USDT", ["USDT", "USDC", "EUR"], index)


@pytest.fixture
def market():
    return FakeMarket(profitable_books(), EUR_RULES)


@pytest.fixture
def fees():
    return {s: Decimal("0.001") for s in ("USDCUSDT", "EURUSDC", "EURUSDT")}


@pytest.fixture
def paper(market, rules, fees):
    return PaperExchange(market, rules, fees, {"USDT": Decimal("1000")})


@pytest.fixture
def config(tmp_path):
    return Config(
        triangles=[["USDT", "USDC", "EUR"]],
        min_trade=Decimal("10"),
        max_trade=Decimal("100"),
        trade_log=str(tmp_path / "trades.jsonl"),
        log_file="",
        poll_interval_sec=0,
        cooldown_sec=0,
    )

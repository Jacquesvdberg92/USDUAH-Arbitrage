from decimal import Decimal as D

from arbitrage_bot.market import SymbolRules, ceil_to_step, fill_buy, fill_sell, floor_to_step, fmt, max_buy_qty

from conftest import make_book

# Copied from a real exchangeInfo response.
USDTTRY = {
    "symbol": "USDTTRY",
    "status": "TRADING",
    "baseAsset": "USDT",
    "quoteAsset": "TRY",
    "quoteAssetPrecision": 8,
    "quoteOrderQtyMarketAllowed": True,
    "filters": [
        {"filterType": "PRICE_FILTER", "minPrice": "34.50000000", "maxPrice": "51.80000000", "tickSize": "0.01000000"},
        {"filterType": "LOT_SIZE", "minQty": "1.00000000", "maxQty": "92141578.00000000", "stepSize": "1.00000000"},
        {"filterType": "MARKET_LOT_SIZE", "minQty": "0.00000000", "maxQty": "11808.79338842", "stepSize": "0.00000000"},
        {"filterType": "NOTIONAL", "minNotional": "10.00000000", "applyMinToMarket": True, "maxNotional": "90000000.00000000"},
    ],
}


def test_step_rounding_and_formatting():
    assert floor_to_step(D("19.996"), D("1")) == 19
    assert floor_to_step(D("14.5454"), D("0.01")) == D("14.54")
    assert ceil_to_step(D("1.00001"), D("0.0001")) == D("1.0001")
    assert fmt(D("100.00000000")) == "100"
    assert fmt(D("0.00010000")) == "0.0001"
    assert fmt(D("1E+2")) == "100"


def test_rules_from_exchange_info():
    r = SymbolRules.from_exchange_info(USDTTRY)
    assert (r.base, r.quote, r.trading) == ("USDT", "TRY", True)
    assert r.step_size == 1 and r.tick_size == D("0.01") and r.min_notional == 10
    assert r.market_max_qty == D("11808.79338842")
    assert r.order_error(D("20"), D("49.10")) is None
    assert r.order_error(D("0"), D("49.10")) == "quantity is zero"
    assert "minQty" in r.order_error(D("0.5"), D("49.10"))
    assert "multiple" in r.order_error(D("20.5"), D("49.10"))
    assert "PRICE_FILTER" in r.order_error(D("20"), D("49.105"))
    assert "maxQty" in r.order_error(D("20000"), D("49.10"), market=True)
    tiny = SymbolRules.from_exchange_info(dict(USDTTRY, filters=USDTTRY["filters"][:2] + [
        {"filterType": "NOTIONAL", "minNotional": "100", "applyMinToMarket": False}]))
    assert "NOTIONAL" in tiny.order_error(D("1"), D("49.10"))
    assert tiny.order_error(D("1"), D("49.10"), market=True) is None  # min notional not applied to market orders


def test_max_buy_qty_walks_levels_and_rounds_down():
    asks = make_book("X", [], [(1.0, 10), (1.1, 10)]).asks
    qty, budget_bound = max_buy_qty(asks, D("15"), D("0.01"))
    assert qty == D("14.54") and budget_bound  # 10 @ 1.0 + 4.545 @ 1.1, floored
    assert fill_buy(asks, qty).quote_qty <= 15

    qty, budget_bound = max_buy_qty(asks, D("100"), D("0.01"))
    assert qty == 20 and not budget_bound  # the book ran out first

    qty, _ = max_buy_qty(asks, D("100"), D("0.01"), limit=D("1.0"))
    assert qty == 10


def test_fill_respects_limit_and_depth():
    book = make_book("X", [(2.0, 5), (1.9, 5)], [(2.1, 5), (2.2, 5)])
    sell = fill_sell(book.bids, D("8"))
    assert (sell.base_qty, sell.quote_qty, sell.worst_price) == (8, D("15.7"), D("1.9"))
    assert fill_sell(book.bids, D("8"), limit=D("2.0")).base_qty == 5
    assert fill_buy(book.asks, D("20")).base_qty == 10
    assert fill_buy(book.asks, D("3")).avg_price == D("2.1")

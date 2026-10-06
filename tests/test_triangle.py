from decimal import Decimal as D

import pytest

from arbitrage_bot.market import BUY, SELL, ZERO
from arbitrage_bot.triangle import (
    MissingMarketError,
    PairIndex,
    build_cycles,
    find_best_plan,
    plan_cycle,
    quick_edge_bps,
    size_grid,
)

from conftest import EUR_RULES, make_book, make_rules, profitable_books


def test_build_cycles_picks_side_from_symbol_orientation():
    index = PairIndex([make_rules("USDCUSDT", "USDC", "USDT"), make_rules("USDCTRY", "USDC", "TRY"), make_rules("USDTTRY", "USDT", "TRY")])
    forward, backward = build_cycles("USDT", ["USDT", "USDC", "TRY"], index)
    assert forward.path == "USDT > USDC > TRY > USDT"
    assert [(l.symbol, l.side) for l in forward.legs] == [("USDCUSDT", BUY), ("USDCTRY", SELL), ("USDTTRY", BUY)]
    assert backward.path == "USDT > TRY > USDC > USDT"
    assert [(l.symbol, l.side) for l in backward.legs] == [("USDTTRY", SELL), ("USDCTRY", BUY), ("USDCUSDT", SELL)]


def test_suspended_markets_are_reported():
    # The original bot's triangle: all three markets are in BREAK on Binance today.
    index = PairIndex([
        make_rules("USDTUAH", "USDT", "UAH", status="BREAK"),
        make_rules("BUSDUAH", "BUSD", "UAH", status="BREAK"),
        make_rules("BUSDUSDT", "BUSD", "USDT", status="BREAK"),
    ])
    with pytest.raises(MissingMarketError, match="USDTUAH exists but its status is BREAK"):
        build_cycles("UAH", ["UAH", "USDT", "BUSD"], index)
    with pytest.raises(ValueError):
        build_cycles("USDT", ["USDC", "EUR", "BRL"], index)


def test_plan_matches_hand_calculation(cycles, rules, fees):
    plan = plan_cycle(cycles[0], D("100"), profitable_books(), rules, fees)
    leg1, leg2, leg3 = plan.legs
    assert (leg1.spent, leg1.received) == (D("100"), D("99.9"))  # 100 USDC @ 1.0, minus 0.1% fee
    assert (leg2.base_qty, leg2.spent) == (D("90.80"), D("99.889080"))  # 99.9 / 1.1001 floored to 0.01
    assert leg2.received == D("90.80") * D("0.999")
    assert (leg3.base_qty, leg3.quote_qty) == (D("90.70"), D("100.2235"))
    assert leg3.received == D("100.2235") * D("0.999")
    assert plan.dust_value > 0  # ~0.011 USDC + ~0.009 EUR of rounding leftovers
    assert plan.profit == leg3.received + plan.dust_value - 100
    assert D("14") < plan.profit_bps < D("15")


def test_fees_and_direction_matter(cycles, rules, fees):
    books = profitable_books()
    no_fee = {s: ZERO for s in fees}
    high_fee = {s: D("0.002") for s in fees}
    assert plan_cycle(cycles[0], D("100"), books, rules, no_fee).profit_bps > 44
    assert plan_cycle(cycles[0], D("100"), books, rules, high_fee).profit < 0
    assert plan_cycle(cycles[1], D("100"), books, rules, fees).profit < 0  # the other direction loses


def test_top_of_book_edge_is_an_upper_bound_for_profitable_cycles(cycles, rules, fees):
    books = profitable_books()
    tickers = {s: b.ticker() for s, b in books.items()}
    edge = quick_edge_bps(cycles[0], tickers, fees)
    assert D("14") < edge < D("15")
    for size in ("10", "55", "100", "1000"):
        assert 0 < plan_cycle(cycles[0], D(size), books, rules, fees).profit_bps <= edge
    # Losing cycles stay below any positive threshold after depth checks too.
    assert quick_edge_bps(cycles[1], tickers, fees) < 0
    assert plan_cycle(cycles[1], D("100"), books, rules, fees).profit_bps < 0


def test_best_size_stops_where_the_book_gets_thin(cycles, rules, fees):
    books = profitable_books()
    books["EURUSDT"] = make_book("EURUSDT", [(1.1050, 50), (1.0900, 5000)], [(1.1051, 5000)])
    plan = find_best_plan(cycles[0], books, rules, fees, D("10"), D("100"), D("2"))
    assert D("40") < plan.spent < D("60")
    assert plan.profit_bps >= 2
    # Too big a trade would eat into the 1.09 level and lose money.
    assert plan_cycle(cycles[0], D("100"), books, rules, fees).profit < plan.profit


def test_middle_leg_must_be_fully_absorbed(cycles, rules, fees):
    books = profitable_books()
    books["EURUSDC"] = make_book("EURUSDC", [(1.1000, 5000)], [(1.1001, 10)])
    assert plan_cycle(cycles[0], D("100"), books, rules, fees) is None
    assert plan_cycle(cycles[0], D("10"), books, rules, fees) is not None


def test_whole_unit_lot_sizes_leave_dust_that_is_valued(fees):
    index = PairIndex([make_rules(r.symbol, r.base, r.quote, step="1", min_qty="1") for r in EUR_RULES])
    cycle = build_cycles("USDT", ["USDT", "USDC", "EUR"], index)[0]
    plan = plan_cycle(cycle, D("100"), profitable_books(), index.by_symbol, fees)
    assert plan.legs[1].base_qty == 90  # 99.9 USDC buys 90.8 EUR -> 90
    assert plan.legs[1].leftover > D("0.8")  # ~0.89 USDC left behind
    assert plan.dust_value > D("0.8")
    assert plan.profit == plan.received + plan.dust_value - plan.spent


def test_size_grid():
    grid = size_grid(D("10"), D("1000"), 3)
    assert grid == [D("10"), D("100"), D("1000")]
    assert size_grid(D("10"), D("5"), 5) == []


def test_buy_is_sized_to_fit_the_budget_at_its_limit_price(cycles, rules, fees):
    books = profitable_books()
    books["USDCUSDT"] = make_book("USDCUSDT", [(0.9999, 5000)], [(0.9990, 60), (1.0000, 5000)])
    leg1 = plan_cycle(cycles[0], D("100"), books, rules, fees).legs[0]
    assert leg1.limit_price == 1 and leg1.base_qty * leg1.limit_price <= 100  # what Binance reserves

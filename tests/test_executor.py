from decimal import Decimal as D

import pytest

from arbitrage_bot.exchange import BinanceAPIError, PaperExchange
from arbitrage_bot.executor import CycleExecutor, StuckPositionError
from arbitrage_bot.triangle import plan_cycle

from conftest import make_book, profitable_books


class FlakyPaper(PaperExchange):
    """Paper exchange that rejects orders on chosen symbols."""

    fail_on = ()
    fail_after = None  # reject every order after this many
    placed = 0

    def _place(self, params):
        self.placed += 1
        if params["symbol"] in self.fail_on or (self.fail_after is not None and self.placed > self.fail_after):
            raise BinanceAPIError(400, -2010, "Order rejected.")  # a definite rejection
        return super()._place(params)


def plan_for(cycles, rules, fees, size="100"):
    return plan_cycle(cycles[0], D(size), profitable_books(), rules, fees)


def test_completed_cycle_matches_the_plan(paper, index, cycles, rules, fees):
    plan = plan_for(cycles, rules, fees)
    result = CycleExecutor(paper, index, "USDT").execute(plan)

    assert result.status == "completed"
    assert result.home_spent == plan.spent and result.home_received == plan.received
    assert result.pnl > 0
    assert set(result.dust) == {"USDC", "EUR"}
    balances = paper.balances()
    assert balances["USDT"] == 1000 + result.pnl
    assert balances["USDC"] == result.dust["USDC"] and balances["EUR"] == result.dust["EUR"]
    assert [o.order_type for leg in result.legs for o in leg.orders] == ["LIMIT"] * 3


def test_no_fill_when_the_opportunity_is_gone(paper, market, index, cycles, rules, fees):
    plan = plan_for(cycles, rules, fees)
    market.books["USDCUSDT"] = make_book("USDCUSDT", [(0.9999, 5000)], [(1.0001, 5000)])  # someone took the 1.0000 asks
    result = CycleExecutor(paper, index, "USDT").execute(plan)
    assert result.status == "no_fill" and result.pnl == 0
    assert paper.balances() == {"USDT": 1000}


def test_later_legs_fall_back_to_market_when_the_book_moves(paper, market, index, cycles, rules, fees):
    plan = plan_for(cycles, rules, fees)
    market.books["EURUSDT"] = make_book("EURUSDT", [(1.1040, 5000)], [(1.1041, 5000)])  # bid dropped after planning
    result = CycleExecutor(paper, index, "USDT").execute(plan)
    assert result.status == "completed"
    last_leg = result.legs[-1]
    assert [(o.order_type, o.executed_qty > 0) for o in last_leg.orders] == [("LIMIT", False), ("MARKET", True)]
    assert 0 < result.pnl < plan.profit  # still completed, at the worse price


def test_without_market_fallback_the_remainder_is_left_as_leftover(paper, market, index, cycles, rules, fees):
    plan = plan_for(cycles, rules, fees)
    market.books["EURUSDT"] = make_book("EURUSDT", [(1.1040, 5000)], [(1.1041, 5000)])
    result = CycleExecutor(paper, index, "USDT", complete_with_market=False).execute(plan)
    # Leg 3 sold nothing, so the executor unwinds the EUR straight back to USDT.
    assert result.status == "unwound"
    assert paper.balances().get("EUR", 0) < 1


def test_failed_leg_is_unwound_to_home(market, index, cycles, rules, fees):
    paper = FlakyPaper(market, rules, fees, {"USDT": D("1000")})
    paper.fail_on = ("EURUSDC",)
    result = CycleExecutor(paper, index, "USDT").execute(plan_for(cycles, rules, fees))
    assert result.status == "unwound" and "EURUSDC" in result.error
    balances = paper.balances()
    assert balances["USDC"] < 1  # sold back via USDCUSDT
    assert D("999.7") < balances["USDT"] < 1000  # lost only spread and fees


def test_unwind_failure_is_a_stuck_position(market, index, cycles, rules, fees):
    paper = FlakyPaper(market, rules, fees, {"USDT": D("1000")})
    paper.fail_after = 1  # leg 1 fills, then the exchange rejects everything
    with pytest.raises(StuckPositionError) as err:
        CycleExecutor(paper, index, "USDT").execute(plan_for(cycles, rules, fees))
    assert err.value.asset == "USDC" and err.value.amount == D("99.9")


def test_tiny_partial_entry_is_kept_as_dust_not_a_stuck_position(paper, market, index, cycles, rules, fees):
    plan = plan_for(cycles, rules, fees)
    market.books["USDCUSDT"] = make_book("USDCUSDT", [(0.9999, 5000)], [(1.0000, 3), (1.0001, 5000)])
    result = CycleExecutor(paper, index, "USDT").execute(plan)
    # 3 USDC filled - too little for leg 2 (5 USDC minimum) and too little to sell back.
    assert result.status == "unwound"
    assert result.home_spent == 3 and result.home_received == 0
    assert result.dust == {"USDC": D("2.997")}


def test_paper_market_fallback_walks_past_what_the_ioc_took(paper, market, index, cycles, rules, fees):
    plan = plan_for(cycles, rules, fees)  # sells 90.70 EUR at 1.1050
    market.books["EURUSDT"] = make_book("EURUSDT", [(1.1050, 50), (1.1040, 5000)], [(1.1051, 5000)])
    result = CycleExecutor(paper, index, "USDT").execute(plan)
    ioc, fallback = result.legs[-1].orders
    assert (ioc.executed_qty, ioc.quote_qty) == (D("50"), D("55.25"))
    assert (fallback.executed_qty, fallback.quote_qty) == (D("40.70"), D("40.70") * D("1.1040"))

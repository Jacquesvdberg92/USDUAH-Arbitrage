"""Failure handling: unknown order outcomes, stuck positions, Ctrl+C, error streaks, exit codes."""

import json
import os
import re
import signal
from decimal import Decimal as D

import pytest
import requests

from arbitrage_bot import __main__ as cli
from arbitrage_bot.bot import ArbitrageBot
from arbitrage_bot.exchange import BinanceAPIError, LiveExchange, OrderResult, OrderStatusUnknown, PaperExchange
from arbitrage_bot.executor import CycleExecutor, CycleResult
from arbitrage_bot.triangle import plan_cycle

from conftest import EUR_RULES, FakeMarket, make_book, make_rules, profitable_books


class FakeLiveClient:
    """Stands in for BinanceClient in live mode: really executes orders (on a paper
    exchange) but can lose the HTTP response, like a timeout after the POST was sent."""

    def __init__(self, paper, lose_response_for=(), lookup_fails=False):
        self.paper = paper
        self.market = paper.market
        self.lose = list(lose_response_for)
        self.lookup_fails = lookup_fails
        self.orders = {}
        self.sent = []

    def new_order(self, params):
        self.sent.append(dict(params))
        result = self.paper._place({k: v for k, v in params.items() if k != "newClientOrderId"})
        raw = dict(result.raw, clientOrderId=params["newClientOrderId"])
        self.orders[params["newClientOrderId"]] = raw
        for key in (params["symbol"], (params["symbol"], params["type"])):
            if key in self.lose:
                self.lose.remove(key)
                raise requests.ReadTimeout("read timed out")
        return OrderResult.from_api(raw)

    def get_order(self, symbol, client_order_id):
        if self.lookup_fails:
            raise requests.ConnectionError("network down")
        if client_order_id not in self.orders:
            raise BinanceAPIError(400, -2013, "Order does not exist.")
        return {k: v for k, v in self.orders[client_order_id].items() if k != "fills"}

    def my_trades(self, symbol, order_id):
        raw = next(o for o in self.orders.values() if o["orderId"] == order_id)
        return [dict(f) for f in raw["fills"]]

    def balances(self):
        return self.paper.balances()

    def order_book(self, symbol, limit=20):
        return self.market.order_book(symbol, limit)

    def book_tickers(self, symbols):
        return self.market.book_tickers(symbols)


def live_exchange(client, rules):
    return LiveExchange(client, rules, sleep=lambda s: None)


def plan_for(cycles, rules, fees):
    return plan_cycle(cycles[0], D("100"), profitable_books(), rules, fees)


# -- unknown order outcomes ----------------------------------------------------

def test_every_live_order_carries_a_valid_client_id(paper, index, cycles, rules, fees):
    client = FakeLiveClient(paper)
    CycleExecutor(live_exchange(client, rules), index, "USDT").execute(plan_for(cycles, rules, fees))
    ids = [p["newClientOrderId"] for p in client.sent]
    assert len(ids) == 3 and len(set(ids)) == 3
    assert all(re.fullmatch(r"[.A-Z:/a-z0-9_-]{1,36}", i) for i in ids)


def test_lost_response_is_looked_up_and_the_cycle_completes(paper, index, cycles, rules, fees):
    client = FakeLiveClient(paper, lose_response_for=["EURUSDC"])
    result = CycleExecutor(live_exchange(client, rules), index, "USDT").execute(plan_for(cycles, rules, fees))
    assert result.status == "completed" and result.pnl > 0  # no bogus unwind of USDC we no longer hold
    leg2 = result.legs[1].orders[0]
    assert leg2.executed_qty == D("90.80") and leg2.commissions == {"EUR": D("90.80") * D("0.001")}


def test_definite_rejection_is_not_looked_up(paper, rules):
    class Rejecting(FakeLiveClient):
        def new_order(self, params):
            raise BinanceAPIError(400, -2010, "Account has insufficient balance for requested action.")

    client = Rejecting(paper)
    client.get_order = lambda *a: pytest.fail("a 4xx rejection must not be reconciled")
    with pytest.raises(BinanceAPIError):
        live_exchange(client, rules).market_sell("USDCUSDT", D("10"))


def test_server_error_with_no_order_on_record_is_a_rejection(paper, rules):
    class ServerError(FakeLiveClient):
        def new_order(self, params):
            raise BinanceAPIError(503, -1001, "Internal error")  # 5xx: outcome unknown...

    with pytest.raises(BinanceAPIError) as err:
        live_exchange(ServerError(paper), rules).market_sell("USDCUSDT", D("10"))
    assert err.value.code == -2013  # ...but Binance never heard of it, so it wasn't placed


def test_unresolvable_order_stops_the_bot_and_is_recorded(config, paper, index, cycles, rules, fees):
    client = FakeLiveClient(paper, lose_response_for=["EURUSDC"], lookup_fails=True)
    exchange = live_exchange(client, rules)
    config.max_cycles = 5
    bot = ArbitrageBot(config, exchange, CycleExecutor(exchange, index, "USDT"), cycles, fees, sleep=lambda s: None)
    assert bot.run() == 2
    row = json.loads(open(config.trade_log).read().splitlines()[-1])
    assert row["status"] == "unknown" and "may have executed" in row["error"]
    assert len(client.sent) == 2  # stopped right there: no unwind, no retries


# -- stuck positions, Ctrl+C, error streaks ------------------------------------

class RejectAfter(PaperExchange):
    fail_after = None
    placed = 0

    def _place(self, params):
        self.placed += 1
        if self.fail_after is not None and self.placed > self.fail_after:
            raise BinanceAPIError(400, -2010, "Order rejected.")
        return super()._place(params)


def test_stuck_position_is_recorded_and_exits_2(config, market, index, cycles, rules, fees):
    paper = RejectAfter(market, rules, fees, {"USDT": D("1000")})
    paper.fail_after = 1
    bot = ArbitrageBot(config, paper, CycleExecutor(paper, index, "USDT"), cycles, fees, sleep=lambda s: None)
    assert bot.run() == 2
    row = json.loads(open(config.trade_log).read().splitlines()[-1])
    assert row["status"] == "stuck" and row["dust"] == {"USDC": "99.9"}
    assert bot.stats.cycles == 1 and bot.dust == {"USDC": D("99.9")}  # the open position is in the books
    assert D("-1") < bot.equity_change() < 0


def test_ctrl_c_mid_cycle_finishes_the_cycle_first(config, market, index, cycles, rules, fees):
    class InterruptOnLeg2(PaperExchange):
        def _place(self, params):
            if params["symbol"] == "EURUSDC":
                os.kill(os.getpid(), signal.SIGINT)
            return super()._place(params)

    paper = InterruptOnLeg2(market, rules, fees, {"USDT": D("1000")})
    bot = ArbitrageBot(config, paper, CycleExecutor(paper, index, "USDT"), cycles, fees, sleep=lambda s: None)
    assert bot.run() == 0
    assert bot.stats.cycles == 1 and bot.stats.wins == 1
    assert set(paper.balances()) - {"USDT"} <= {"USDC", "EUR"} and paper.balances()["USDT"] > 1000
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_rejected_orders_stop_the_bot_even_between_quiet_scans(config, market, index, cycles, rules, fees):
    paper = RejectAfter(market, rules, fees, {"USDT": D("1000")})
    paper.fail_after = 0
    flat = {**profitable_books(), "EURUSDT": make_book("EURUSDT", [(1.0999, 5000)], [(1.1000, 5000)])}
    scans = []

    def tickers(symbols):
        scans.append(1)
        market.books = profitable_books() if len(scans) % 2 else flat  # opportunity on every other scan
        return FakeMarket.book_tickers(market, symbols)

    paper.book_tickers = tickers
    config.max_consecutive_errors = 3
    bot = ArbitrageBot(config, paper, CycleExecutor(paper, index, "USDT"), cycles, fees, sleep=lambda s: None)
    assert bot.run() == 2
    assert bot.consecutive_order_errors == 3 and len(scans) < 10


def test_bad_api_key_stops_immediately(config, market, index, cycles, rules, fees):
    class BadKey(PaperExchange):
        def _place(self, params):
            raise BinanceAPIError(401, -2015, "Invalid API-key, IP, or permissions for action.")

    paper = BadKey(market, rules, fees, {"USDT": D("1000")})
    bot = ArbitrageBot(config, paper, CycleExecutor(paper, index, "USDT"), cycles, fees, sleep=lambda s: None)
    assert bot.run() == 2 and bot.consecutive_order_errors == 1


def test_failed_time_resync_is_just_another_error(config, paper, index, cycles, fees):
    def skewed(symbols):
        raise BinanceAPIError(400, -1021, "Timestamp for this request is outside of the recvWindow.")

    def resync():
        raise requests.ConnectionError("still down")

    paper.book_tickers = skewed
    paper.market.sync_time = resync
    config.max_consecutive_errors = 3
    bot = ArbitrageBot(config, paper, CycleExecutor(paper, index, "USDT"), cycles, fees, sleep=lambda s: None)
    assert bot.run() == 2 and bot.stats.errors == 3


# -- fees paid in BNB ----------------------------------------------------------

def test_fees_paid_in_bnb_count_against_pnl_and_limits(config, index, cycles, rules, fees):
    rules = dict(rules, BNBUSDT=make_rules("BNBUSDT", "BNB", "USDT"))
    books = dict(profitable_books(), BNBUSDT=make_book("BNBUSDT", [(600, 100)], [(600.1, 100)]))
    market = FakeMarket(books)
    from arbitrage_bot.triangle import PairIndex

    index = PairIndex(list(rules.values()))
    paper = PaperExchange(market, rules, fees, {"USDT": D("1000")})
    config.max_loss = D("0.5")
    bot = ArbitrageBot(config, paper, CycleExecutor(paper, index, "USDT"), cycles, fees, sleep=lambda s: None)
    result = CycleResult(plan_for(cycles, rules, fees), "completed", home_spent=D("100"), home_received=D("100.1"),
                         other_fees={"BNB": D("0.001")})  # 0.6 USDT of BNB: more than the 0.1 gained
    with pytest.raises(Exception, match="loss limit"):
        bot._record(result)
    assert bot.stats.fees_value == D("0.6") and bot.stats.losses == 1
    assert bot.equity_change() == D("0.1") - D("0.6")


# -- startup checks --------------------------------------------------------------

def test_unwritable_trade_log_fails_at_startup(tmp_path, monkeypatch):
    market = FakeMarket(profitable_books(), EUR_RULES)
    monkeypatch.setattr(cli, "BinanceClient", lambda *a, **k: market)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps(
        {"triangles": [["USDT", "USDC", "EUR"]], "log_file": "", "trade_log": "missing-dir/trades.jsonl"}))
    assert cli.main([]) == 1


def test_trade_log_write_failure_does_not_crash(config, paper, index, cycles, fees, tmp_path):
    config.trade_log = str(tmp_path / "missing-dir" / "trades.jsonl")
    bot = ArbitrageBot(config, paper, CycleExecutor(paper, index, "USDT"), cycles, fees, sleep=lambda s: None)
    assert bot.step().status == "completed"


# -- follow-up review: interrupted / unknown cycles ---------------------------------

def test_ctrl_c_during_a_rejected_entry_stops_without_another_cycle(config, market, index, cycles, rules, fees):
    class InterruptThenReject(PaperExchange):
        sent = 0

        def _place(self, params):
            self.sent += 1
            if self.sent == 1:
                os.kill(os.getpid(), signal.SIGINT)
                raise BinanceAPIError(400, -2010, "Order rejected.")
            return super()._place(params)

    paper = InterruptThenReject(market, rules, fees, {"USDT": D("1000")})
    bot = ArbitrageBot(config, paper, CycleExecutor(paper, index, "USDT"), cycles, fees, sleep=lambda s: None)
    assert bot.run() == 0 and paper.sent == 1


def test_forced_exit_mid_cycle_keeps_the_open_position_in_the_books(config, market, index, cycles, rules, fees):
    class ForceQuitOnLeg2(PaperExchange):
        def _place(self, params):
            if params["symbol"] == "EURUSDC":
                os.kill(os.getpid(), signal.SIGINT)  # "finish the cycle first"...
                os.kill(os.getpid(), signal.SIGINT)  # ...no, quit now
            return super()._place(params)

    paper = ForceQuitOnLeg2(market, rules, fees, {"USDT": D("1000")})
    bot = ArbitrageBot(config, paper, CycleExecutor(paper, index, "USDT"), cycles, fees, sleep=lambda s: None)
    assert bot.run() == 2  # a human should look: a position is open
    row = json.loads(open(config.trade_log).read().splitlines()[-1])
    assert row["status"] == "interrupted" and row["dust"] == {"USDC": "99.9"} and row["error"] == "KeyboardInterrupt"
    assert (bot.stats.unresolved, bot.stats.wins, bot.stats.losses) == (1, 0, 0)
    assert D("-0.3") < bot.equity_change() < 0  # not -100: the USDC is still ours
    assert paper.balances()["USDC"] == D("99.9")


def test_unknown_entry_order_is_unresolved_not_a_win(config, paper, index, cycles, rules, fees):
    client = FakeLiveClient(paper, lose_response_for=["USDCUSDT"], lookup_fails=True)
    exchange = live_exchange(client, rules)
    bot = ArbitrageBot(config, exchange, CycleExecutor(exchange, index, "USDT"), cycles, fees, sleep=lambda s: None)
    assert bot.run() == 2
    assert (bot.stats.unresolved, bot.stats.wins, bot.stats.planned_pnl) == (1, 0, 0)


def test_unknown_market_fallback_keeps_the_ioc_fill(paper, market, index, cycles, rules, fees):
    plan = plan_for(cycles, rules, fees)
    market.books["EURUSDT"] = make_book("EURUSDT", [(1.1050, 50), (1.1040, 5000)], [(1.1051, 5000)])
    client = FakeLiveClient(paper, lose_response_for=[("EURUSDT", "MARKET")], lookup_fails=True)
    with pytest.raises(OrderStatusUnknown) as err:
        CycleExecutor(live_exchange(client, rules), index, "USDT").execute(plan)
    result = err.value.cycle_result
    assert result.status == "unknown"
    assert [o.order_type for leg in result.legs for o in leg.orders] == ["LIMIT", "LIMIT", "LIMIT"]
    assert result.home_received == D("55.25") * D("0.999")  # the IOC part of leg 3 is in the books
    # The EUR the fallback tried to sell may or may not still be there; USDC is leg 2's rounding leftover.
    assert result.dust == {"EUR": D("90.7092") - 50, "USDC": D("0.01092")}


def test_ctrl_c_during_post_trade_bookkeeping_still_books_the_sweep(config, market, index, cycles, rules, fees):
    class InterruptAfterSweep(PaperExchange):
        def _place(self, params):
            result = super()._place(params)
            if params["type"] == "MARKET" and params["symbol"] == "USDCUSDT":
                os.kill(os.getpid(), signal.SIGINT)  # the sweep order executed; Ctrl+C before it's booked
            return result

    paper = InterruptAfterSweep(market, rules, fees, {"USDT": D("1000"), "USDC": D("50")})
    bot = ArbitrageBot(config, paper, CycleExecutor(paper, index, "USDT"), cycles, fees, sleep=lambda s: None)
    bot.dust["USDC"] = D("50")  # leftovers from earlier cycles, big enough to sweep
    assert bot.run() == 0
    assert bot.stats.cycles == 1 and len(open(config.trade_log).read().splitlines()) == 1
    assert bot.dust["USDC"] < 1  # the sweep is in the books...
    assert bot.stats.pnl > 49  # ...and so are its proceeds


def test_failure_to_connect_is_a_definite_rejection():
    import urllib3
    from arbitrage_bot.exchange import is_definite_rejection

    session = requests.Session()
    session.trust_env = False  # no proxy: really try 127.0.0.1:1
    with pytest.raises(requests.ConnectionError) as refused:
        session.post("http://127.0.0.1:1/api/v3/order", timeout=2)
    assert is_definite_rejection(refused.value)  # connection refused: never sent
    assert is_definite_rejection(requests.ConnectTimeout())
    assert not is_definite_rejection(requests.ReadTimeout())  # sent, answer lost
    assert not is_definite_rejection(requests.ConnectionError(urllib3.exceptions.ProtocolError("reset")))
    assert not is_definite_rejection(BinanceAPIError(503, -1001, "Internal error"))
    assert not is_definite_rejection(BinanceAPIError(408, -1007, "Timeout waiting for response"))
    assert is_definite_rejection(BinanceAPIError(429, -1003, "Too many requests"))


def test_buy_sizing_terminates_on_decimal_rounding_edge():
    from arbitrage_bot.triangle import Leg, simulate_leg

    rules = make_rules("XQ", "X", "Q", step="0.01", min_qty="0.01", min_notional="1")
    book = make_book("XQ", [(43.03, 100)], [(43.04, 100)])
    # One unit in the 28th digit below 1.23 x 43.04: Decimal division rounds back up to 1.23.
    plan = simulate_leg(Leg("XQ", "BUY", "Q", "X"), rules, book, D("52.93919999999999999999999999"), D("0"))
    assert plan.base_qty == D("1.22")

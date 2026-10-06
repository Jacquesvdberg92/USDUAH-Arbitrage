from decimal import Decimal as D
from urllib.parse import parse_qs, urlparse

import pytest

from arbitrage_bot.exchange import BinanceAPIError, BinanceClient, OrderResult, sign
from arbitrage_bot.market import BUY, SELL

from conftest import make_book


class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.text = str(self._body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, *responses):
        self.headers = {}
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, timeout=None):
        self.requests.append((method, url))
        return self.responses.pop(0)


def test_signature_matches_binance_docs_example():
    secret = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
    query = "symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1&recvWindow=5000&timestamp=1499827319559"
    assert sign(secret, query) == "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"


def test_signed_order_request():
    raw = {
        "symbol": "USDCUSDT", "side": "BUY", "type": "LIMIT", "status": "FILLED",
        "executedQty": "10.00000000", "cummulativeQuoteQty": "10.00100000",
        "fills": [{"price": "1.0001", "qty": "10", "commission": "0.01", "commissionAsset": "USDC"}],
    }
    session = FakeSession(FakeResponse(body=raw))
    client = BinanceClient("https://example.test/", "key", "secret", session=session)
    result = client.new_order({"symbol": "USDCUSDT", "side": BUY, "type": "LIMIT", "timeInForce": "IOC", "quantity": "10", "price": "1.0001"})

    method, url = session.requests[0]
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    assert method == "POST" and parsed.path == "/api/v3/order"
    assert session.headers["X-MBX-APIKEY"] == "key"
    assert params["newOrderRespType"] == ["FULL"] and params["timeInForce"] == ["IOC"]
    unsigned = parsed.query.rsplit("&signature=", 1)[0]
    assert params["signature"] == [sign("secret", unsigned)]
    assert result.executed_qty == 10 and result.commissions == {"USDC": D("0.01")}


def test_errors_carry_code_and_retry_after():
    session = FakeSession(FakeResponse(429, {"code": -1003, "msg": "Too many requests"}, {"Retry-After": "7"}))
    client = BinanceClient("https://example.test", session=session)
    with pytest.raises(BinanceAPIError) as err:
        client.order_book("USDCUSDT")
    assert (err.value.status, err.value.code, err.value.retry_after) == (429, -1003, 7.0)
    with pytest.raises(BinanceAPIError):
        BinanceClient("https://example.test", session=FakeSession()).balances()  # no secret


def test_book_tickers_sends_compact_symbol_list():
    session = FakeSession(FakeResponse(body=[{"symbol": "A", "bidPrice": "1", "bidQty": "2", "askPrice": "3", "askQty": "4"}]))
    tickers = BinanceClient("https://example.test", session=session).book_tickers(["B", "A"])
    assert parse_qs(urlparse(session.requests[0][1]).query)["symbols"] == ['["A","B"]']
    assert tickers["A"].ask == 3


def test_order_flows(rules):
    buy = OrderResult("EURUSDC", BUY, "LIMIT", "FILLED", D("10"), D("11"), {"EUR": D("0.01")})
    assert buy.flows(rules["EURUSDC"]) == (D("11"), D("9.99"), {})
    sell_bnb = OrderResult("EURUSDC", SELL, "MARKET", "FILLED", D("10"), D("11"), {"BNB": D("0.0001")})
    assert sell_bnb.flows(rules["EURUSDC"]) == (D("10"), D("11"), {"BNB": D("0.0001")})


def test_paper_limit_ioc_partial_fill_and_balances(paper, market):
    market.books["USDCUSDT"] = make_book("USDCUSDT", [(0.9999, 100)], [(1.0000, 30), (1.0002, 100)])
    result = paper.limit_ioc("USDCUSDT", BUY, D("50"), D("1.0000"))
    assert result.status == "EXPIRED" and result.executed_qty == 30  # only the 1.0000 level qualifies
    balances = paper.balances()
    assert balances["USDT"] == 970 and balances["USDC"] == D("30") * D("0.999")


def test_paper_market_orders(paper):
    bought = paper.market_buy_quote("USDCUSDT", D("100"))
    assert bought.executed_qty == 100 and bought.quote_qty == 100
    sold = paper.market_sell("USDCUSDT", D("50"))
    assert sold.quote_qty == D("49.995")
    balances = paper.balances()
    assert balances["USDC"] == D("99.9") - 50
    assert balances["USDT"] == 900 + D("49.995") * D("0.999")


def test_paper_enforces_balance_and_filters(paper):
    with pytest.raises(BinanceAPIError) as err:
        paper.limit_ioc("USDCUSDT", BUY, D("2000"), D("1.0000"))
    assert err.value.code == -2010
    with pytest.raises(BinanceAPIError) as err:
        paper.limit_ioc("USDCUSDT", BUY, D("1"), D("1.0000"))  # below the 5 USDT minimum notional
    assert err.value.code == -1013
    with pytest.raises(BinanceAPIError) as err:
        paper.limit_ioc("USDCUSDT", BUY, D("10.001"), D("1.0000"))  # not a lot-size multiple
    assert err.value.code == -1013
    assert paper.balances() == {"USDT": 1000}


def test_paper_remembers_liquidity_it_took_until_it_expires(market, rules, fees):
    from arbitrage_bot.exchange import PaperExchange

    clock = [0.0]
    paper = PaperExchange(market, rules, fees, {"USDT": D("1000")}, depletion_ttl=60, now=lambda: clock[0])
    paper.market_buy_quote("USDCUSDT", D("100"))
    assert paper.order_books(["USDCUSDT"])["USDCUSDT"].asks[0] == (D("1.0000"), D("4900"))
    assert paper.book_tickers(["USDCUSDT"])["USDCUSDT"].ask_qty == 4900
    clock[0] = 61
    assert paper.order_books(["USDCUSDT"])["USDCUSDT"].asks[0] == (D("1.0000"), D("5000"))


def test_paper_forgets_a_level_once_it_leaves_the_public_book(market, rules, fees):
    from arbitrage_bot.exchange import PaperExchange

    paper = PaperExchange(market, rules, fees, {"USDT": D("1000")})
    paper.market_buy_quote("USDCUSDT", D("100"))
    market.books["USDCUSDT"] = make_book("USDCUSDT", [(0.9999, 5000)], [(1.0001, 5000)])
    paper.order_books(["USDCUSDT"])  # the 1.0000 level is gone...
    market.books["USDCUSDT"] = make_book("USDCUSDT", [(0.9999, 5000)], [(1.0000, 70)])
    assert paper.order_books(["USDCUSDT"])["USDCUSDT"].asks == ((D("1.0000"), D("70")),)  # ...so this is new liquidity

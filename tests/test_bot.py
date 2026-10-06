import json
from decimal import Decimal as D

import pytest

from arbitrage_bot import __main__ as cli
from arbitrage_bot.bot import ArbitrageBot, StopTrading
from arbitrage_bot.config import Config, load_config
from arbitrage_bot.exchange import BinanceAPIError
from arbitrage_bot.executor import CycleExecutor

from conftest import EUR_RULES, FakeMarket, make_book, profitable_books


def make_bot(config, paper, index, cycles, fees):
    return ArbitrageBot(config, paper, CycleExecutor(paper, index, "USDT"), cycles, fees, sleep=lambda s: None)


def flat_books():
    """No edge at all: every cycle loses the spread plus fees."""
    return {
        "USDCUSDT": make_book("USDCUSDT", [(0.9999, 5000)], [(1.0000, 5000)]),
        "EURUSDC": make_book("EURUSDC", [(1.1000, 5000)], [(1.1001, 5000)]),
        "EURUSDT": make_book("EURUSDT", [(1.0999, 5000)], [(1.1000, 5000)]),
    }


def test_trades_a_real_opportunity_and_logs_it(config, paper, index, cycles, fees):
    bot = make_bot(config, paper, index, cycles, fees)
    result = bot.step()
    assert result.status == "completed" and result.pnl > 0
    assert bot.stats.cycles == 1 and bot.stats.wins == 1 and bot.stats.pnl == result.pnl
    assert result.plan.spent == config.max_trade  # deep book -> biggest allowed size
    row = json.loads(open(config.trade_log).read().splitlines()[0])
    assert row["mode"] == "paper" and row["status"] == "completed" and len(row["orders"]) == 3


def test_screening_skips_depth_calls_when_nothing_is_close(config, paper, market, index, cycles, fees):
    market.books = flat_books()
    bot = make_bot(config, paper, index, cycles, fees)
    assert bot.step() is None
    assert market.ticker_calls == 1 and market.depth_calls == []
    assert bot.stats.best_edge_bps < 0


def test_trade_size_is_capped_by_balance(config, market, rules, index, cycles, fees):
    from arbitrage_bot.exchange import PaperExchange

    paper = PaperExchange(market, rules, fees, {"USDT": D("50")})
    result = make_bot(config, paper, index, cycles, fees).step()
    assert result.plan.spent <= 50 * D("0.9")


def test_loss_limit_stops_the_bot(config, paper, market, index, cycles, fees):
    market.books = flat_books()
    config.min_profit_bps = D("-1000")  # force trades so we can lose money
    config.min_profit_abs = D("-1000")
    config.max_loss = D("0.5")
    config.max_consecutive_losses = 100
    bot = make_bot(config, paper, index, cycles, fees)
    with pytest.raises(StopTrading, match="loss limit"):
        for _ in range(50):
            bot.step()
    assert bot.stats.losses >= 1 and bot.stats.pnl < 0


def test_consecutive_losses_stop_the_bot(config, paper, market, index, cycles, fees):
    market.books = flat_books()
    config.min_profit_bps = config.min_profit_abs = D("-1000")
    config.max_consecutive_losses = 2
    bot = make_bot(config, paper, index, cycles, fees)
    bot.step()
    with pytest.raises(StopTrading, match="2 losing cycles"):
        bot.step()


def test_run_stops_after_max_cycles(config, paper, index, cycles, fees):
    config.max_cycles = 2
    bot = make_bot(config, paper, index, cycles, fees)
    bot.run()
    assert bot.stats.cycles == 2 and bot.stats.pnl > 0


def test_run_backs_off_and_stops_on_repeated_errors(config, paper, index, cycles, fees):
    sleeps = []

    def broken(symbols):
        raise BinanceAPIError(503, -1001, "unavailable")

    paper.book_tickers = broken
    config.max_consecutive_errors = 3
    bot = ArbitrageBot(config, paper, CycleExecutor(paper, index, "USDT"), cycles, fees, sleep=sleeps.append)
    bot.run()
    assert bot.stats.errors == 3 and sleeps == [2.0, 4.0]


def test_dust_is_swept_once_it_is_tradable(config, paper, index, cycles, fees):
    bot = make_bot(config, paper, index, cycles, fees)
    bot.dust["EUR"] = D("10")  # pretend earlier cycles left 10 EUR behind
    paper._balances["EUR"] = D("10")
    bot._sweep_dust()
    assert bot.dust["EUR"] == 0
    assert bot.stats.pnl == D("10") * D("1.1050") * D("0.999")


def test_config_loading(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"min_profit_bps": "1.5", "fee_overrides_bps": {"USDCUSDT": 0}, "paper_balances": {"USDT": 50}}))
    cfg = load_config(str(path))
    assert cfg.min_profit_bps == D("1.5") and cfg.paper_balances == {"USDT": D("50")}
    assert cfg.fee_rate("USDCUSDT", D("0.001")) == 0  # override wins
    assert cfg.fee_rate("EURUSDT", D("0.00075")) == D("0.00075")  # then the account rate
    assert cfg.fee_rate("EURUSDT") == D("0.001")  # then taker_fee_bps
    path.write_text(json.dumps({"min_proft_bps": 1}))
    with pytest.raises(ValueError, match="unknown config keys"):
        load_config(str(path))
    with pytest.raises(ValueError):
        Config(triangles=[["USDC", "EUR", "BRL"]]).validate()


@pytest.fixture
def fake_client(monkeypatch):
    market = FakeMarket(profitable_books(), EUR_RULES)
    monkeypatch.setattr(cli, "BinanceClient", lambda *a, **k: market)
    return market


def test_cli_once_prints_report(tmp_path, monkeypatch, capsys, fake_client):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"triangles": [["USDT", "USDC", "EUR"], ["USDT", "USDC", "TRY"]], "log_file": ""}))
    assert cli.main(["--once"]) == 0
    out = capsys.readouterr().out
    assert "USDT > USDC > EUR > USDT" in out and "USDT > EUR > USDC > USDT" in out


def test_cli_refuses_live_without_confirmation(tmp_path, monkeypatch, fake_client):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    with pytest.raises(SystemExit):
        cli.main(["--mode", "live"])


def test_cli_live_needs_keys(tmp_path, monkeypatch, fake_client):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    with pytest.raises(SystemExit):
        cli.main(["--mode", "live", "--confirm-live"])


def test_risk_checks_run_even_if_post_trade_refresh_fails(config, paper, market, index, cycles, fees):
    market.books = flat_books()
    config.min_profit_bps = config.min_profit_abs = D("-1000")
    config.max_cycles = 1
    bot = make_bot(config, paper, index, cycles, fees)

    def broken():
        raise BinanceAPIError(503, -1001, "unavailable")

    paper.balances = broken
    with pytest.raises(StopTrading, match="max_cycles"):
        bot.step()

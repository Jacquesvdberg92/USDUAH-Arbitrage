import http.client
import json
import pathlib
import logging
import threading
import time

import pytest

from arbitrage_bot import __main__ as cli
from arbitrage_bot.bot import ArbitrageBot
from arbitrage_bot.dashboard import Dashboard, LogBuffer
from arbitrage_bot.executor import CycleExecutor

from conftest import EUR_RULES, FakeMarket, make_book, profitable_books


def make_bot(config, paper, index, cycles, fees, **kwargs):
    kwargs.setdefault("sleep", lambda s: None)
    return ArbitrageBot(config, paper, CycleExecutor(paper, index, "USDT"), cycles, fees, **kwargs)


def flat_books():
    return {**profitable_books(), "EURUSDT": make_book("EURUSDT", [(1.0999, 5000)], [(1.1000, 5000)])}


@pytest.fixture(autouse=True)
def stub_page(tmp_path, monkeypatch):
    """Server tests don't depend on the real page's contents (tested separately)."""
    page = tmp_path / "dashboard.html"
    page.write_text("<!doctype html><html><body>dashboard</body></html>")
    monkeypatch.setattr("arbitrage_bot.dashboard.PAGE", page)


@pytest.fixture
def served(config, paper, index, cycles, fees, caplog):
    caplog.set_level(logging.INFO)  # as setup_logging does in the real program
    bot = make_bot(config, paper, index, cycles, fees)
    buffer = LogBuffer()
    logging.getLogger().addHandler(buffer)
    dashboard = Dashboard(bot, buffer, port=0)
    dashboard.start()
    yield bot, dashboard
    dashboard.stop()
    logging.getLogger().removeHandler(buffer)


def request(dashboard, method, path, headers=None, host=None):
    port = dashboard.server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = dict(headers or {})
    if host:
        headers["Host"] = host
    conn.request(method, path, headers=headers)
    response = conn.getresponse()
    body = response.read()
    conn.close()
    return response.status, dict(response.getheaders()), body


def test_snapshot_describes_what_the_bot_is_doing(config, paper, index, cycles, fees):
    bot = make_bot(config, paper, index, cycles, fees)
    result = bot.step()
    bot.publish()
    snap = json.loads(json.dumps(bot.latest_snapshot))  # plain JSON all the way down
    assert snap["mode"] == "paper" and snap["status"] == "running" and snap["home"] == "USDT"
    assert {c["path"] for c in snap["cycles"]} == {c.path for c in cycles}
    best = max(snap["cycles"], key=lambda c: c["edge_bps"])
    assert best["edge_bps"] > 14 and best["last_check"]["traded"] is True
    assert len(snap["edge_history"]) == 1 and snap["edge_history"][0]["best_bps"] == pytest.approx(best["edge_bps"])
    trade = snap["trades"][0]
    assert trade["status"] == "completed" and trade["pnl"] == pytest.approx(float(result.pnl))
    assert snap["stats"]["equity_change"] > 0 and snap["balances"]["USDT"] > 1000
    assert [limit["name"] for limit in snap["limits"]] == ["Loss limit", "Losing streak", "Error streak", "Rejected orders", "Cycles"]


def test_server_serves_the_page_and_live_state(served):
    bot, dashboard = served
    bot.step()
    bot.publish()
    status, headers, body = request(dashboard, "GET", "/")
    assert status == 200 and headers["Content-Type"].startswith("text/html") and b"<html" in body.lower()
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    status, _, body = request(dashboard, "GET", "/api/state")
    state = json.loads(body)
    assert status == 200 and state["status"] == "running" and state["stats"]["cycles"] == 1
    assert any("OPPORTUNITY" in e["message"] for e in state["events"])


def test_controls_need_the_dashboard_header(served):
    bot, dashboard = served
    status, _, _ = request(dashboard, "POST", "/api/pause")
    assert status == 403 and not bot.paused  # a cross-site form post can't do this
    status, _, body = request(dashboard, "POST", "/api/pause", {"X-Arb-Dashboard": "1"})
    assert status == 200 and json.loads(body) == {"ok": True, "status": "paused"} and bot.paused
    request(dashboard, "POST", "/api/resume", {"X-Arb-Dashboard": "1"})
    assert not bot.paused
    status, _, _ = request(dashboard, "POST", "/api/withdraw", {"X-Arb-Dashboard": "1"})
    assert status == 404
    request(dashboard, "POST", "/api/stop", {"X-Arb-Dashboard": "1"})
    assert bot.stop_requested and bot.status == "stopping"


def test_foreign_host_header_is_refused(served):
    _, dashboard = served
    status, _, _ = request(dashboard, "GET", "/api/state", host="evil.example:8765")  # DNS rebinding
    assert status == 403
    status, _, _ = request(dashboard, "GET", "/api/state", host="localhost:8765")
    assert status == 200


def test_paused_bot_watches_but_does_not_trade(config, paper, index, cycles, fees, caplog):
    bot = make_bot(config, paper, index, cycles, fees)
    bot.pause()
    with caplog.at_level(logging.INFO):
        assert bot.step() is None
    assert paper.balances() == {"USDT": 1000}
    assert any("would trade" in r.getMessage() for r in caplog.records)
    assert not any(check["traded"] for check in bot.last_checks.values())


def test_stop_button_wakes_a_sleeping_bot(config, paper, market, index, cycles, fees):
    market.books = flat_books()
    config.poll_interval_sec = 30  # without the wake-up this would take 30 s
    bot = make_bot(config, paper, index, cycles, fees, sleep=None)
    codes = []
    thread = threading.Thread(target=lambda: codes.append(bot.run()))
    thread.start()
    time.sleep(0.2)
    bot.request_stop()
    thread.join(timeout=5)
    assert not thread.is_alive() and codes == [0]
    assert bot.status == "stopped" and bot.latest_snapshot["status"] == "stopped"
    assert bot.latest_snapshot["stop_reason"].startswith("stop requested")


def test_cli_ui_flag(tmp_path, monkeypatch):
    market = FakeMarket(profitable_books(), EUR_RULES)
    monkeypatch.setattr(cli, "BinanceClient", lambda *a, **k: market)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"triangles": [["USDT", "USDC", "EUR"]], "log_file": "", "max_cycles": 1}))
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)

    def stop_waiting(seconds):
        raise KeyboardInterrupt  # the user closes the program after reading the dashboard

    monkeypatch.setattr(cli.time, "sleep", stop_waiting)
    assert cli.main(["--ui", "--ui-port", "0"]) == 0
    assert opened and opened[0].startswith("http://127.0.0.1:")


def test_real_page_is_self_contained_and_uses_the_control_header():
    import re

    import arbitrage_bot

    html = (pathlib.Path(arbitrage_bot.__file__).parent / "dashboard.html").read_text()
    assert not re.search(r"""(src|href)\s*=\s*["']https?://""", html)  # works offline, no CDNs
    assert "X-Arb-Dashboard" in html and "/api/state" in html

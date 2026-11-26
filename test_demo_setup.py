"""Offline tests of demo-account boundaries; no terminal or network is used."""
import copy
import importlib
import logging
import gc
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import pandas  # Keep numerical libraries real when running the existing suite too.
import numpy

with patch("logging.FileHandler", return_value=logging.NullHandler()):
    core = importlib.import_module("connect_mt5")
import demo_test


class DemoSetupTests(unittest.TestCase):
    def account(self, **overrides):
        fields = dict(login=123, server="Test-Demo", trade_mode=0, currency="USD",
                      balance=1000., equity=1000., trade_allowed=True, trade_expert=True)
        return SimpleNamespace(**dict(fields, **overrides))

    def bot(self):
        bot = core.TradingBot.__new__(core.TradingBot)
        bot.config = dict(demo_only=True, expected_login=123, expected_server="Test-Demo")
        return bot

    @patch.object(core, "mt5")
    def test_order_blocked_on_real_account(self, broker):
        broker.ACCOUNT_TRADE_MODE_DEMO = 0
        broker.account_info.return_value = self.account(trade_mode=2)
        self.assertIsNone(self.bot().send_order({"action": 1}))
        broker.order_send.assert_not_called()

    @patch.object(core, "mt5")
    def test_order_blocked_after_account_switch_or_disconnect(self, broker):
        broker.ACCOUNT_TRADE_MODE_DEMO = 0
        for account in [None, self.account(login=124), self.account(server="Other-Demo")]:
            broker.account_info.return_value = account
            self.assertIsNone(self.bot().send_order({"action": 1}))
        broker.order_send.assert_not_called()

    @patch.object(core, "mt5")
    def test_expected_demo_order_is_tagged(self, broker):
        broker.ACCOUNT_TRADE_MODE_DEMO = 0
        broker.account_info.return_value = self.account()
        request = {"action": 1}
        self.bot().send_order(request)
        broker.order_send.assert_called_once_with({"action": 1, "comment": "DemoTest-v1"})
        self.assertEqual(request, {"action": 1})

    def test_profile_isolated_from_original_configuration(self):
        original = copy.deepcopy(core.CONFIG)
        cfg = demo_test.build_profile(original, self.account(), "EURUSD#", Path("demo-records"))
        self.assertEqual(original, core.CONFIG)
        self.assertEqual(cfg["max_total_positions"], 1)
        self.assertEqual(cfg["risk_percent"], .25)
        self.assertFalse(cfg["enable_dynamic_risk"])
        self.assertFalse(cfg["enable_telegram"])
        self.assertFalse(cfg["load_optimized_settings"])
        self.assertEqual(list(cfg["portfolio"]), ["EURUSD#"])
        self.assertNotEqual(cfg["db_name"], original["db_name"])

    @patch.object(demo_test, "mt5")
    def test_preflight_rejects_real_without_order(self, broker):
        broker.initialize.return_value = True
        broker.ACCOUNT_TRADE_MODE_DEMO = 0
        broker.account_info.return_value = self.account(trade_mode=2)
        broker.terminal_info.return_value = SimpleNamespace(connected=True)
        with self.assertRaisesRegex(RuntimeError, "DEMO accounts only"):
            demo_test.preflight("EURUSD#")
        broker.order_send.assert_not_called()

    @patch.object(demo_test, "mt5")
    def test_successful_preflight_never_sends_orders(self, broker):
        broker.initialize.return_value = True
        broker.ACCOUNT_TRADE_MODE_DEMO = 0
        broker.SYMBOL_TRADE_MODE_FULL = 4
        broker.account_info.return_value = self.account()
        broker.terminal_info.return_value = SimpleNamespace(
            connected=True, trade_allowed=True, tradeapi_disabled=False)
        broker.symbol_info.return_value = SimpleNamespace(
            point=.00001, trade_mode=4, volume_step=.01, volume_min=.01,
            trade_tick_size=.00001, trade_tick_value=1.)
        broker.symbol_info_tick.return_value = SimpleNamespace(
            ask=1.1001, bid=1.1, time=demo_test.time.time())
        broker.copy_rates_from_pos.side_effect = lambda symbol, timeframe, start, count: [0] * count
        broker.positions_get.return_value = ()
        broker.orders_get.return_value = ()
        with patch.object(demo_test, "confirm_quote_updates", return_value=broker.symbol_info_tick.return_value):
            self.assertEqual(demo_test.preflight("EURUSD#").login, 123)
        broker.order_send.assert_not_called()

    @patch.object(demo_test.time, "sleep")
    @patch.object(demo_test, "mt5")
    def test_advancing_quotes_pass_despite_three_hour_clock_offset(self, broker, sleep):
        initial = SimpleNamespace(time=20000, time_msc=20000000, bid=1.1, ask=1.1001)
        latest = SimpleNamespace(time=20001, time_msc=20001000, bid=1.1, ask=1.1001)
        broker.symbol_info_tick.return_value = latest
        with patch.object(demo_test.time, "time", return_value=9200), patch.object(
                demo_test.time, "monotonic", side_effect=[100, 101]):
            self.assertIs(demo_test.confirm_quote_updates("EURUSD", initial), latest)
        broker.order_send.assert_not_called()

    @patch.object(demo_test.time, "sleep")
    @patch.object(demo_test, "mt5")
    def test_frozen_quote_with_current_timestamp_is_rejected(self, broker, sleep):
        initial = SimpleNamespace(time=20000, time_msc=20000000, bid=1.1, ask=1.1001)
        broker.symbol_info_tick.return_value = initial
        with patch.object(demo_test.time, "monotonic", side_effect=[100, 101, 102]):
            with self.assertRaisesRegex(RuntimeError, "No fresh quote progression"):
                demo_test.confirm_quote_updates("EURUSD", initial, attempts=2)
        broker.order_send.assert_not_called()

    @patch.object(demo_test.time, "sleep")
    @patch.object(demo_test, "mt5")
    def test_large_timestamp_jump_alone_does_not_prove_liveness(self, broker, sleep):
        initial = SimpleNamespace(time=20000, time_msc=20000000, bid=1.1, ask=1.1001)
        broker.symbol_info_tick.return_value = SimpleNamespace(
            time=30800, time_msc=30800000, bid=1.1, ask=1.1001)
        with patch.object(demo_test.time, "monotonic", side_effect=[100, 101, 101, 102]):
            with self.assertRaisesRegex(RuntimeError, "No fresh quote progression"):
                demo_test.confirm_quote_updates("EURUSD", initial, attempts=2)

    @patch.object(demo_test.time, "sleep")
    @patch.object(demo_test, "mt5")
    def test_quote_read_error_is_rejected(self, broker, sleep):
        initial = SimpleNamespace(time=20000, bid=1.1, ask=1.1001)
        broker.symbol_info_tick.return_value = None
        with self.assertRaisesRegex(RuntimeError, "Quote unavailable"):
            demo_test.confirm_quote_updates("EURUSD", initial)


class NewsFilterTests(unittest.TestCase):
    @patch.object(core.requests, "get")
    def test_http_failure_blocks_entries_and_throttles_retries(self, get):
        get.return_value.status_code = 403
        news = core.NewsFilter("https://example.test/calendar.json")
        db = MagicMock()
        for _ in range(4):
            self.assertEqual(news.is_news_active(db, -5, 30), (True, None))
        get.assert_called_once()
        db.update_news.assert_not_called()

    @patch.object(core.requests, "get")
    def test_json_timezone_offset_is_normalized_and_blocks_event_window(self, get):
        event_time = datetime.now(timezone.utc) + timedelta(minutes=10)
        eastern = event_time.astimezone(timezone(timedelta(hours=-4)))
        get.return_value.status_code = 200
        get.return_value.json.return_value = [dict(date=eastern.isoformat(), impact="High",
                                                 title="Test announcement", country="USD")]
        news = core.NewsFilter("https://example.test/calendar.json")
        db = MagicMock()
        active, event = news.is_news_active(db, -5, 30)
        self.assertTrue(active)
        self.assertEqual(event, event_time)
        self.assertEqual(event.utcoffset(), timedelta(0))
        news.is_news_active(db, -5, 30)
        get.assert_called_once()

    @patch.object(core.requests, "get")
    def test_expired_calendar_and_failed_refresh_block_entries(self, get):
        news = core.NewsFilter("https://example.test/calendar.json")
        news.last_fetch = datetime.now(timezone.utc) - timedelta(hours=7)
        get.return_value.status_code = 429
        self.assertEqual(news.is_news_active(MagicMock(), -5, 30), (True, None))

    @patch.object(core.requests, "get")
    def test_invalid_payload_never_becomes_no_news(self, get):
        get.return_value.status_code = 200
        get.return_value.json.return_value = []
        news = core.NewsFilter("https://example.test/calendar.json")
        self.assertEqual(news.is_news_active(MagicMock(), -5, 30), (True, None))
        self.assertIsNone(news.last_fetch)

    def test_simultaneous_events_do_not_break_database_refresh(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            db = core.DatabaseManager(str(Path(folder) / "news.db"))
            moment = datetime.now(timezone.utc)
            db.update_news([(moment, "First event", "USD"), (moment, "Second event", "EUR")])
            conn = sqlite3.connect(db.db_name)
            try:
                rows = conn.execute("SELECT title FROM upcoming_news").fetchall()
                self.assertEqual(rows, [("First event; Second event",)])
            finally:
                conn.close()
            gc.collect()


if __name__ == "__main__":
    unittest.main()

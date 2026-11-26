import sys
from unittest.mock import MagicMock

# Mock MetaTrader5 before importing connect_mt5 so tests run on any Python environment
if "MetaTrader5" not in sys.modules:
    mock_mt5_module = MagicMock()
    mock_mt5_module.TIMEFRAME_M1 = 1
    mock_mt5_module.TIMEFRAME_H1 = 16385
    mock_mt5_module.POSITION_TYPE_BUY = 0
    mock_mt5_module.POSITION_TYPE_SELL = 1
    mock_mt5_module.ORDER_TYPE_BUY = 0
    mock_mt5_module.ORDER_TYPE_SELL = 1
    mock_mt5_module.DEAL_ENTRY_IN = 0
    mock_mt5_module.DEAL_ENTRY_OUT = 1
    mock_mt5_module.DEAL_ENTRY_OUT_BY = 2
    mock_mt5_module.ORDER_FILLING_IOC = 1
    mock_mt5_module.ORDER_FILLING_FOK = 0
    mock_mt5_module.ORDER_FILLING_RETURN = 2
    mock_mt5_module.TRADE_ACTION_DEAL = 1
    mock_mt5_module.TRADE_ACTION_SLTP = 6
    mock_mt5_module.TRADE_RETCODE_DONE = 10009
    sys.modules["MetaTrader5"] = mock_mt5_module

if "pandas" not in sys.modules:
    mock_pandas = MagicMock()
    mock_pandas.DataFrame = MagicMock
    mock_pandas.Series = MagicMock
    mock_pandas.read_sql_query = MagicMock(return_value=MagicMock(empty=True))
    sys.modules["pandas"] = mock_pandas

if "numpy" not in sys.modules:
    sys.modules["numpy"] = MagicMock()

if "requests" not in sys.modules:
    sys.modules["requests"] = MagicMock()

import os
os.environ.setdefault("MT5_LOGIN", "12345678")
os.environ.setdefault("MT5_PASSWORD", "testpass")
os.environ.setdefault("MT5_SERVER", "TestServer")

import unittest
from unittest.mock import patch
import sqlite3
import os
import tempfile
from datetime import datetime, timedelta

from connect_mt5 import DatabaseManager, TradingBot, CONFIG

class TestTradingBotRegressions(unittest.TestCase):
    def setUp(self):
        # Create an isolated temporary database for testing
        self.temp_db = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        self.db_name = self.temp_db.name
        self.temp_db.close()

    def tearDown(self):
        import gc
        gc.collect()
        if os.path.exists(self.db_name):
            try:
                os.remove(self.db_name)
            except PermissionError:
                pass

    def test_1_database_migration_existing_table(self):
        """[P1 Fix] Ensure database migration adds missing columns to an existing legacy table."""
        # 1. Create a legacy table lacking commission, swap, fee
        with sqlite3.connect(self.db_name) as conn:
            conn.execute('''CREATE TABLE trades (
                ticket INTEGER PRIMARY KEY, symbol TEXT, open_time TEXT, type TEXT,
                lots REAL, open_price REAL, sl REAL, tp REAL, close_time TEXT,
                close_price REAL, profit REAL
            )''')
            conn.execute("INSERT INTO trades VALUES (101, 'EURUSD#', '2026-05-10T12:00:00', 'BUY', 0.01, 1.0800, 1.0750, 1.0900, NULL, NULL, NULL)")
            conn.commit()

        # 2. Instantiate DatabaseManager (triggers _init_db and migrations)
        db = DatabaseManager(self.db_name)

        # 3. Verify columns now exist
        with sqlite3.connect(self.db_name) as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()]
            self.assertIn("commission", cols)
            self.assertIn("swap", cols)
            self.assertIn("fee", cols)

        # 4. Record a close - must NOT raise 'no such column: commission'
        db.log_trade_close(101, 1.0850, 5.0, commission=-0.5, swap=-0.1, fee=0.0, close_time="2026-05-10T13:00:00")

        with sqlite3.connect(self.db_name) as conn:
            row = conn.execute("SELECT close_price, profit, commission, swap, close_time FROM trades WHERE ticket = 101").fetchone()
            self.assertEqual(row[0], 1.0850)
            self.assertEqual(row[1], 5.0)
            self.assertEqual(row[2], -0.5)
            self.assertEqual(row[3], -0.1)
            self.assertEqual(row[4], "2026-05-10T13:00:00")

    @patch("connect_mt5.mt5")
    def test_2_reconciliation_api_error_does_not_create_false_close(self, mock_mt5):
        """[P1 Fix] Broker query errors (returning None or empty deals) must not mark trades closed with 0 profit."""
        db = DatabaseManager(self.db_name)
        db.log_trade_open(202, "EURUSD#", "BUY", 0.01, 1.0800, 1.0750, 1.0900)

        cfg = CONFIG.copy()
        cfg["db_name"] = self.db_name
        bot = TradingBot(cfg)

        # Case A: positions_get returns None (simulated connection/query failure)
        mock_mt5.positions_get.return_value = None
        mock_mt5.last_error.return_value = (-1, "Terminal disconnected")
        bot.reconcile_trades()

        # Verify trade is STILL open
        self.assertIn(202, db.get_open_trade_tickets())

        # Case B: position closed at broker (pos == ()), but history_deals_get returns None (API error)
        mock_mt5.positions_get.return_value = ()
        mock_mt5.history_deals_get.return_value = None
        bot.reconcile_trades()

        # Verify trade is STILL open (not marked 0.00 profit)
        self.assertIn(202, db.get_open_trade_tickets())

        # Case C: history_deals_get returns empty tuple () (deals still syncing)
        mock_mt5.history_deals_get.return_value = ()
        bot.reconcile_trades()

        self.assertIn(202, db.get_open_trade_tickets())

    @patch("connect_mt5.mt5")
    def test_3_reconciliation_aggregates_all_deals_and_costs(self, mock_mt5):
        """[P1 Fix] Multi-deal position (entry commission + partial loss + exit gain) aggregates correctly."""
        db = DatabaseManager(self.db_name)
        db.log_trade_open(303, "EURUSD#", "BUY", 0.02, 1.0800, 1.0750, 1.0900)

        cfg = CONFIG.copy()
        cfg["db_name"] = self.db_name
        bot = TradingBot(cfg)

        # Mock deals:
        # Deal 1 (IN): commission = -10, profit = 0
        # Deal 2 (OUT partial): profit = -15, comm = -2.5, swap = -0.5
        # Deal 3 (OUT final): profit = +9, comm = -2.5, swap = -0.5
        # Total net profit = -10 + (-15 - 2.5 - 0.5) + (9 - 2.5 - 0.5) = -27
        deal1 = MagicMock(entry=0, profit=0.0, commission=-10.0, swap=0.0, fee=0.0, price=1.0800, time=1700000000, symbol="EURUSD#") # DEAL_ENTRY_IN
        deal2 = MagicMock(entry=1, profit=-15.0, commission=-2.5, swap=-0.5, fee=0.0, price=1.0780, time=1700001000, symbol="EURUSD#") # DEAL_ENTRY_OUT
        deal3 = MagicMock(entry=1, profit=9.0, commission=-2.5, swap=-0.5, fee=0.0, price=1.0820, time=1700002000, symbol="EURUSD#") # DEAL_ENTRY_OUT

        mock_mt5.DEAL_ENTRY_OUT = 1
        mock_mt5.DEAL_ENTRY_OUT_BY = 2
        mock_mt5.positions_get.return_value = () # closed
        mock_mt5.history_deals_get.return_value = (deal1, deal2, deal3)

        bot.reconcile_trades()

        # Trade must now be closed with aggregate net loss of -27.0
        self.assertNotIn(303, db.get_open_trade_tickets())
        with sqlite3.connect(self.db_name) as conn:
            row = conn.execute("SELECT profit, commission, swap, close_time FROM trades WHERE ticket = 303").fetchone()
            self.assertEqual(row[0], -22.0)
            self.assertEqual(row[1], -15.0)
            self.assertEqual(row[2], -1.0)
            expected_close_time = datetime.fromtimestamp(1700002000).isoformat()
            self.assertEqual(row[3], expected_close_time)

        # Streak count must be incremented for EURUSD# because it was a net loss
        self.assertEqual(bot.streak_counts["EURUSD#"], 1)

    @patch("connect_mt5.mt5")
    def test_4_emergency_stop_recovery_and_closure_retry(self, mock_mt5):
        """[P1 Fix] Emergency stop keeps retrying closure of positions and does not prevent recovery."""
        db = DatabaseManager(self.db_name)
        cfg = CONFIG.copy()
        cfg["db_name"] = self.db_name
        cfg["magic_number"] = 123456
        bot = TradingBot(cfg)

        # Set emergency stopped state
        db.set_emergency_stopped(True)
        db.set_control_command("STOPPED")

        # Mock an open bot position that failed to close previously
        mock_pos = MagicMock(ticket=999, symbol="EURUSD#", magic=123456, type=0) # BUY
        mock_mt5.positions_get.return_value = [mock_pos]
        mock_mt5.symbol_info_tick.return_value = MagicMock(bid=1.0800, ask=1.0802)
        mock_mt5.order_send.return_value = MagicMock(retcode=10009) # TRADE_RETCODE_DONE

        with patch.object(bot, "close_all_positions") as mock_close_all:
            is_emergency = bot.check_emergency_stop()
            self.assertTrue(is_emergency)
            # Must retry closure because open_bot_pos was detected
            mock_close_all.assert_called_once()

        # Now simulate user sending /resume via Telegram or RESUME from dashboard
        db.set_control_command("RESUME")
        bot.check_remote_commands()

        # Bot must now be recovered: not paused, emergency flag cleared, command START
        self.assertFalse(bot.is_paused)
        self.assertFalse(db.get_risk_state()["emergency_stopped"])
        self.assertEqual(db.get_control_command(), "START")

    @patch("connect_mt5.mt5")
    def test_5_manual_trade_validations_and_state_preservation(self, mock_mt5):
        """[P1 Fix] can_open_trade blocks manual entry outside session or while paused, and preserves state."""
        db = DatabaseManager(self.db_name)
        cfg = CONFIG.copy()
        cfg["db_name"] = self.db_name
        bot = TradingBot(cfg)

        # Mock MT5 symbol & tick info
        mock_mt5.symbol_info.return_value = MagicMock(point=0.00001)
        mock_mt5.symbol_info_tick.return_value = MagicMock(bid=1.08000, ask=1.08010)
        mock_mt5.positions_get.return_value = []

        # 1. Blocked when paused
        bot.is_paused = True
        self.assertFalse(bot.can_open_trade("EURUSD#", is_manual=True))
        bot.is_paused = False

        # 2. Blocked outside trading session
        with patch.object(bot, "is_in_session", return_value=False):
            self.assertFalse(bot.can_open_trade("EURUSD#", is_manual=True))

        # 3. Blocked during news blackout
        with patch.object(bot, "is_in_session", return_value=True):
            if bot.news:
                with patch.object(bot.news, "is_news_active", return_value=(True, datetime.now())):
                    self.assertFalse(bot.can_open_trade("EURUSD#", is_manual=True))

        # 4. State preservation: if bot was PAUSED, a manual command must NOT reset it to START
        bot.is_paused = True
        db.set_control_command("MANUAL_BUY:EURUSD#")
        with patch.object(bot, "can_open_trade", return_value=False):
            bot.check_remote_commands()

        # Command must be preserved as PAUSE, not START
        self.assertTrue(bot.is_paused)
        self.assertEqual(db.get_control_command(), "PAUSE")

if __name__ == "__main__":
    unittest.main()

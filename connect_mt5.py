import MetaTrader5 as mt5
import pandas as pd
import time
import logging
import math
import requests
import sqlite3
import os
from datetime import datetime, timedelta, timezone
import numpy as np

# --- CONFIGURATION ---
CONFIG = {
    "login": int(os.getenv("MT5_LOGIN") or 0),
    "password": os.getenv("MT5_PASSWORD"),
    "server": os.getenv("MT5_SERVER"),
    
    "portfolio": {
        "EURUSD#": {"strategy": "MA_Crossover_RSI"},
        "GBPUSD#": {"strategy": "MA_Crossover_RSI", "fast_ma": 12, "slow_ma": 26, "risk_percent": 0.5},
        "USDJPY#": {"strategy": "BB_Mean_Reversion", "risk_percent": 1.5}
    },

    # Risk & Session Management
    "basket_tp_currency": 500.0,
    "basket_sl_currency": -250.0,
    "max_consecutive_losses": 3,
    "streak_pause_hours": 4,
    "trading_sessions": [
        {"start": "00:00", "end": "07:00"},
        {"start": "08:00", "end": "12:00"},
        {"start": "13:00", "end": "17:00"}
    ],
    "friday_close_hour": 20,
    "check_friday": True,
    
    "max_total_positions": 3,
    "max_correlation": 0.8,
    "correlation_timeframe": mt5.TIMEFRAME_H1,
    "enable_dynamic_risk": True,
    "kelly_fraction": 0.2,
    "min_risk_percent": 0.5,
    "max_risk_percent": 3.0,
    
    "enable_news_filter": True,
    "news_pause_mins": 30,
    "news_timezone_offset": -5,
    "news_url": "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    
    "fast_ma": 10, "slow_ma": 20, "rsi_period": 14, "rsi_upper": 70, "rsi_lower": 30,
    "sl_atr_multiplier": 1.5, "tp_atr_multiplier": 3.0, "breakeven_atr_mult": 1.0, "atr_period": 14,
    "risk_percent": 1.0, "timeframe": mt5.TIMEFRAME_M1, "enable_mtf": True,
    "trend_timeframe": mt5.TIMEFRAME_H1, "trend_period": 200, "trailing_stop_points": 100,
    "trailing_step_points": 20, "magic_number": 123456, "max_drawdown_percent": 5.0,
    "max_drawdown_close_all": 10.0, "daily_loss_limit": 100.0, "daily_profit_target": 200.0,
    "trading_start_hour": 8, "trading_end_hour": 20, "trade_cooldown_minutes": 15,
    "max_spread_points": 50, "db_name": "trading_data.db",
    "telegram_token": os.getenv("TELEGRAM_TOKEN"),
    "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID"),
    "enable_telegram": bool(os.getenv("TELEGRAM_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")), "heartbeat_interval_hours": 1, "equity_log_interval_mins": 15
}

# --- LOGGING ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[logging.FileHandler(os.environ.get("BOT_LOG_FILE", "trading_bot.log"), encoding="utf-8"), logging.StreamHandler()])
logger = logging.getLogger(__name__)

# --- DATABASE ---
class DatabaseManager:
    def __init__(self, db_name):
        self.db_name = db_name
        self._init_db()
    def _init_db(self):
        with sqlite3.connect(self.db_name) as conn:
            c = conn.cursor()
            c.execute('CREATE TABLE IF NOT EXISTS trades (ticket INTEGER PRIMARY KEY, symbol TEXT, open_time TEXT, type TEXT, lots REAL, open_price REAL, sl REAL, tp REAL, close_time TEXT, close_price REAL, profit REAL, commission REAL DEFAULT 0, swap REAL DEFAULT 0, fee REAL DEFAULT 0)')
            c.execute('CREATE TABLE IF NOT EXISTS equity_history (time TEXT PRIMARY KEY, balance REAL, equity REAL, margin REAL)')
            c.execute('CREATE TABLE IF NOT EXISTS bot_control (id INTEGER PRIMARY KEY, command TEXT, status TEXT)')
            c.execute('CREATE TABLE IF NOT EXISTS upcoming_news (time TEXT PRIMARY KEY, title TEXT, country TEXT)')
            c.execute('CREATE TABLE IF NOT EXISTS risk_state (id INTEGER PRIMARY KEY, peak_equity REAL, day_date TEXT, day_start_equity REAL, emergency_stopped INTEGER DEFAULT 0)')
            c.execute("INSERT OR IGNORE INTO bot_control (id, command, status) VALUES (1, 'START', 'ACTIVE')")
            c.execute("INSERT OR IGNORE INTO risk_state (id, peak_equity, day_date, day_start_equity, emergency_stopped) VALUES (1, 0, '', 0, 0)")

            # --- MIGRATIONS FOR EXISTING DATABASES ---
            # Ensure trades table has commission, swap, fee columns
            existing_cols = [row[1] for row in c.execute("PRAGMA table_info(trades)").fetchall()]
            for col_name, col_def in [('commission', 'REAL DEFAULT 0'), ('swap', 'REAL DEFAULT 0'), ('fee', 'REAL DEFAULT 0')]:
                if col_name not in existing_cols:
                    c.execute(f"ALTER TABLE trades ADD COLUMN {col_name} {col_def}")
                    logger.info(f"Database migration: Added column {col_name} to trades table.")
            conn.commit()
    def log_trade_open(self, ticket, symbol, order_type, lot, price, sl, tp):
        with sqlite3.connect(self.db_name) as conn:
            conn.execute('INSERT OR REPLACE INTO trades (ticket, symbol, open_time, type, lots, open_price, sl, tp) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                         (ticket, symbol, datetime.now().isoformat(), order_type, lot, price, sl, tp))
    def log_trade_close(self, ticket, price, profit, commission=0.0, swap=0.0, fee=0.0, close_time=None):
        if close_time is None:
            close_time = datetime.now().isoformat()
        with sqlite3.connect(self.db_name) as conn:
            conn.execute('UPDATE trades SET close_time = ?, close_price = ?, profit = ?, commission = ?, swap = ?, fee = ? WHERE ticket = ?',
                         (close_time, price, profit, commission, swap, fee, ticket))
    def log_equity(self, b, e, m):
        with sqlite3.connect(self.db_name) as conn:
            conn.execute('INSERT OR REPLACE INTO equity_history (time, balance, equity, margin) VALUES (?, ?, ?, ?)', (datetime.now().strftime("%Y-%m-%d %H:%M"), b, e, m))
    def update_news(self, events):
        with sqlite3.connect(self.db_name) as conn:
            conn.execute("DELETE FROM upcoming_news")
            for dt, title, country in events:
                conn.execute("INSERT INTO upcoming_news VALUES (?,?,?) ON CONFLICT(time) DO UPDATE SET "
                             "title = upcoming_news.title || '; ' || excluded.title, "
                             "country = upcoming_news.country || ',' || excluded.country",
                             (dt.isoformat(), title, country))
    def get_control_command(self):
        with sqlite3.connect(self.db_name) as conn:
            res = conn.execute("SELECT command FROM bot_control WHERE id = 1").fetchone()
            return res[0] if res else "START"
    def set_control_command(self, cmd):
        with sqlite3.connect(self.db_name) as conn:
            conn.execute("UPDATE bot_control SET command = ? WHERE id = 1", (cmd,))
    def set_bot_status(self, s):
        with sqlite3.connect(self.db_name) as conn: conn.execute("UPDATE bot_control SET status = ? WHERE id = 1", (s,))

    # --- Risk State ---
    def get_risk_state(self):
        with sqlite3.connect(self.db_name) as conn:
            row = conn.execute("SELECT peak_equity, day_date, day_start_equity, emergency_stopped FROM risk_state WHERE id = 1").fetchone()
            return {"peak_equity": row[0], "day_date": row[1], "day_start_equity": row[2], "emergency_stopped": bool(row[3])}
    def update_peak_equity(self, equity):
        with sqlite3.connect(self.db_name) as conn:
            conn.execute("UPDATE risk_state SET peak_equity = MAX(peak_equity, ?) WHERE id = 1", (equity,))
    def update_daily_baseline(self, today_str, equity):
        """Set day_start_equity once per calendar day."""
        with sqlite3.connect(self.db_name) as conn:
            current = conn.execute("SELECT day_date FROM risk_state WHERE id = 1").fetchone()
            if current[0] != today_str:
                conn.execute("UPDATE risk_state SET day_date = ?, day_start_equity = ? WHERE id = 1", (today_str, equity))
    def set_emergency_stopped(self, stopped):
        with sqlite3.connect(self.db_name) as conn:
            conn.execute("UPDATE risk_state SET emergency_stopped = ? WHERE id = 1", (1 if stopped else 0,))
    def init_peak_equity(self, equity):
        """Initialize peak equity if it's still 0 (first run)."""
        with sqlite3.connect(self.db_name) as conn:
            conn.execute("UPDATE risk_state SET peak_equity = ? WHERE id = 1 AND peak_equity = 0", (equity,))

    # --- Dynamic Risk ---
    def get_dynamic_risk(self, symbol, default_risk, fraction, min_r, max_r):
        with sqlite3.connect(self.db_name) as conn:
            df = pd.read_sql_query("SELECT profit FROM trades WHERE symbol = ? AND profit IS NOT NULL ORDER BY close_time DESC LIMIT 50", conn, params=(symbol,))
            if len(df) < 10: return default_risk
            wins, losses = df[df['profit'] > 0]['profit'], abs(df[df['profit'] < 0]['profit'])
            if losses.empty or wins.empty: return default_risk
            kelly = (len(wins)/len(df)) - (1 - (len(wins)/len(df))) / (wins.mean()/losses.mean())
            return max(min(kelly * fraction * 100, max_r), min_r)
    def get_stats(self):
        with sqlite3.connect(self.db_name) as conn:
            df = pd.read_sql_query("SELECT profit FROM trades WHERE profit IS NOT NULL", conn)
            if df.empty: return None
            return {"total_trades": len(df), "win_rate": (len(df[df['profit']>0])/len(df))*100, "total_profit": df['profit'].sum()}
    def get_open_trade_tickets(self):
        with sqlite3.connect(self.db_name) as conn:
            rows = conn.execute("SELECT ticket FROM trades WHERE close_time IS NULL").fetchall()
            return [r[0] for r in rows]

# --- UTILS ---
def send_telegram_message(m):
    if not CONFIG["enable_telegram"]: return
    try: requests.post(f"https://api.telegram.org/bot{CONFIG['telegram_token']}/sendMessage", json={"chat_id": CONFIG['telegram_chat_id'], "text": m, "parse_mode": "HTML"}, timeout=5)
    except Exception as e: logger.debug(f"Telegram send failed: {e}")
# --- SHARED STRATEGY ENGINE ---
from strategy_engine import calculate_rsi, calculate_atr, StrategyFactory, BaseStrategy, MACrossoverRSI, BBMeanReversion

# --- CORE BOT ---
class TradingBot:
    def __init__(self, config):
        self.config = config
        self.db = DatabaseManager(config["db_name"])
        self.news = NewsFilter(config["news_url"]) if config.get("enable_news_filter") else None
        self.load_optimized_settings()
        self.strategies = {s: StrategyFactory.get_strategy(d["strategy"]) for s, d in config["portfolio"].items()}
        self.daily_report_sent, self.is_paused, self.last_tg_id = False, False, 0
        self.last_hb, self.last_eq = datetime.now(), datetime.now() - timedelta(minutes=config["equity_log_interval_mins"])
        self.last_trade = {s: datetime.now() - timedelta(hours=1) for s in config["portfolio"]}
        self.streak_counts = {s: 0 for s in config["portfolio"]}
        self.streak_pauses = {s: datetime.now() - timedelta(days=1) for s in config["portfolio"]}
        self.last_reload = datetime.now()

    def get_setting(self, s, k): return self.config["portfolio"][s].get(k, self.config.get(k))

    def get_filling_mode(self, symbol):
        si = mt5.symbol_info(symbol)
        if si is None: return mt5.ORDER_FILLING_IOC
        if si.filling_mode & 1: return mt5.ORDER_FILLING_FOK
        if si.filling_mode & 2: return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN

    def load_optimized_settings(self):
        if not self.config.get("load_optimized_settings", True): return
        import json
        for s in self.config["portfolio"]:
            f = f"settings_{s}.json"
            if os.path.exists(f):
                try:
                    with open(f, "r") as j:
                        o = json.load(j)
                        update_dict = {}
                        for k in ["fast_ma", "slow_ma", "bb_window", "bb_std"]:
                            if k in o and o[k] is not None: update_dict[k] = o[k]
                        if "rsi_up" in o and o["rsi_up"] is not None: update_dict["rsi_upper"] = o["rsi_up"]
                        if "rsi_low" in o and o["rsi_low"] is not None: update_dict["rsi_lower"] = o["rsi_low"]
                        self.config["portfolio"][s].update(update_dict)
                except Exception as e:
                    logger.warning(f"Error loading settings for {s}: {e}")

    def initialize(self):
        if self.config.get("use_terminal_session"):
            path = self.config.get("terminal_path")
            connected = mt5.initialize(path, timeout=10000) if path else mt5.initialize(timeout=10000)
        else:
            if not self.config["login"] or not self.config["password"] or not self.config["server"]:
                logger.error("Set MT5_LOGIN, MT5_PASSWORD, and MT5_SERVER, or use demo_test.py with a logged-in demo terminal.")
                return False
            connected = mt5.initialize(login=self.config["login"], password=self.config["password"], server=self.config["server"])
        if not connected: return False
        if not self.account_is_allowed(): return False
        acc = mt5.account_info()
        if acc:
            self.db.init_peak_equity(acc.equity)
            self.db.update_daily_baseline(datetime.now().strftime("%Y-%m-%d"), acc.equity)
        for s in self.config["portfolio"]: mt5.symbol_select(s, True)
        self.reconcile_trades()
        return True

    def account_is_allowed(self):
        """Pin a demo test to its verified account; never follow an account switch."""
        if not self.config.get("demo_only"):
            return True
        acc = mt5.account_info()
        allowed = (acc is not None and acc.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO
                   and acc.login == self.config.get("expected_login")
                   and acc.server == self.config.get("expected_server"))
        if not allowed:
            logger.error("Demo account missing or changed. Blocking broker operations.")
        return allowed

    def send_order(self, request):
        if not self.account_is_allowed():
            return None
        if self.config.get("demo_only"):
            request = dict(request, comment="DemoTest-v1")
        return mt5.order_send(request)

    def is_in_session(self):
        now = datetime.now().time()
        for sess in self.config["trading_sessions"]:
            s, e = datetime.strptime(sess["start"], "%H:%M").time(), datetime.strptime(sess["end"], "%H:%M").time()
            if s <= now <= e: return True
        return False

    def check_friday_close(self):
        now = datetime.now()
        if now.weekday() == 4 and now.hour >= self.config["friday_close_hour"]:
            send_telegram_message("🏁 <b>Friday Close</b>"); self.close_all_positions(); return True
        return False

    def check_emergency_stop(self):
        """Hard stop: measures drawdown from peak equity. Sets persistent flag and retries close until flat."""
        acc = mt5.account_info()
        if not acc: return False
        risk = self.db.get_risk_state()
        if risk["emergency_stopped"]:
            # Ensure all bot positions are closed; retry if any remain open
            open_bot_pos = [p for p in (mt5.positions_get() or []) if p.magic == self.config["magic_number"]]
            if open_bot_pos:
                logger.warning(f"Emergency stop active: retrying closure of {len(open_bot_pos)} open positions...")
                self.close_all_positions()
            return True

        # Update peak equity and daily baseline
        self.db.update_peak_equity(acc.equity)
        self.db.update_daily_baseline(datetime.now().strftime("%Y-%m-%d"), acc.equity)
        # Refresh after update
        risk = self.db.get_risk_state()
        if risk["peak_equity"] > 0:
            drawdown_pct = (risk["peak_equity"] - acc.equity) / risk["peak_equity"] * 100
            if drawdown_pct >= self.config["max_drawdown_close_all"]:
                logger.critical(f"EMERGENCY STOP TRIGGERED: {drawdown_pct:.1f}% drawdown from peak ${risk['peak_equity']:.2f}")
                send_telegram_message(f"🚨 <b>EMERGENCY STOP</b>\nDrawdown: {drawdown_pct:.1f}% from peak ${risk['peak_equity']:.2f}")
                self.close_all_positions()
                self.db.set_emergency_stopped(True)
                self.db.set_control_command("STOPPED")
                self.is_paused = True
                return True
        return False

    def check_risk_limits(self):
        """Soft block: prevents new entries when daily or drawdown limits are hit."""
        acc = mt5.account_info()
        if not acc: return False  # False = entries NOT allowed
        risk = self.db.get_risk_state()
        # Soft drawdown check (max_drawdown_percent)
        if risk["peak_equity"] > 0:
            dd_pct = (risk["peak_equity"] - acc.equity) / risk["peak_equity"] * 100
            if dd_pct >= self.config["max_drawdown_percent"]:
                logger.warning(f"Soft drawdown limit: {dd_pct:.1f}% >= {self.config['max_drawdown_percent']}% — blocking new entries")
                return False
        # Daily loss limit
        if risk["day_start_equity"] > 0:
            daily_loss = risk["day_start_equity"] - acc.equity
            if daily_loss >= self.config["daily_loss_limit"]:
                logger.warning(f"Daily loss limit hit: ${daily_loss:.2f} >= ${self.config['daily_loss_limit']:.2f}")
                return False
            # Daily profit target
            daily_profit = acc.equity - risk["day_start_equity"]
            if daily_profit >= self.config["daily_profit_target"]:
                logger.info(f"Daily profit target reached: ${daily_profit:.2f} >= ${self.config['daily_profit_target']:.2f}")
                return False
        return True  # True = entries allowed

    def check_basket_targets(self):
        acc = mt5.account_info()
        if acc:
            if acc.profit >= self.config["basket_tp_currency"]:
                send_telegram_message(f"💰 <b>Basket TP: {acc.profit:.2f}</b>"); self.close_all_positions()
            elif acc.profit <= self.config["basket_sl_currency"]:
                send_telegram_message(f"📉 <b>Basket SL: {acc.profit:.2f}</b>"); self.close_all_positions()

    def close_all_positions(self):
        for s in self.config["portfolio"]:
            pos = mt5.positions_get(symbol=s)
            if pos:
                for p in pos:
                    if p.magic != self.config["magic_number"]: continue
                    t = mt5.symbol_info_tick(s)
                    self.close_position(p, t.bid if p.type == mt5.POSITION_TYPE_BUY else t.ask)

    def check_remote_commands(self):
        if self.config["enable_telegram"]:
            try:
                res = requests.get(f"https://api.telegram.org/bot{self.config['telegram_token']}/getUpdates", params={"offset": self.last_tg_id + 1, "timeout": 1}).json()
                if res.get("ok"):
                    for u in res.get("result", []):
                        self.last_tg_id = u["update_id"]
                        msg = u.get("message", {})
                        if msg.get("chat", {}).get("id") != int(self.config["telegram_chat_id"]): continue
                        txt = msg.get("text", "")
                        if txt == "/status":
                            acc = mt5.account_info()
                            eq_str = f"{acc.equity:.2f}" if acc else "N/A"
                            status_str = "STOPPED" if self.db.get_risk_state()["emergency_stopped"] else ("PAUSED" if self.is_paused else "RUNNING")
                            send_telegram_message(f"ℹ️ Status: {status_str}\nEquity: {eq_str}")
                        elif txt == "/pause":
                            self.is_paused = True
                            self.db.set_control_command("PAUSE")
                        elif txt == "/resume":
                            self.is_paused = False
                            self.db.set_emergency_stopped(False)
                            self.db.set_control_command("START")
                            send_telegram_message("✅ <b>Bot Resumed</b>: Operating state set to START.")
                        elif txt == "/closeall":
                            self.close_all_positions()
                            self.is_paused = True
                            self.db.set_control_command("STOPPED")
            except Exception as e: logger.warning(f"Telegram poll error: {e}")

        cmd = self.db.get_control_command()
        if cmd == "STOPPED":
            self.is_paused = True
        elif cmd == "PAUSE":
            self.is_paused = True
        elif cmd in ("START", "RESUME"):
            self.is_paused = False
            self.db.set_emergency_stopped(False)
            if cmd == "RESUME":
                self.db.set_control_command("START")
        elif cmd == "CLOSE_ALL":
            self.close_all_positions()
            self.is_paused = True
            self.db.set_control_command("STOPPED")
            send_telegram_message("🚨 <b>PANIC EXIT</b> — all positions closed. Send RESUME to restart.")
        elif "MANUAL_" in cmd:
            prior_paused = self.is_paused
            prior_risk = self.db.get_risk_state()
            if "CLOSE:" in cmd:
                ticket = int(cmd.split(":")[1])
                pos = mt5.positions_get(ticket=ticket)
                if pos:
                    logger.warning(f"Manual Close requested for ticket {ticket}")
                    t = mt5.symbol_info_tick(pos[0].symbol)
                    self.close_position(pos[0], t.bid if pos[0].type == mt5.POSITION_TYPE_BUY else t.ask)
                    send_telegram_message(f"✅ <b>Manual Close</b>: Ticket {ticket} closed.")
            else:
                action, symbol = cmd.split(":")
                if not self.can_open_trade(symbol, is_manual=True):
                    logger.warning(f"Manual {action} {symbol} rejected by entry validation checks")
                else:
                    df = pd.DataFrame(mt5.copy_rates_from_pos(symbol, self.config["timeframe"], 0, 50))
                    if not df.empty:
                        atr = calculate_atr(df, self.get_setting(symbol, "atr_period")).iloc[-1]
                        if self.open_trade(symbol, mt5.ORDER_TYPE_BUY if "BUY" in action else mt5.ORDER_TYPE_SELL, atr):
                            self.last_trade[symbol] = datetime.now()

            # Preserve existing operating state: do NOT blindly reset to START!
            if prior_risk.get("emergency_stopped"):
                self.db.set_control_command("STOPPED")
                self.is_paused = True
            elif prior_paused:
                self.db.set_control_command("PAUSE")
                self.is_paused = True
            else:
                self.db.set_control_command("START")
                self.is_paused = False

        self.db.set_bot_status("STOPPED" if self.db.get_risk_state()["emergency_stopped"] else ("PAUSED" if self.is_paused else "ACTIVE"))

    def can_open_trade(self, symbol, is_manual=False):
        """
        Unified entry validator for both automated and manual entry paths.
        Validates pause state, emergency stops, trading sessions, news events,
        loss streak pauses, cooldown, max position counts, spreads, portfolio correlation,
        and financial risk limits.
        """
        # 1. Pause & Emergency stop
        if self.is_paused or self.db.get_risk_state()["emergency_stopped"]:
            logger.warning(f"Trade entry rejected for {symbol}: Bot is paused or emergency stopped.")
            return False

        # 2. Trading session
        if not self.is_in_session():
            logger.warning(f"Trade entry rejected for {symbol}: Outside configured trading session.")
            return False

        # 3. News blackout
        if self.config.get("enable_news_filter") and self.news:
            active, et = self.news.is_news_active(self.db, self.config["news_timezone_offset"], self.config["news_pause_mins"])
            if active:
                reason = "calendar unavailable" if et is None else f"high-impact event at {et.isoformat()}"
                if getattr(self, "last_news_block", None) != reason:
                    logger.warning(f"New entries blocked: {reason}. Existing positions remain managed.")
                    self.last_news_block = reason
                return False
            self.last_news_block = None

        # 4. Streak pause
        if datetime.now() < self.streak_pauses.get(symbol, datetime.min):
            logger.warning(f"Trade entry rejected for {symbol}: In consecutive loss-streak pause.")
            return False

        # 5. Trade cooldown
        if datetime.now() < self.last_trade.get(symbol, datetime.min) + timedelta(minutes=self.config["trade_cooldown_minutes"]):
            logger.warning(f"Trade entry rejected for {symbol}: Trade cooldown active.")
            return False

        # 6. Max total positions
        own_positions = [p for p in (mt5.positions_get() or []) if p.magic == self.config["magic_number"]]
        if len(own_positions) >= self.config["max_total_positions"]:
            logger.warning(f"Trade entry rejected for {symbol}: Max positions ({self.config['max_total_positions']}) reached.")
            return False

        # 7. Spread check
        tick = mt5.symbol_info_tick(symbol)
        si = mt5.symbol_info(symbol)
        if not tick or not si:
            logger.warning(f"Trade entry rejected for {symbol}: Symbol info or market tick unavailable.")
            return False
        if (tick.ask - tick.bid) / si.point > self.config["max_spread_points"]:
            logger.warning(f"Trade entry rejected for {symbol}: Spread ({((tick.ask - tick.bid) / si.point):.1f}) exceeds max points ({self.config['max_spread_points']}).")
            return False

        # 8. Portfolio correlation
        if not self.check_correlation(symbol):
            logger.warning(f"Trade entry rejected for {symbol}: Correlation limit exceeded.")
            return False

        # 9. Financial risk limits
        if not self.check_risk_limits():
            logger.warning(f"Trade entry rejected for {symbol}: Financial risk limit reached.")
            return False

        return True

    def reconcile_trades(self):
        """
        Reconcile open trades in SQLite against broker deal history.
        Detects positions closed by broker SL/TP or manual interventions.
        Accounts for all deals (entry + exit), commissions, swaps, and fees.
        Safely retries on MT5 query errors without creating false trade results.
        """
        open_tickets = self.db.get_open_trade_tickets()
        if not open_tickets:
            return

        for ticket in open_tickets:
            pos = mt5.positions_get(ticket=ticket)
            # 1. API error handling: MT5 returns None on query errors
            if pos is None:
                logger.warning(f"Failed to query position {ticket}: {mt5.last_error()}. Retrying next cycle.")
                continue

            # 2. Position is still open at broker: nothing to reconcile
            if len(pos) > 0:
                continue

            # 3. Position is confirmed closed (pos == ())
            from_date = datetime.now() - timedelta(days=90)
            deals = mt5.history_deals_get(from_date, datetime.now() + timedelta(days=1), position=ticket)
            if deals is None:
                logger.warning(f"Failed to fetch deal history for position {ticket}: {mt5.last_error()}. Retrying next cycle.")
                continue

            if len(deals) == 0:
                logger.warning(f"Position {ticket} closed at broker but deals not yet synchronized. Retrying next cycle.")
                continue

            # 4. Aggregate ALL deals for this position (entry + exits)
            total_profit = sum(d.profit for d in deals)
            total_comm = sum(d.commission for d in deals)
            total_swap = sum(d.swap for d in deals)
            total_fee = sum(d.fee for d in deals)
            net_profit = total_profit + total_comm + total_swap + total_fee

            out_deals = [d for d in deals if d.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY)]
            if out_deals:
                last_deal = out_deals[-1]
                close_price = last_deal.price
                close_time = datetime.fromtimestamp(last_deal.time).isoformat()
            else:
                last_deal = deals[-1]
                close_price = last_deal.price
                close_time = datetime.fromtimestamp(last_deal.time).isoformat()

            self.db.log_trade_close(ticket, close_price, net_profit, total_comm, total_swap, total_fee, close_time=close_time)
            symbol = deals[0].symbol if deals else "UNKNOWN"
            logger.info(f"Reconciled trade {ticket} ({symbol}): closed at {close_price} ({close_time}), net P&L = ${net_profit:.2f} (profit={total_profit:.2f}, comm={total_comm:.2f}, swap={total_swap:.2f}, fee={total_fee:.2f})")

            # Update streak tracking
            if symbol in self.streak_counts:
                if net_profit < 0:
                    self.streak_counts[symbol] += 1
                    if self.streak_counts[symbol] >= self.config["max_consecutive_losses"]:
                        self.streak_pauses[symbol] = datetime.now() + timedelta(hours=self.config["streak_pause_hours"])
                        send_telegram_message(f"⚠️ <b>Streak Pause</b>: {symbol} ({self.streak_counts[symbol]} consecutive losses)")
                else:
                    self.streak_counts[symbol] = 0

    def run(self):
        logger.info("Bot starting Portfolio Engine...")
        while True:
            try:
                terminal = mt5.terminal_info()
                if terminal is None or not terminal.connected:
                    if not self.initialize(): time.sleep(10); continue
                if not self.account_is_allowed():
                    self.db.set_bot_status("ACCOUNT_BLOCKED")
                    time.sleep(5)
                    continue
                if self.check_friday_close(): time.sleep(3600); continue

                # Check emergency stop; keep engine running for recovery even if stopped
                is_emergency = self.check_emergency_stop()
                self.check_remote_commands()
                self.check_basket_targets()
                self.reconcile_trades()

                now = datetime.now()
                if now > self.last_reload + timedelta(minutes=5): self.load_optimized_settings(); self.last_reload = now
                if now > self.last_hb + timedelta(hours=self.config["heartbeat_interval_hours"]):
                    send_telegram_message(f"❤️ Heartbeat\nEquity: {mt5.account_info().equity:.2f}"); self.last_hb = now
                if now > self.last_eq + timedelta(minutes=self.config["equity_log_interval_mins"]):
                    acc = mt5.account_info()
                    if acc: self.db.log_equity(acc.balance, acc.equity, acc.margin); self.last_eq = now

                # 1. ALWAYS manage active positions (trailing stops / breakeven)
                for s in self.config["portfolio"]:
                    tick = mt5.symbol_info_tick(s)
                    if not tick: continue
                    pos = mt5.positions_get(symbol=s)
                    if pos:
                        df_m = pd.DataFrame(mt5.copy_rates_from_pos(s, self.config["timeframe"], 0, 50))
                        if not df_m.empty:
                            atr = calculate_atr(df_m, self.get_setting(s, "atr_period")).iloc[-1]
                            for p in pos:
                                if p.magic == self.config["magic_number"]:
                                    self.apply_management(p, tick, atr)

                # 2. Check if new entries are allowed (block if emergency stop or paused)
                if not is_emergency and not self.is_paused:
                    for s in self.config["portfolio"]:
                        self.process_symbol(s)

                time.sleep(5)
            except KeyboardInterrupt: break
            except Exception as e: logger.error(f"Loop error: {e}"); time.sleep(10)

    def get_trend_status(self, symbol):
        if not self.config["enable_mtf"]: return None
        df = pd.DataFrame(mt5.copy_rates_from_pos(symbol, self.config["trend_timeframe"], 0, self.config["trend_period"] + 2))
        if df.empty or len(df) < self.config["trend_period"]: return None
        # Use completed H1 candle and convert to Python bool (not NumPy bool_)
        return bool(df['close'].iloc[-2] > df['close'].rolling(window=self.config["trend_period"]).mean().iloc[-2])

    def check_correlation(self, symbol):
        pos = mt5.positions_get()
        if not pos: return True
        # Only consider our own positions (magic number filter)
        own_pos = [p for p in pos if p.magic == self.config["magic_number"]]
        if not own_pos: return True
        syms = list(set([p.symbol for p in own_pos]))
        if symbol in syms: return True
        data = {}
        for s in syms + [symbol]:
            r = mt5.copy_rates_from_pos(s, self.config["correlation_timeframe"], 0, 51)
            if r is not None:
                closes = pd.Series([x['close'] for x in r])
                data[s] = closes.pct_change().dropna().values
        if len(data) < 2: return True
        df_corr = pd.DataFrame(data).corr()
        for s in syms:
            if s in df_corr.columns and symbol in df_corr.columns:
                if abs(df_corr[symbol][s]) > self.config["max_correlation"]: return False
        return True

    def process_symbol(self, symbol):
        mt5.symbol_select(symbol, True)
        if not self.can_open_trade(symbol, is_manual=False):
            return
        tick = mt5.symbol_info_tick(symbol)
        if not tick: return
        df = pd.DataFrame(mt5.copy_rates_from_pos(symbol, self.config["timeframe"], 0, 200))
        if df.empty: return
        trend = self.get_trend_status(symbol)
        sig = self.strategies[symbol].get_signal(symbol, df, trend, self)
        atr = calculate_atr(df, self.get_setting(symbol, "atr_period")).iloc[-1]
        sym_positions = [p for p in (mt5.positions_get(symbol=symbol) or []) if p.magic == self.config["magic_number"]]
        if sig == "BUY":
            for p in [p for p in sym_positions if p.type == mt5.POSITION_TYPE_SELL]: self.close_position(p, tick.ask)
            if not any(p.type == mt5.POSITION_TYPE_BUY for p in sym_positions):
                if self.open_trade(symbol, mt5.ORDER_TYPE_BUY, atr): self.last_trade[symbol] = datetime.now()
        elif sig == "SELL":
            for p in [p for p in sym_positions if p.type == mt5.POSITION_TYPE_BUY]: self.close_position(p, tick.bid)
            if not any(p.type == mt5.POSITION_TYPE_SELL for p in sym_positions):
                if self.open_trade(symbol, mt5.ORDER_TYPE_SELL, atr): self.last_trade[symbol] = datetime.now()

    def apply_management(self, pos, tick, atr):
        pnt = mt5.symbol_info(pos.symbol).point
        be_thresh = atr * self.get_setting(pos.symbol, "breakeven_atr_mult")
        prof = (tick.bid - pos.price_open) if pos.type == mt5.POSITION_TYPE_BUY else (pos.price_open - tick.ask)
        sl_behind = (pos.sl <= pos.price_open) if pos.type == mt5.POSITION_TYPE_BUY else (pos.sl >= pos.price_open or pos.sl == 0)
        if sl_behind and prof >= be_thresh:
            new_sl = pos.price_open + (5 * pnt) if pos.type == mt5.POSITION_TYPE_BUY else pos.price_open - (5 * pnt)
            self.send_order({"action": mt5.TRADE_ACTION_SLTP, "position": pos.ticket, "symbol": pos.symbol, "sl": float(new_sl), "tp": float(pos.tp)})
            return
        t_dist, t_step = self.config["trailing_stop_points"] * pnt, self.config["trailing_step_points"] * pnt
        if pos.type == mt5.POSITION_TYPE_BUY:
            new_sl = tick.bid - t_dist
            if new_sl > pos.sl + t_step and new_sl > pos.price_open:
                self.send_order({"action": mt5.TRADE_ACTION_SLTP, "position": pos.ticket, "symbol": pos.symbol, "sl": float(new_sl), "tp": float(pos.tp)})
        elif pos.type == mt5.POSITION_TYPE_SELL:
            new_sl = tick.ask + t_dist
            if (pos.sl == 0 or new_sl < pos.sl - t_step) and new_sl < pos.price_open:
                self.send_order({"action": mt5.TRADE_ACTION_SLTP, "position": pos.ticket, "symbol": pos.symbol, "sl": float(new_sl), "tp": float(pos.tp)})

    def open_trade(self, symbol, order_type, atr):
        sl_dist = atr * self.get_setting(symbol, "sl_atr_multiplier")
        tp_dist = atr * self.get_setting(symbol, "tp_atr_multiplier")
        risk_pct = self.get_setting(symbol, "risk_percent")
        if self.config["enable_dynamic_risk"]: risk_pct = self.db.get_dynamic_risk(symbol, risk_pct, self.config["kelly_fraction"], self.config["min_risk_percent"], self.config["max_risk_percent"])
        acc, si = mt5.account_info(), mt5.symbol_info(symbol)
        if not acc or not si: return False

        # Calculate lot size: floor to volume step to never exceed risk allocation
        raw_lot = (acc.equity * (risk_pct / 100)) / ((sl_dist / si.trade_tick_size) * si.trade_tick_value)
        lot = math.floor(raw_lot / si.volume_step) * si.volume_step

        if lot < si.volume_min:
            logger.warning(f"Order skipped for {symbol}: calculated lot ({lot:.3f}) is below broker min ({si.volume_min}). Budget protection enforced.")
            return False
        lot = min(lot, si.volume_max)

        tick = mt5.symbol_info_tick(symbol)
        if not tick: return False
        price = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid
        sl, tp = (price - sl_dist, price + tp_dist) if order_type == mt5.ORDER_TYPE_BUY else (price + sl_dist, price - tp_dist)
        if mt5.order_calc_margin(order_type, symbol, lot, price) > (acc.margin_free * 0.7):
            logger.warning(f"Order skipped for {symbol}: insufficient free margin.")
            return False
        res = self.send_order({"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": float(lot), "type": order_type, "price": price, "sl": float(sl), "tp": float(tp), "magic": self.config["magic_number"], "type_filling": self.get_filling_mode(symbol)})
        if res is None:
            logger.error(f"Order Send failed for {symbol}: {mt5.last_error()}"); return False
        if res.retcode == mt5.TRADE_RETCODE_DONE:
            self.db.log_trade_open(res.order, symbol, "BUY" if order_type == mt5.ORDER_TYPE_BUY else "SELL", lot, price, sl, tp)
            send_telegram_message(f"🔔 <b>Trade Opened</b>\n{symbol} @ {price}"); return True
        else:
            logger.error(f"Trade rejected for {symbol}: retcode={res.retcode}, comment={res.comment}")
        return False

    def close_position(self, pos, price):
        res = self.send_order({"action": mt5.TRADE_ACTION_DEAL, "symbol": pos.symbol, "volume": pos.volume, "type": mt5.ORDER_TYPE_BUY if pos.type == mt5.POSITION_TYPE_SELL else mt5.ORDER_TYPE_SELL, "position": pos.ticket, "price": price, "type_filling": self.get_filling_mode(pos.symbol)})
        if res is None:
            logger.error(f"Close Order failed for {pos.symbol}: {mt5.last_error()}"); return False
        if res.retcode == mt5.TRADE_RETCODE_DONE:
            time.sleep(0.1)
            deals = mt5.history_deals_get(position=pos.ticket)
            if deals:
                total_profit = sum(d.profit for d in deals)
                total_comm = sum(d.commission for d in deals)
                total_swap = sum(d.swap for d in deals)
                total_fee = sum(d.fee for d in deals)
                net_profit = total_profit + total_comm + total_swap + total_fee
                out_deals = [d for d in deals if d.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY)]
                close_time = datetime.fromtimestamp(out_deals[-1].time).isoformat() if out_deals else datetime.now().isoformat()
                self.db.log_trade_close(pos.ticket, price, net_profit, total_comm, total_swap, total_fee, close_time=close_time)
            else:
                net_profit = 0.0
                self.db.log_trade_close(pos.ticket, price, 0.0)

            if net_profit < 0:
                self.streak_counts[pos.symbol] += 1
                if self.streak_counts[pos.symbol] >= self.config["max_consecutive_losses"]:
                    self.streak_pauses[pos.symbol] = datetime.now() + timedelta(hours=self.config["streak_pause_hours"])
                    send_telegram_message(f"⚠️ <b>Streak Pause</b>: {pos.symbol}")
            else: self.streak_counts[pos.symbol] = 0
            return True
        else:
            logger.error(f"Close rejected for {pos.symbol}: retcode={res.retcode}, comment={res.comment}")
            return False

# --- NEWS FILTER ---
class NewsFilter:
    REFRESH_INTERVAL = timedelta(hours=6)
    RETRY_INTERVAL = timedelta(minutes=15)

    def __init__(self, url):
        self.url = url
        self.high_impact_events = []
        self.last_fetch = None
        self.next_attempt = datetime.min.replace(tzinfo=timezone.utc)

    def fetch_events(self, db, offset):
        # JSON dates carry their own offsets; the legacy numeric offset is ignored.
        now = datetime.now(timezone.utc)
        if now < self.next_attempt:
            return False
        self.next_attempt = now + self.RETRY_INTERVAL
        try:
            resp = requests.get(self.url, timeout=10)
            if resp.status_code != 200:
                raise ValueError(f"HTTP {resp.status_code}")
            payload = resp.json()
            if not isinstance(payload, list) or not payload:
                raise ValueError("empty or invalid calendar export")
            db_events, dates = [], []
            for event in payload:
                dt = datetime.fromisoformat(event["date"])
                if dt.tzinfo is None:
                    raise ValueError("calendar event has no timezone")
                dt = dt.astimezone(timezone.utc)
                dates.append(dt)
                if event["impact"] == "High":
                    db_events.append((dt, event["title"], event["country"]))
            if max(dates) < now - timedelta(days=1) or min(dates) > now + timedelta(days=7):
                raise ValueError("calendar export is outside the current week")
            db.update_news(db_events)
            self.high_impact_events = [dt for dt, _, _ in db_events]
            self.last_fetch = now
            self.next_attempt = now + self.REFRESH_INTERVAL
            logger.info(f"News calendar loaded: {len(db_events)} high-impact events; times normalized to UTC.")
            return True
        except Exception as e:
            logger.warning(f"News calendar unavailable ({e}); retry in 15 minutes. "
                           "New entries require a fresh calendar.")
            return False

    def is_news_active(self, db, offset, pause_mins):
        now = datetime.now(timezone.utc)
        if self.last_fetch is None or now >= self.last_fetch + self.REFRESH_INTERVAL:
            self.fetch_events(db, offset)
        if self.last_fetch is None or now >= self.last_fetch + self.REFRESH_INTERVAL:
            return True, None  # Unavailable is not equivalent to "no news".
        for et in self.high_impact_events:
            if et - timedelta(minutes=pause_mins) <= now <= et + timedelta(minutes=pause_mins): return True, et
        return False, None

if __name__ == "__main__":
    bot = TradingBot(CONFIG)
    if bot.initialize():
        try:
            bot.run()
        except KeyboardInterrupt:
            logger.info("Bot stopped by user.")
        finally:
            send_telegram_message("🛑 <b>Bot Shutting Down</b>")
            mt5.shutdown()
            logger.info("MT5 connection closed.")

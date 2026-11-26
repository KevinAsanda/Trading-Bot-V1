"""Check a logged-in MT5 demo terminal, or run the isolated demo profile."""
import argparse
import copy
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import time

import MetaTrader5 as mt5

ROOT = Path(__file__).resolve().parent
MAGIC = 2601013


def confirm_quote_updates(symbol, initial_tick, attempts=15):
    """Prove feed liveness locally instead of guessing the server clock offset.

    A timestamp alone may use a different clock basis. Require a new tick whose
    timestamp advances at a plausible rate during our monotonic observation.
    A large jump only establishes a new baseline; it does not pass the check.
    """
    previous = initial_tick
    observed_at = time.monotonic()
    for _ in range(attempts):
        time.sleep(1)
        latest = mt5.symbol_info_tick(symbol)
        if latest is None or latest.bid <= 0 or latest.ask < latest.bid:
            raise RuntimeError("Quote unavailable or invalid while checking feed updates. Retry after reconnecting MT5.")
        previous_ms = getattr(previous, "time_msc", previous.time * 1000)
        latest_ms = getattr(latest, "time_msc", latest.time * 1000)
        elapsed = time.monotonic() - observed_at
        advance = (latest_ms - previous_ms) / 1000
        if 0 < advance <= elapsed + 5:
            print("Quote feed: fresh tick observed.")
            return latest
        if advance != 0:
            previous = latest
            observed_at = time.monotonic()
    raise RuntimeError(f"No fresh quote progression observed within {attempts}s. "
                       "Check MT5 connectivity and retry during market hours.")


def preflight(symbol, terminal_path=None):
    connected = (mt5.initialize(str(terminal_path), timeout=10000) if terminal_path
                 else mt5.initialize(timeout=10000))
    if not connected:
        raise RuntimeError("Cannot connect to desktop MT5. Open it, log into your demo account, "
                           "and retry (use --terminal-path if needed). " + str(mt5.last_error()))
    acc, terminal = mt5.account_info(), mt5.terminal_info()
    if acc is None or terminal is None or not terminal.connected:
        raise RuntimeError("MT5 is not connected to a trading account.")
    if acc.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
        raise RuntimeError("This launcher accepts DEMO accounts only. No orders were sent.")
    print(f"DEMO account: {acc.login} | server: {acc.server} | currency: {acc.currency}")
    print(f"Balance: {acc.balance:.2f} | equity: {acc.equity:.2f}")
    if not acc.trade_allowed or not acc.trade_expert:
        raise RuntimeError("Account does not allow automated trading; check login permissions with your broker.")
    if not terminal.trade_allowed or terminal.tradeapi_disabled:
        raise RuntimeError("Enable Algo Trading and allow external Python API trading in MT5's "
                           "Tools > Options > Expert Advisors settings.")
    if acc.equity <= 0:
        raise RuntimeError("Demo equity must be positive.")
    if not mt5.symbol_select(symbol, True):
        raise RuntimeError(f"Symbol {symbol!r} is unavailable. Use --symbol with its exact Market Watch name.")
    si, tick = mt5.symbol_info(symbol), mt5.symbol_info_tick(symbol)
    if si is None or tick is None or si.point <= 0 or tick.bid <= 0 or tick.ask < tick.bid:
        raise RuntimeError("Symbol metadata or quote is unavailable.")
    if si.trade_mode != mt5.SYMBOL_TRADE_MODE_FULL:
        raise RuntimeError("The selected symbol is not enabled for both buying and selling.")
    if min(si.volume_step, si.volume_min, si.trade_tick_size, si.trade_tick_value) <= 0:
        raise RuntimeError("Invalid broker volume or tick-value metadata.")
    offset = tick.time - time.time()
    print(f"Symbol: {symbol} | raw tick clock offset: {offset:+.0f}s (tick minus PC)")
    print("Checking for a live quote update (up to 15 seconds)...")
    tick = confirm_quote_updates(symbol, tick)
    spread = (tick.ask - tick.bid) / si.point
    print(f"Symbol: {symbol} | spread: {spread:.1f} points")
    if spread > 50:
        raise RuntimeError("Spread exceeds the test profile's 50-point limit. Retry later.")
    for timeframe, required, label in [(mt5.TIMEFRAME_M1, 200, "M1"), (mt5.TIMEFRAME_H1, 202, "H1")]:
        bars = mt5.copy_rates_from_pos(symbol, timeframe, 0, required)
        if bars is None or len(bars) < required:
            raise RuntimeError(f"Need {required} {label} bars. Open that chart in MT5, let history load, and retry.")
    positions, orders = mt5.positions_get(), mt5.orders_get()
    if positions is None or orders is None:
        raise RuntimeError("Cannot read positions/orders. No test run will start.")
    if orders or any(p.magic != MAGIC or p.symbol != symbol for p in positions):
        raise RuntimeError("Use a dedicated demo account without other positions or pending orders for this test.")
    print(f"Open test positions: {len(positions)} | minimum volume: {si.volume_min} lots")
    return acc


def build_profile(base, account, symbol, run_dir, terminal_path=None):
    cfg = copy.deepcopy(base)
    cfg.update(
        demo_only=True, use_terminal_session=True, expected_login=account.login,
        expected_server=account.server, terminal_path=terminal_path,
        portfolio={symbol: {"strategy": "MA_Crossover_RSI", "risk_percent": 0.25}},
        risk_percent=0.25, max_total_positions=1, enable_dynamic_risk=False,
        load_optimized_settings=False, enable_telegram=False, telegram_token="", telegram_chat_id="",
        magic_number=MAGIC, db_name=str(run_dir / "trading_data.db"),
        equity_log_interval_mins=1,
        daily_loss_limit=account.equity * 0.02, daily_profit_target=account.equity * 0.04,
        basket_sl_currency=-account.equity * 0.02, basket_tp_currency=account.equity * 0.04,
    )
    return cfg


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Preflight only; never sends orders (default).")
    mode.add_argument("--run", action="store_true", help="Run the strategy; can place DEMO orders.")
    parser.add_argument("--symbol", default="EURUSD", help="Exact symbol from Market Watch (default: EURUSD on MetaQuotes-Demo).")
    parser.add_argument("--terminal-path", help="Full path to the intended terminal64.exe.")
    args = parser.parse_args()
    lock = None
    bot = None
    try:
        account = preflight(args.symbol, args.terminal_path)
        if not args.run:
            print("Preflight passed. No orders sent. Use --run to start the demo strategy.")
            return 0
        key = hashlib.sha256(f"{account.server}:{account.login}".encode()).hexdigest()[:12]
        run_dir = ROOT / "demo_runs" / key
        run_dir.mkdir(parents=True, exist_ok=True)
        # OS lock is released even if Python crashes; prevents duplicate demo engines.
        import msvcrt
        lock = (run_dir / "engine.lock").open("a+b")
        if lock.seek(0, 2) == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError("A demo engine is already running for this account.") from None
        os.chdir(ROOT)
        os.environ["BOT_LOG_FILE"] = str(run_dir / "trading_bot.log")
        import connect_mt5 as core
        core.CONFIG["enable_telegram"] = False
        cfg = build_profile(core.CONFIG, account, args.symbol, run_dir, args.terminal_path)
        snapshot = {k: v for k, v in cfg.items() if k not in ("password", "telegram_token", "telegram_chat_id")}
        (run_dir / "profile.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        bot = core.TradingBot(cfg)
        if not bot.initialize():
            raise RuntimeError("Demo account verification failed during engine initialization.")
        command = bot.db.get_control_command()
        bot.is_paused = command != "START" or bot.db.get_risk_state()["emergency_stopped"]
        if bot.is_paused:
            print("Saved pause/stop preserved. Review the saved risk state before resuming.")
        print("DEMO engine: one symbol, max one position, fixed 0.25% risk, no Kelly sizing.")
        print(f"Records and log: {run_dir}")
        print("Watch Trade / History in MT5. Ctrl+C stops Python; it does not close open positions.")
        bot.run()
        return 0
    except KeyboardInterrupt:
        return 0
    except (RuntimeError, OSError) as exc:
        print(f"SETUP BLOCKED: {exc}", file=sys.stderr)
        return 1
    finally:
        if bot is not None:
            bot.db.set_bot_status("OFFLINE")
            print("Demo engine stopped. Any open positions remain at the broker.")
        mt5.shutdown()
        logging.shutdown()
        if lock is not None:
            lock.close()


if __name__ == "__main__":
    raise SystemExit(main())

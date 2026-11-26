import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import os
import json
from datetime import datetime

# --- SETTINGS ---
SYMBOL = "EURUSD#"
TIMEFRAME = mt5.TIMEFRAME_M1
BARS_COUNT = 10000

# Default Strategy Parameters (Matches connect_mt5.py)
FAST_MA = 10
SLOW_MA = 20
RSI_PERIOD = 14
RSI_UPPER = 70
RSI_LOWER = 30
SL_ATR_MULT = 1.5
TP_ATR_MULT = 3.0
BE_ATR_MULT = 1.0
ATR_PERIOD = 14
TRAILING_POINTS = 100
TRAILING_STEP = 20
SPREAD_POINTS = 15  # Typical XM Ultra Low spread in points (1.5 pips)

def load_optimized_settings(symbol):
    """Loads settings from settings_{symbol}.json if it exists."""
    global FAST_MA, SLOW_MA, RSI_UPPER, RSI_LOWER
    filename = f"settings_{symbol}.json"
    if os.path.exists(filename):
        try:
            with open(filename, "r") as f:
                opt = json.load(f)
                FAST_MA = opt.get("fast_ma", FAST_MA)
                SLOW_MA = opt.get("slow_ma", SLOW_MA)
                RSI_UPPER = opt.get("rsi_up", RSI_UPPER)
                RSI_LOWER = opt.get("rsi_low", RSI_LOWER)
            print(f"✅ Loaded optimized settings from {filename}")
        except Exception as e:
            print(f"⚠️ Error loading settings file: {e}")
    else:
        print(f"ℹ️ No optimized settings found for {symbol}. Using defaults.")

from strategy_engine import calculate_rsi, calculate_atr, StrategyFactory

def run_backtest():
    load_optimized_settings(SYMBOL)

    login = int(os.getenv("MT5_LOGIN") or 0)
    password = os.getenv("MT5_PASSWORD")
    server = os.getenv("MT5_SERVER")

    init_ok = mt5.initialize(login=login, password=password, server=server) if (login and password and server) else mt5.initialize()
    if not init_ok:
        print(f"MT5 Init failed: {mt5.last_error()}")
        return

    print(f"Fetching {BARS_COUNT} bars for {SYMBOL}...")
    rates = mt5.copy_rates_from_pos(SYMBOL, TIMEFRAME, 0, BARS_COUNT)
    si = mt5.symbol_info(SYMBOL)
    point = si.point if si else 0.00001
    mt5.shutdown()

    if rates is None:
        print("Failed to fetch historical rates")
        return

    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')

    # Calculate indicators
    df['fast_ma'] = df['close'].rolling(window=FAST_MA).mean()
    df['slow_ma'] = df['close'].rolling(window=SLOW_MA).mean()
    df['rsi'] = calculate_rsi(df['close'], RSI_PERIOD)
    df['atr'] = calculate_atr(df, ATR_PERIOD)

    # Completed candle signals (evaluated at bar i based on closed bars i-1 and i-2)
    warmup = max(SLOW_MA, RSI_PERIOD, ATR_PERIOD) + 5
    trades = []
    current_trade = None
    spread = SPREAD_POINTS * point

    for i in range(warmup, len(df)):
        bar = df.iloc[i]
        prev_bar = df.iloc[i-1]
        prev_prev_bar = df.iloc[i-2]

        # 1. Manage active position first (SL, TP, Breakeven, Trailing Stop)
        if current_trade is not None:
            pos_type = current_trade['type']
            open_price = current_trade['open_price']
            sl = current_trade['sl']
            tp = current_trade['tp']
            atr = current_trade['atr']
            be_threshold = atr * BE_ATR_MULT
            trail_dist = TRAILING_POINTS * point
            trail_step = TRAILING_STEP * point

            closed = False
            exit_price = 0.0
            exit_reason = ""

            if pos_type == 'BUY':
                # Breakeven check
                prof = bar['high'] - open_price
                if sl <= open_price and prof >= be_threshold:
                    sl = open_price + (5 * point)
                    current_trade['sl'] = sl

                # Trailing stop
                new_sl = bar['close'] - trail_dist
                if new_sl > sl + trail_step and new_sl > open_price:
                    sl = new_sl
                    current_trade['sl'] = sl

                # Hit SL or TP
                if bar['low'] <= sl:
                    exit_price = sl
                    exit_reason = "SL"
                    closed = True
                elif bar['high'] >= tp:
                    exit_price = tp
                    exit_reason = "TP"
                    closed = True

            elif pos_type == 'SELL':
                # Breakeven check
                prof = open_price - bar['low']
                if (sl >= open_price or sl == 0) and prof >= be_threshold:
                    sl = open_price - (5 * point)
                    current_trade['sl'] = sl

                # Trailing stop
                new_sl = bar['close'] + trail_dist
                if (sl == 0 or new_sl < sl - trail_step) and new_sl < open_price:
                    sl = new_sl
                    current_trade['sl'] = sl

                # Hit SL or TP
                if bar['high'] >= sl:
                    exit_price = sl
                    exit_reason = "SL"
                    closed = True
                elif bar['low'] <= tp:
                    exit_price = tp
                    exit_reason = "TP"
                    closed = True

            if closed:
                pnl_pts = (exit_price - open_price) if pos_type == 'BUY' else (open_price - exit_price)
                # Assume 0.01 lot (1,000 units on forex: 1 point = $0.01 per 0.01 lot roughly)
                pnl_dollars = (pnl_pts / point) * 0.1
                trades.append({
                    "ticket": len(trades) + 1,
                    "type": pos_type,
                    "open_time": current_trade['open_time'],
                    "close_time": bar['time'],
                    "open_price": open_price,
                    "close_price": exit_price,
                    "profit": pnl_dollars,
                    "reason": exit_reason
                })
                current_trade = None

        # 2. Check for entry signals on completed bars
        # Last closed bar was i-1, previous was i-2
        f_curr, f_prev = prev_bar['fast_ma'], prev_prev_bar['fast_ma']
        s_curr, s_prev = prev_bar['slow_ma'], prev_prev_bar['slow_ma']
        rsi_curr = prev_bar['rsi']
        atr_curr = prev_bar['atr']

        buy_signal = (f_prev <= s_prev) and (f_curr > s_curr) and (rsi_curr < RSI_UPPER)
        sell_signal = (f_prev >= s_prev) and (f_curr < s_curr) and (rsi_curr > RSI_LOWER)

        if buy_signal:
            # Reversal exit if holding short
            if current_trade is not None and current_trade['type'] == 'SELL':
                exit_price = bar['open'] + spread
                pnl_pts = current_trade['open_price'] - exit_price
                trades.append({
                    "ticket": len(trades) + 1,
                    "type": 'SELL',
                    "open_time": current_trade['open_time'],
                    "close_time": bar['time'],
                    "open_price": current_trade['open_price'],
                    "close_price": exit_price,
                    "profit": (pnl_pts / point) * 0.1,
                    "reason": "REVERSAL"
                })
                current_trade = None

            if current_trade is None and not np.isnan(atr_curr):
                entry_price = bar['open'] + spread
                sl_dist = atr_curr * SL_ATR_MULT
                tp_dist = atr_curr * TP_ATR_MULT
                current_trade = {
                    "type": 'BUY',
                    "open_time": bar['time'],
                    "open_price": entry_price,
                    "sl": entry_price - sl_dist,
                    "tp": entry_price + tp_dist,
                    "atr": atr_curr
                }

        elif sell_signal:
            # Reversal exit if holding long
            if current_trade is not None and current_trade['type'] == 'BUY':
                exit_price = bar['open']
                pnl_pts = exit_price - current_trade['open_price']
                trades.append({
                    "ticket": len(trades) + 1,
                    "type": 'BUY',
                    "open_time": current_trade['open_time'],
                    "close_time": bar['time'],
                    "open_price": current_trade['open_price'],
                    "close_price": exit_price,
                    "profit": (pnl_pts / point) * 0.1,
                    "reason": "REVERSAL"
                })
                current_trade = None

            if current_trade is None and not np.isnan(atr_curr):
                entry_price = bar['open']
                sl_dist = atr_curr * SL_ATR_MULT
                tp_dist = atr_curr * TP_ATR_MULT
                current_trade = {
                    "type": 'SELL',
                    "open_time": bar['time'],
                    "open_price": entry_price,
                    "sl": entry_price + sl_dist,
                    "tp": entry_price - tp_dist,
                    "atr": atr_curr
                }

    # Summary performance metrics based on completed trades
    trades_df = pd.DataFrame(trades)
    print("\n" + "="*45)
    print(f"HIGH-FIDELITY BACKTEST RESULTS: {SYMBOL}")
    print("="*45)
    print(f"Strategy:          MA Crossover + Wilder RSI")
    print(f"MA Settings:       {FAST_MA} / {SLOW_MA}")
    print(f"RSI Thresholds:    {RSI_LOWER} / {RSI_UPPER}")
    print(f"Execution Model:   ATR Exits, Breakeven, Trailing Stop, Spread")
    print("-"*45)

    if trades_df.empty:
        print("No completed trades recorded.")
        print("="*45)
        return

    wins = trades_df[trades_df['profit'] > 0]
    losses = trades_df[trades_df['profit'] < 0]
    total_trades = len(trades_df)
    win_rate = (len(wins) / total_trades) * 100
    gross_profit = wins['profit'].sum()
    gross_loss = abs(losses['profit'].sum())
    net_profit = trades_df['profit'].sum()
    profit_factor = (gross_profit / gross_loss) if gross_loss != 0 else float('inf')
    expected_payoff = trades_df['profit'].mean()

    # Drawdown based on equity curve of closed trades
    trades_df['cum_profit'] = trades_df['profit'].cumsum()
    trades_df['peak'] = trades_df['cum_profit'].cummax()
    trades_df['dd'] = trades_df['cum_profit'] - trades_df['peak']
    max_dd = abs(trades_df['dd'].min())

    print(f"Total Completed Trades: {total_trades}")
    print(f"Winning Trades:         {len(wins)} ({win_rate:.1f}%)")
    print(f"Losing Trades:          {len(losses)} ({100 - win_rate:.1f}%)")
    print(f"Gross Profit:           ${gross_profit:.2f}")
    print(f"Gross Loss:             ${gross_loss:.2f}")
    print(f"Net Profit:             ${net_profit:.2f}")
    print(f"Profit Factor:          {profit_factor:.2f}")
    print(f"Expected Payoff/Trade:  ${expected_payoff:.2f}")
    print(f"Max Equity Drawdown:    ${max_dd:.2f}")
    print("="*45)

if __name__ == "__main__":
    run_backtest()

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from itertools import product
import json
import os
from datetime import datetime
from connect_mt5 import CONFIG # Import symbols from main config

# --- BATCH SETTINGS ---
TIMEFRAME = mt5.TIMEFRAME_M1
BARS_COUNT = 5000 

# Ranges to test (Brute Force Space)
FAST_MA_RANGE = range(5, 15, 2)
SLOW_MA_RANGE = range(15, 30, 3)
RSI_UPPER_RANGE = [65, 70, 75]
RSI_LOWER_RANGE = [25, 30, 35]

from strategy_engine import calculate_rsi

def evaluate_strategy(df, fast_ma, slow_ma, rsi_upper, rsi_lower):
    df['fast'] = df['close'].rolling(window=fast_ma).mean()
    df['slow'] = df['close'].rolling(window=slow_ma).mean()
    df['rsi'] = calculate_rsi(df['close'], 14)
    df['pos'] = 0
    df.loc[(df['fast'] > df['slow']) & (df['fast'].shift(1) <= df['slow'].shift(1)) & (df['rsi'] < rsi_upper), 'pos'] = 1
    df.loc[(df['fast'] < df['slow']) & (df['fast'].shift(1) >= df['slow'].shift(1)) & (df['rsi'] > rsi_lower), 'pos'] = -1
    df['pos'] = df['pos'].replace(0, np.nan).ffill().fillna(0)
    df['strat_ret'] = df['close'].pct_change() * df['pos'].shift(1)
    total_return = (df['strat_ret'] + 1).prod() - 1
    return total_return

def evaluate_bb_strategy(df, bb_window=20, bb_std=2.0):
    df['m'] = df['close'].rolling(window=bb_window).mean()
    df['std'] = df['close'].rolling(window=bb_window).std()
    u, l = df['m'] + (bb_std * df['std']), df['m'] - (bb_std * df['std'])
    df['pos'] = 0
    df.loc[df['close'] < l, 'pos'] = 1
    df.loc[df['close'] > u, 'pos'] = -1
    df['pos'] = df['pos'].replace(0, np.nan).ffill().fillna(0)
    df['strat_ret'] = df['close'].pct_change() * df['pos'].shift(1)
    total_return = (df['strat_ret'] + 1).prod() - 1
    return total_return

def optimize_symbol(symbol, df_base):
    print(f"\n🔍 Optimizing {symbol}...")
    strat = CONFIG.get("portfolio", {}).get(symbol, {}).get("strategy", "MA_Crossover_RSI")
    
    if strat == "BB_Mean_Reversion":
        results = []
        for w in [15, 20, 25, 30]:
            for s in [1.5, 2.0, 2.5, 3.0]:
                ret = evaluate_bb_strategy(df_base.copy(), w, s)
                results.append({'bb_window': w, 'bb_std': s, 'return': ret})
        best = pd.DataFrame(results).sort_values(by='return', ascending=False).iloc[0]
        best_settings = {
            "bb_window": int(best['bb_window']),
            "bb_std": float(best['bb_std']),
            "last_optimized": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "estimated_return": float(best['return'])
        }
    else:
        results = []
        combinations = list(product(FAST_MA_RANGE, SLOW_MA_RANGE, RSI_UPPER_RANGE, RSI_LOWER_RANGE))
        for f_ma, s_ma, r_up, r_low in combinations:
            ret = evaluate_strategy(df_base.copy(), f_ma, s_ma, r_up, r_low)
            results.append({'fast_ma': f_ma, 'slow_ma': s_ma, 'rsi_up': r_up, 'rsi_low': r_low, 'return': ret})

        best = pd.DataFrame(results).sort_values(by='return', ascending=False).iloc[0]
        best_settings = {
            "fast_ma": int(best['fast_ma']),
            "slow_ma": int(best['slow_ma']),
            "rsi_up": int(best['rsi_up']),
            "rsi_low": int(best['rsi_low']),
            "last_optimized": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "estimated_return": float(best['return'])
        }
    
    with open(f"settings_{symbol}.json", "w") as f:
        json.dump(best_settings, f, indent=4)
    
    print(f"✅ {symbol} Optimized ({strat})! Best Return: {best['return']*100:.2f}%")

def run_batch():
    # Use credentials from main CONFIG to ensure proper authorization
    if not mt5.initialize(
        login=CONFIG["login"], 
        password=CONFIG["password"], 
        server=CONFIG["server"]
    ):
        print(f"❌ MT5 Init failed! Error code: {mt5.last_error()}")
        return

    symbols = list(CONFIG["portfolio"].keys())
    print(f"🚀 Starting Batch Optimization for: {symbols}")

    for symbol in symbols:
        rates = mt5.copy_rates_from_pos(symbol, TIMEFRAME, 0, BARS_COUNT)
        if rates is not None:
            df = pd.DataFrame(rates)
            optimize_symbol(symbol, df)
        else:
            print(f"❌ Failed to download data for {symbol}")

    mt5.shutdown()
    print("\n🏁 Portfolio Batch Optimization Complete!")

if __name__ == "__main__":
    run_batch()

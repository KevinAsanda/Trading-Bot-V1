import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from itertools import product
from datetime import datetime
import os

# --- SEARCH SPACE ---
SYMBOL = "EURUSD#"
TIMEFRAME = mt5.TIMEFRAME_M1
BARS_COUNT = 5000  # Smaller count for faster optimization

# Ranges to test
FAST_MA_RANGE = range(5, 15, 2)   # [5, 7, 9, 11, 13]
SLOW_MA_RANGE = range(15, 30, 3)  # [15, 18, 21, 24, 27]
RSI_UPPER_RANGE = [65, 70, 75]
RSI_LOWER_RANGE = [25, 30, 35]

from strategy_engine import calculate_rsi

def evaluate_strategy(df, fast_ma, slow_ma, rsi_upper, rsi_lower):
    # Indicators
    df['fast'] = df['close'].rolling(window=fast_ma).mean()
    df['slow'] = df['close'].rolling(window=slow_ma).mean()
    df['rsi'] = calculate_rsi(df['close'], 14)

    # Signals
    df['pos'] = 0
    df.loc[(df['fast'] > df['slow']) & (df['fast'].shift(1) <= df['slow'].shift(1)) & (df['rsi'] < rsi_upper), 'pos'] = 1
    df.loc[(df['fast'] < df['slow']) & (df['fast'].shift(1) >= df['slow'].shift(1)) & (df['rsi'] > rsi_lower), 'pos'] = -1
    
    df['pos'] = df['pos'].replace(0, np.nan).ffill().fillna(0)
    
    # Returns
    df['strat_ret'] = df['close'].pct_change() * df['pos'].shift(1)
    total_return = (df['strat_ret'] + 1).prod() - 1
    
    # Simple Sharpe Ratio (Return / Volatility)
    sharpe = df['strat_ret'].mean() / df['strat_ret'].std() * np.sqrt(252 * 1440) if df['strat_ret'].std() != 0 else 0
    
    return total_return, sharpe

def run_optimization():
    import os
    if not mt5.initialize(login=int(os.environ["MT5_LOGIN"]), password=os.environ["MT5_PASSWORD"], server=os.environ["MT5_SERVER"]):
        print("MT5 Init failed")
        return

    print(f"Downloading data for {SYMBOL}...")
    rates = mt5.copy_rates_from_pos(SYMBOL, TIMEFRAME, 0, BARS_COUNT)
    mt5.shutdown()
    
    if rates is None: return
    df_base = pd.DataFrame(rates)

    results = []
    
    # Generate all combinations
    combinations = list(product(FAST_MA_RANGE, SLOW_MA_RANGE, RSI_UPPER_RANGE, RSI_LOWER_RANGE))
    print(f"Testing {len(combinations)} combinations...")

    for f_ma, s_ma, r_up, r_low in combinations:
        ret, sharpe = evaluate_strategy(df_base.copy(), f_ma, s_ma, r_up, r_low)
        results.append({
            'fast_ma': f_ma,
            'slow_ma': s_ma,
            'rsi_up': r_up,
            'rsi_low': r_low,
            'return': ret,
            'sharpe': sharpe
        })

    # Sort results
    results_df = pd.DataFrame(results).sort_values(by='return', ascending=False)
    
    print("\n" + "="*40)
    print("TOP 5 OPTIMIZED PARAMETERS")
    print("="*40)
    print(results_df.head(5).to_string(index=False))
    print("="*40)

    best = results_df.iloc[0]
    best_settings = {
        "fast_ma": int(best['fast_ma']),
        "slow_ma": int(best['slow_ma']),
        "rsi_up": int(best['rsi_up']),
        "rsi_low": int(best['rsi_low']),
        "last_optimized": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "estimated_return": float(best['return'])
    }
    
    import json
    settings_file = f"settings_{SYMBOL}.json"
    with open(settings_file, "w") as f:
        json.dump(best_settings, f, indent=4)

    print(f"\n✅ Best Settings saved to: {settings_file}")
    print(f"Fast MA: {best['fast_ma']}, Slow MA: {best['slow_ma']}")
    print(f"Estimated Return: {best['return']*100:.2f}%")

if __name__ == "__main__":
    run_optimization()

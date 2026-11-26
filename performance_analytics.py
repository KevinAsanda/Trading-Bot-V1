import sqlite3
import pandas as pd
import numpy as np
import MetaTrader5 as mt5
from datetime import datetime
import os

# --- SETTINGS ---
DB_NAME = "trading_data.db"
SYMBOLS = ["EURUSD#", "GBPUSD#", "USDJPY#"] # Should match bot config

def calculate_metrics():
    if not os.path.exists(DB_NAME):
        print(f"Error: Database {DB_NAME} not found.")
        return

    # 1. Load Data
    conn = sqlite3.connect(DB_NAME)
    trades_df = pd.read_sql_query("SELECT * FROM trades WHERE profit IS NOT NULL", conn)
    equity_df = pd.read_sql_query("SELECT * FROM equity_history", conn)
    conn.close()

    if trades_df.empty:
        print("No closed trades found in history.")
        return

    # 2. Performance Ratios
    gross_profit = trades_df[trades_df['profit'] > 0]['profit'].sum()
    gross_loss = abs(trades_df[trades_df['profit'] < 0]['profit'].sum())
    
    profit_factor = gross_profit / gross_loss if gross_loss != 0 else float('inf')
    expected_payoff = trades_df['profit'].mean()
    
    # Drawdown & Recovery
    equity_df['peak'] = equity_df['equity'].cummax()
    equity_df['dd'] = (equity_df['equity'] - equity_df['peak'])
    max_dd_val = abs(equity_df['dd'].min())
    
    net_profit = trades_df['profit'].sum()
    recovery_factor = net_profit / max_dd_val if max_dd_val != 0 else float('inf')

    # 3. Portfolio Correlation (Live Fetch)
    correlation_report = "N/A (MT5 connection failed)"
    if mt5.initialize():
        corr_data = {}
        for symbol in SYMBOLS:
            rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, 100)
            if rates is not None:
                closes = pd.Series([r['close'] for r in rates])
                corr_data[symbol] = closes.pct_change().dropna().values
        mt5.shutdown()
        
        if corr_data:
            corr_df = pd.DataFrame(corr_data).corr()
            correlation_report = "\n" + corr_df.to_string()

    # 4. Final Output
    print("\n" + "="*45)
    print("🔬 QUANT PERFORMANCE REPORT (V2)")
    print("="*45)
    print(f"Total Trades:       {len(trades_df)}")
    print(f"Win Rate:           {(len(trades_df[trades_df['profit']>0])/len(trades_df)*100):.1f}%")
    print(f"Net Profit:         {net_profit:.2f}")
    print(f"---")
    print(f"Profit Factor:      {profit_factor:.2f}")
    print(f"Expected Payoff:    {expected_payoff:.2f}")
    print(f"Recovery Factor:    {recovery_factor:.2f}")
    print(f"Max Drawdown ($):   {max_dd_val:.2f}")
    print(f"---")
    print(f"Portfolio Correlation (H1):")
    print(correlation_report)
    print("="*45)

if __name__ == "__main__":
    calculate_metrics()

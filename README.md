# MetaTrader 5 Python Trading Suite

For the isolated demo setup and desktop/mobile monitoring instructions, see
[DEMO_TESTING.md](DEMO_TESTING.md). Run `demo_test.py --check` first; it sends no orders.

A professional-grade algorithmic trading system for MetaTrader 5, featuring automated execution, risk management, backtesting, and optimization.

## 🚀 Components

1.  **`connect_mt5.py`**: The core Trading Bot.
    *   Multi-symbol portfolio support.
    *   Moving Average Crossover + RSI filtering.
    *   Dynamic position sizing (percentage risk).
    *   Global safety limits (drawdown, daily P/L).
    *   Trailing stops and signal reversal handling.
    *   Telegram notifications and SQLite persistent logging.
2.  **`backtest_mt5.py`**: Strategy validation tool.
    *   Vectorized backtesting using Pandas.
    *   Calculates Total Return, Max Drawdown, and Win Rate.
3.  **`optimizer_mt5.py`**: Parameter optimization tool.
    *   Brute-force search for the best MA and RSI settings.
    *   Ranks settings by return and Sharpe Ratio.
4.  **`performance_analytics.py`**: Post-trade analysis tool.
    *   Reads from SQLite database.
    *   Calculates Sharpe Ratio, Max Drawdown, and Net Profit.
    *   Exports equity data for visualization.

## 🛠️ Workflow

To get the most out of this system, follow this professional workflow:

### 1. Optimize
Run the optimizer to find the best settings for your chosen symbol and timeframe.
```bash
python optimizer_mt5.py
```
Copy the "Best Settings" from the output.

### 2. Backtest
Validate the optimized settings over a larger dataset to ensure stability.
```bash
python backtest_mt5.py
```

### 3. Execute
Update the `CONFIG` in `connect_mt5.py` with your optimized settings and broker credentials, then start the bot.
```bash
python connect_mt5.py
```

### 4. Analyze
Periodically run the analytics script to see how the bot is performing in the real market.
```bash
python performance_analytics.py
```

## 📋 Requirements
*   MetaTrader 5 Windows Terminal (installed and logged in).
*   Python 3.8+.
*   Libraries: `MetaTrader5`, `pandas`, `numpy`, `requests`.

## ⚠️ Disclaimer
Trading financial markets involves significant risk. This software is for educational purposes only. Always test thoroughly on a **Demo Account** before considering live trading. The authors are not responsible for any financial losses incurred.

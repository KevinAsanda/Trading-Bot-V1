import pandas as pd
import numpy as np

def calculate_rsi(s: pd.Series, p: int = 14) -> pd.Series:
    """Wilder's Exponential Smoothing RSI - Shared Single Source of Truth."""
    d = s.diff()
    g, l = d.where(d > 0, 0), -d.where(d < 0, 0)
    ag, al = g.rolling(window=p).mean(), l.rolling(window=p).mean()
    for i in range(p, len(ag)):
        ag.iloc[i] = (ag.iloc[i-1] * (p - 1) + g.iloc[i]) / p
        al.iloc[i] = (al.iloc[i-1] * (p - 1) + l.iloc[i]) / p
    return 100 - (100 / (1 + (ag / al)))

def calculate_atr(df: pd.DataFrame, p: int = 14) -> pd.Series:
    """Wilder's True Range Average (ATR) - Shared Single Source of Truth."""
    tr = pd.concat([
        df['high'] - df['low'],
        abs(df['high'] - df['close'].shift()),
        abs(df['low'] - df['close'].shift())
    ], axis=1).max(axis=1)
    return tr.rolling(window=p).mean()

def _get_val(settings, symbol, key, default):
    if hasattr(settings, "get_setting"):
        v = settings.get_setting(symbol, key)
        return default if v is None else v
    if isinstance(settings, dict):
        return settings.get(key, default)
    return default

def _get_mtf(settings):
    if hasattr(settings, "config") and isinstance(settings.config, dict):
        return settings.config.get("enable_mtf", False)
    if isinstance(settings, dict):
        return settings.get("enable_mtf", False)
    return False

class BaseStrategy:
    def get_signal(self, symbol, df, trend_bullish, settings):
        raise NotImplementedError

class MACrossoverRSI(BaseStrategy):
    def get_signal(self, symbol, df, trend_bullish, settings):
        f = _get_val(settings, symbol, "fast_ma", 10)
        s = _get_val(settings, symbol, "slow_ma", 20)
        rsi_period = _get_val(settings, symbol, "rsi_period", 14)
        rsi_upper = _get_val(settings, symbol, "rsi_upper", 70)
        rsi_lower = _get_val(settings, symbol, "rsi_lower", 30)
        enable_mtf = _get_mtf(settings)

        df['f'] = df['close'].rolling(window=f).mean()
        df['s'] = df['close'].rolling(window=s).mean()
        df['rsi'] = calculate_rsi(df['close'], rsi_period)

        if len(df) < max(f, s, rsi_period) + 3:
            return "NONE"

        l_f, p_f = df['f'].iloc[-2], df['f'].iloc[-3]
        l_s, p_s = df['s'].iloc[-2], df['s'].iloc[-3]
        l_rsi = df['rsi'].iloc[-2]

        if p_f <= p_s and l_f > l_s and l_rsi < rsi_upper:
            if enable_mtf and trend_bullish == False:
                return "NONE"
            return "BUY"

        if p_f >= p_s and l_f < l_s and l_rsi > rsi_lower:
            if enable_mtf and trend_bullish == True:
                return "NONE"
            return "SELL"

        return "NONE"

class BBMeanReversion(BaseStrategy):
    def get_signal(self, symbol, df, trend_bullish, settings):
        w = int(_get_val(settings, symbol, "bb_window", 20) or 20)
        std_mult = float(_get_val(settings, symbol, "bb_std", 2.0) or 2.0)

        if len(df) < w + 2:
            return "NONE"

        df['m'] = df['close'].rolling(window=w).mean()
        df['std'] = df['close'].rolling(window=w).std()
        u = df['m'] + (std_mult * df['std'])
        l = df['m'] - (std_mult * df['std'])

        lc = df['close'].iloc[-2]
        if lc < l.iloc[-2]:
            return "BUY"
        if lc > u.iloc[-2]:
            return "SELL"
        return "NONE"

class StrategyFactory:
    _strategies = {
        "MA_Crossover_RSI": MACrossoverRSI(),
        "BB_Mean_Reversion": BBMeanReversion()
    }

    @classmethod
    def get_strategy(cls, name: str) -> BaseStrategy:
        return cls._strategies.get(name, cls._strategies["MA_Crossover_RSI"])

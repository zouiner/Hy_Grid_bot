from dataclasses import dataclass
from typing import Dict, Any, Optional, List, Tuple
import numpy as np
import pandas as pd
from ta.trend import EMAIndicator, ADXIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import BollingerBands, AverageTrueRange

@dataclass
class StrategyConfig:
    timeframe: str = '1H'
    mode: str = 'auto'  # auto | trend | grid
    adx_trend: float = 22.0
    bb_width_max: float = 0.06  # 6% of price considered range
    ema_fast: int = 20
    ema_slow: int = 50
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    rsi_len: int = 14
    atr_len: int = 14
    grid_levels: int = 5
    grid_step_atr: float = 0.5  # step = ATR * factor
    grid_tp_mult: float = 2.0   # TP distance = step * mult


def compute_indicators(df: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    df['ema_fast'] = EMAIndicator(close=df['close'], window=cfg.ema_fast).ema_indicator()
    df['ema_slow'] = EMAIndicator(close=df['close'], window=cfg.ema_slow).ema_indicator()
    macd = MACD(close=df['close'], window_slow=cfg.macd_slow, window_fast=cfg.macd_fast, window_sign=cfg.macd_signal)
    df['macd'] = macd.macd()
    df['macd_signal'] = macd.macd_signal()
    df['macd_hist'] = macd.macd_diff()
    df['rsi'] = RSIIndicator(close=df['close'], window=cfg.rsi_len).rsi()
    atr = AverageTrueRange(high=df['high'], low=df['low'], close=df['close'], window=cfg.atr_len)
    df['atr'] = atr.average_true_range()
    adx = ADXIndicator(high=df['high'], low=df['low'], close=df['close'], window=cfg.atr_len)
    df['adx'] = adx.adx()
    bb = BollingerBands(close=df['close'], window=20, window_dev=2)
    df['bb_high'] = bb.bollinger_hband()
    df['bb_low'] = bb.bollinger_lband()
    df['bb_mid'] = bb.bollinger_mavg()
    df['bb_width'] = (df['bb_high'] - df['bb_low']) / df['close']
    return df


def detect_regime(df: pd.DataFrame, cfg: StrategyConfig) -> str:
    if df.empty:
        return 'trend'
    last = df.iloc[-1]
    if cfg.mode in ('trend', 'grid'):
        return cfg.mode
    # auto mode
    if last['adx'] >= cfg.adx_trend and last['close'] > last['ema_slow']:
        return 'trend'
    if last['bb_width'] <= cfg.bb_width_max and last['adx'] < cfg.adx_trend:
        return 'grid'
    # default bias
    return 'trend'


def trend_signal(df: pd.DataFrame, cfg: StrategyConfig) -> Optional[Dict[str, Any]]:
    if len(df) < max(cfg.ema_slow, cfg.atr_len, cfg.macd_slow) + 5:
        return None
    last = df.iloc[-1]
    prev = df.iloc[-2]
    up_trend = last['ema_fast'] > last['ema_slow'] and last['macd'] > last['macd_signal'] and last['rsi'] >= 50
    # Entry trigger: 1) breakout of 20-bar high OR 2) pullback to ema_fast then close back above it
    donchian_high = df['high'].rolling(20).max().iloc[-2]
    breakout = last['close'] > donchian_high
    pullback_reject = (prev['close'] < prev['ema_fast']) and (last['close'] > last['ema_fast'])

    if up_trend and (breakout or pullback_reject):
        entry = float(last['close'])
        stop = float(min(df['low'].iloc[-20:]))  # swing low last 20
        atr = float(last['atr'])
        # safety SL not tighter than ATR*2 below entry
        stop = min(stop, entry - 2.0 * atr)
        return {
            'side': 'long',
            'entry': entry,
            'stop': stop,
            'tp': None,             # let it run; trailing handles exits
            'mode': 'trend',
            'reason': f"EMA/MACD/RSI aligned; trigger={'breakout' if breakout else 'pullback'}"
        }
    return None


def grid_plan(df: pd.DataFrame, cfg: StrategyConfig) -> Optional[Dict[str, Any]]:
    if df.empty:
        return None
    last = df.iloc[-1]
    mid = float(last['bb_mid'])
    atr = float(last['atr'])
    step = max(atr * cfg.grid_step_atr, 0.0000001)
    levels = []
    for i in range(1, cfg.grid_levels + 1):
        buy_px = mid - i * step
        sell_px = buy_px + cfg.grid_tp_mult * step
        levels.append({'buy': float(buy_px), 'sell': float(sell_px)})
    return {
        'mode': 'grid',
        'levels': levels,
        'step': step,
        'mid': mid,
        'reason': f"Range mode: bb_width={last['bb_width']:.3f}, adx={last['adx']:.1f}"
    }


def generate_signal(df: pd.DataFrame, cfg: StrategyConfig) -> Dict[str, Any]:
    df = compute_indicators(df, cfg)
    regime = detect_regime(df, cfg)
    if regime == 'trend':
        sig = trend_signal(df, cfg)
        return sig or {'mode': 'trend', 'noop': True}
    else:
        plan = grid_plan(df, cfg)
        return plan or {'mode': 'grid', 'noop': True}
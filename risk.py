from dataclasses import dataclass
from typing import Optional
import pandas as pd


@dataclass
class RiskConfig:
risk_per_trade: float = 0.01 # 1%
atr_multiplier_sl: float = 2.0
atr_multiplier_trail: float = 2.5
min_position_usd: float = 20.0




def position_size_spot(equity_usd: float, entry: float, stop: float, risk_per_trade: float) -> float:
"""Return base-asset size for spot so that (entry-stop) * size ≈ equity * risk%.
Guards against zero/negative stop-distance.
"""
dist = abs(entry - stop)
if dist <= 0:
return 0.0
risk_usd = max(equity_usd * risk_per_trade, 0)
size = risk_usd / dist
return max(0.0, size)




def atr_trailing_stop(df: pd.DataFrame, atr_mult: float, side: str) -> Optional[float]:
"""ATR trailing stop based on last row. side: 'long' | 'short'"""
if df.empty or 'atr' not in df.columns:
return None
last = df.iloc[-1]
if side == 'long':
return float(last['close'] - atr_mult * last['atr'])
else:
return float(last['close'] + atr_mult * last['atr'])




def update_trailing_stop(current_trail: Optional[float], new_trail: Optional[float], side: str) -> Optional[float]:
if new_trail is None:
return current_trail
if current_trail is None:
return new_trail
if side == 'long':
return max(current_trail, new_trail)
else:
return min(current_trail, new_trail)
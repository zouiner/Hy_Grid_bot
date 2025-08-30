# Hybrid Trend+Grid Crypto Bot (OKX + Telegram)


**Core Principles (from your ideas):**
- Risk per trade = 0.5–1.0% (configurable) ⇒ small fixed losses, survive to catch **1 WIN**.
- Trend mode: **EMA(20/50) + MACD + ADX** alignment, entry on **breakout** or **pullback reclaim**, exit via **ATR trailing** (let winner run).
- Range mode: **Bollinger width + low ADX** ⇒ deploy **small grid** around mid; scalp oscillations with defined steps and TPs.


## Quick Start
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env # fill keys
python main.py
# Hybrid Trend+Grid Crypto Bot (OKX + Telegram)

A production‑ready Python bot that combines **Trend Following** and **Volatility‑aware Grid** tactics, wires **real OKX limit orders**, attaches **OCO (TP/SL) *after fill***, auto‑enters on **Dip‑Buy**/**Breakout** levels, and reports **daily PnL & R‑multiples** via Telegram. Includes **precision rounding** (tick/lot) and **chart plotting** for grids/TP/SL/current price.

> **Safety first** → Default runs in **OKX simulated trading**. Flip the flag only when you’re ready.

---

## ✨ Features

- **Hybrid strategy**: Regime switch (`auto|trend|grid`) with EMA/ADX confirmation, ATR‑based stops, and grid built from volatility.
- **Real orders**: Places live **limit buys**; on **fill detection** places **OCO** (TP/SL) via OKX `order-algo`.
- **Auto Dip/Breakout**: Your custom levels trigger entries with risk‑aware SL/TP.
- **Risk & R‑tracking**: Fixed fractional risk (default 1%), per‑trade **R** and daily summary (realized/unrealized).
- **Precision rounding**: Price/size rounding to instrument **tickSize** and **lotSz**.
- **Plotting**: `/plot SYMBOL [TF]` sends a chart with **grid lines**, **TP**, **SL**, **current price**, plus **EMA fast/slow** & **ATR±2 bands**.
- **Telegram control**: Full command set, summaries at **21:00 Europe/London** (configurable).

---

## 🗂️ Project Structure

```
project/
  ├─ .env.example
  ├─ requirements.txt
  ├─ main.py               # Telegram bot, scheduling, plotting, orchestration
  ├─ okx_client.py         # Minimal OKX REST client (spot + OCO algos)
  ├─ strategy.py           # Signals (trend/grid), indicators (EMA, ADX, ATR)
  ├─ risk.py               # Sizing, ATR trailing stop, helpers, R-multiple
  ├─ storage.py            # JSON state: positions, alerts, trades
  └─ README.md
```

---

## 📦 Requirements

- Python **3.10+**
- An **OKX** account with API key/secret/passphrase (Spot)
- A **Telegram Bot Token** (BotFather) and your chat ID

Install deps:

```bash
pip install -r requirements.txt
```

**requirements.txt (core):**

```
python-telegram-bot>=20.7
pandas>=2.1
numpy>=1.26
matplotlib>=3.8
APScheduler>=3.10
python-dotenv>=1.0
requests>=2.32
ujson>=5.9
 tzdata>=2024.1
```

> If running on Linux server without system timezone data, `tzdata` ensures Europe/London scheduling.

---

## 🔐 .env Setup

Create **.env** from the template:

```ini
# OKX
OKX_API_KEY=your_key
OKX_API_SECRET=your_secret
OKX_API_PASSPHRASE=your_passphrase

# OKX DEMO (for testing)
OKX_DEMO_API_KEY='your_key'
OKX_DEMO_API_SECRET='your_secret'
OKX_DEMO_API_PASSPHRASE='your_passphrase'

OKX_ENV=demo # live

# Telegram
TELEGRAM_BOT_TOKEN=your_telegram_token
# optional: restrict bot to a single chat id
TELEGRAM_CHAT_ID=123456789

# Strategy / runtime
WATCHLIST=ETH-USDT,BTC-USDT
TIMEFRAME=1H
RISK_PER_TRADE=0.01
MODE=auto
JOB_INTERVAL_SEC=300
BASE_QUOTE=USDT
```

**OKX permissions**: enable **Read** and **Trade** for Spot. IP whitelist recommended.

**Telegram chat id**: send `/start` to your bot, check logs for chat id, or set `TELEGRAM_CHAT_ID` to restrict access.

---

## ▶️ Run

```bash
python main.py
```

The scheduler runs every `JOB_INTERVAL_SEC` seconds and posts a daily PnL at **21:00 Europe/London**.

---

## 🤖 Commands (Telegram)

```
/start
/status
/watchlist
/add SYMBOL-USDT         # /add SUI-USDT
/rm SYMBOL-USDT
/mode auto|trend|grid
/risk 0.005..0.02        # 0.01 = 1%
/pause
/resume

/alerts
/alert SYMBOL dip PRICE          # /alert ETH-USDT dip 3500
/alert SYMBOL breakout PRICE     # /alert ETH-USDT breakout 4200
/auto_dip on|off
/auto_breakout on|off
/clear SYMBOL

/plot SYMBOL [TF]        # /plot ETH-USDT 4H
/close SYMBOL
/pnl
```

**Plotting** uses your current timeframe unless overridden (allowed TFs: 1m,3m,5m,15m,30m,1H,2H,4H,6H,12H,1D,1W).

---

## 🧠 Strategy Logic (overview)

### Trend Following
- **Filters**: `EMA_fast > EMA_slow` and `ADX >= adx_trend` for longs.
- **Entry**: market/close proxy (simulation) sized by fixed‑fraction risk.
- **Stop**: `entry − 2×ATR`. **Trailing**: ATR trailing (`atr_mult≈2.5`).
- **Exit**: on stop or trailing stop breach.

### Volatility Grid
- **Levels**: derived from ATR and structure; creates N `buy` prices with paired `sell` targets.
- **Orders**: submit **real limit buys**; **no TP/SL attached initially**.
- **Fill‑detected OCO**: when a buy is **filled** (confirmed via `orders-pending` + `order`), place **OCO** (TP/SL) of equal size.
- **Cycle end**: when none of our grid buys remain pending, grid cycle is considered complete.

### Dip‑Buy & Breakout Auto‑Positioning
- **Dip**: on price ≤ level → place limit buy at level, OCO after fill. TP≈`level + 2×ATR`, SL≈`level − 1.5×ATR`.
- **Breakout**: trigger only if `EMA_fast > EMA_slow` and `ADX ≥ threshold`; SL≈`entry − 2×ATR`.

### Sizing & R‑metrics
- **Risk per trade** = `balance × RISK_PER_TRADE`.
- **Size** = risk ÷ distance to stop; rounded to **lotSz**.
- **R‑multiple** = realized PnL ÷ (size × |entry − stop|).

---

## 📈 Charts

- `/plot SYMBOL [TF]` overlays:
  - Close price
  - **Grid lines**, **TP**, **SL**, **Current price**
  - **EMA fast/slow**, **ATR±2 bands**
- Auto‑plots are sent when a **grid** or **auto‑dip** is created.

> Uses `matplotlib` without custom styles for portability.

---

## 🧩 Precision Rounding

- On each order, bot fetches & caches `tickSize` and `lotSz` (OKX `/public/instruments`).
- Prices rounded **down** to nearest tick, sizes rounded **down** to nearest lot.
- Prevents OKX rejections due to invalid increments.

---

## 📝 State & Logs

- `state.json` stores:
  - `open_positions`: trend or grid metadata, grid order IDs, and OCO flags
  - `alerts`: dip/breakout levels
  - `trades`: audit trail (entry/stop/size/exit/PnL/R/open)
- Console logs are verbose; consider redirecting to a file for servers.

---

## ⚙️ Configuration Tips

- **Sim vs Live**: keep `OKX_USE_SIMULATED=1` until you’ve validated behavior.
- **Scheduling**: change `JOB_INTERVAL_SEC` for faster loops; beware API rate limits.
- **Risk**: `/risk 0.01` is **1%** per trade. For new accounts use **0.25%–0.5%**.
- **Watchlist**: keep a short list for stability; each symbol pulls candles & ticker.

---

## 🧪 Quick Test Flow

1. Start in **simulated** mode.
2. `/add ETH-USDT`
3. `/auto_dip on` and `/alert ETH-USDT dip 3500`
4. Wait for trigger → bot places **limit buy**; on fill, OCO is placed.
5. Use `/plot ETH-USDT` to visualize lines.
6. Review `/pnl` at any time; daily summary arrives 21:00 Europe/London.

---

## 🚀 Deploy Notes

- Use **screen/tmux** or a supervisor (systemd) to keep the bot running.
- For Docker, create an image with Python 3.10, install requirements, mount your `.env` and persist `state.json`.
- Consider **logging to file** and rotating logs.

---

## 🧯 Troubleshooting

- **Order rejected (precision)** → ensure rounding is active; instrument metadata cache populates on first use.
- **No chart** → install `matplotlib` and ensure server can render (Agg backend is default when no display).
- **No messages** → check `TELEGRAM_BOT_TOKEN` and that the bot was started in chat; set `TELEGRAM_CHAT_ID`.
- **OKX 401/403** → IP whitelist, API permissions, timestamp skew.
- **Scheduling drift** → install `tzdata`, ensure server time is synced (NTP).

---

## 🔒 Security

- Keep API keys in **.env** only; never commit.
- Use **read/write (trade)** minimal permissions; no withdrawals.
- Set **IP allowlist** on OKX API keys.

---

## ⚠️ Disclaimer

This software is for **educational purposes**. Markets carry risk. Past performance doesn’t guarantee future results. You are responsible for API keys, capital, and compliance.

---

## 📚 Roadmap (optional)

- Partial‑fill OCO (attach as soon as any quantity fills)
- Futures support (isolated/cross, leverage)
- Walk‑forward parameter tuning & regime detection improvements
- Export to CSV/Google Sheets
- Web dashboard (FastAPI) for monitoring & manual actions


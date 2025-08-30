# main.py
import os
import io
import math
import uuid
import logging
from typing import Dict, Any, List, Optional

from dotenv import load_dotenv
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from okx_client import OkxClient
from strategy import StrategyConfig, generate_signal, compute_indicators
from risk import RiskConfig, position_size_spot, atr_trailing_stop, update_trailing_stop
from storage import load_state, save_state

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s - %(message)s')

# ---- env & globals ----
load_dotenv()

# Select demo vs live keys cleanly
ENV_MODE = os.getenv("OKX_ENV", "demo").lower()
if ENV_MODE == "demo":
    OKX_KEY = os.getenv("OKX_DEMO_API_KEY", "")
    OKX_SEC = os.getenv("OKX_DEMO_API_SECRET", "")
    OKX_PAS = os.getenv("OKX_DEMO_API_PASSPHRASE", "")
    SIMULATED = True
else:
    OKX_KEY = os.getenv("OKX_API_KEY", "")
    OKX_SEC = os.getenv("OKX_API_SECRET", "")
    OKX_PAS = os.getenv("OKX_API_PASSPHRASE", "")
    SIMULATED = False

TG_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
CHAT_LOCK = os.getenv('TELEGRAM_CHAT_ID', '')  # optional: restrict to a single chat
BROADCAST_CHAT_ID: Optional[int] = None         # remembered on /start if CHAT_LOCK not set

WATCHLIST = [s.strip().upper() for s in os.getenv('WATCHLIST', 'ETH-USDT,BTC-USDT').split(',') if s.strip()]
TIMEFRAME = os.getenv('TIMEFRAME', '1H')
RISK_PER_TRADE = float(os.getenv('RISK_PER_TRADE', '0.01'))
MODE = os.getenv('MODE', 'auto')
INTERVAL = int(os.getenv('JOB_INTERVAL_SEC', '300'))  # seconds
BASE_QUOTE = os.getenv('BASE_QUOTE', 'USDT')

risk_cfg = RiskConfig(risk_per_trade=RISK_PER_TRADE)
strat_cfg = StrategyConfig(timeframe=TIMEFRAME, mode=MODE)

state = load_state()
client = OkxClient(OKX_KEY, OKX_SEC, OKX_PAS, simulated=SIMULATED)
RUN_ENABLED = True

# ---- instrument metadata cache (tick/lot precision) ----
_INSTR_META: Dict[str, Dict[str, float]] = {}

def _fetch_instruments() -> List[Dict[str, Any]]:
    try:
        res = client._request('GET', '/api/v5/public/instruments', params={'instType': 'SPOT'}, auth=False)
        return res.get('data', []) or []
    except Exception as e:
        logging.exception(f"Fetch instruments failed: {e}")
        return []

def get_instr_meta(instId: str) -> Dict[str, float]:
    """Return {'tickSize': float, 'lotSz': float} for instId, cached."""
    if instId in _INSTR_META:
        return _INSTR_META[instId]
    data = _fetch_instruments()
    for it in data:
        if it.get('instId') == instId:
            tick = float(it.get('tickSz', it.get('tickSize', '0.00000001')))
            lot  = float(it.get('lotSz', '0.00000001'))
            _INSTR_META[instId] = {'tickSize': tick, 'lotSz': lot}
            return _INSTR_META[instId]
    _INSTR_META[instId] = {'tickSize': 0.01, 'lotSz': 0.000001}
    return _INSTR_META[instId]

def round_to_tick(px: float, tick: float) -> float:
    if tick <= 0:
        return px
    return math.floor(px / tick) * tick

def round_size(sz: float, lot: float) -> float:
    if lot <= 0:
        return sz
    return math.floor(sz / lot) * lot

# ---- helpers ----
def get_chat_id(update: Optional[Update] = None) -> Optional[int]:
    """Prefer CHAT_LOCK; else remember last interactive chat for background pushes."""
    global BROADCAST_CHAT_ID
    if CHAT_LOCK:
        try:
            return int(CHAT_LOCK)
        except Exception:
            return None
    if update and update.effective_chat:
        BROADCAST_CHAT_ID = update.effective_chat.id
        return BROADCAST_CHAT_ID
    return BROADCAST_CHAT_ID

def safe_chat(update: Update) -> bool:
    if not CHAT_LOCK:
        return True
    return update.effective_chat and str(update.effective_chat.id) == str(CHAT_LOCK)

async def say(app: Application, text: str, update: Optional[Update] = None):
    cid = get_chat_id(update)
    if cid is None:
        logging.warning("No chat id to send message.")
        return
    try:
        await app.bot.send_message(chat_id=cid, text=text)
    except Exception as e:
        logging.error(f"Telegram send error: {e}")

def parse_tf_arg(args: List[str], default_tf: str) -> str:
    if not args:
        return default_tf
    tf = args[0].upper()
    ok = {"1M","3M","5M","15M","30M","1H","2H","4H","6H","12H","1D","1W"}
    return tf if tf in ok else default_tf

async def send_plot(app: Application, sym: str, df: pd.DataFrame, lines: Dict[str, List[float]], title_note: str = "", overlay_ind: Optional[pd.DataFrame] = None):
    """
    lines: {'grid':[...], 'tp':[...], 'sl':[...], 'price':[last]}
    Robust to pandas 2.x (no Series.to_pydatetime).
    """
    if df.empty:
        return

    # Robust time conversion (tz-aware -> tz-naive -> python datetime array)
    times_idx = pd.DatetimeIndex(pd.to_datetime(df["time"], utc=True)).tz_convert(None)
    times = times_idx.to_pydatetime()
    closes = df["close"].astype(float).values

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(times, closes, label=f"{sym} close")

    # indicators overlay (align lengths safely)
    if overlay_ind is not None and not overlay_ind.empty:
        k = min(len(overlay_ind), len(times))
        if k > 0:
            if "ema_fast" in overlay_ind.columns:
                ax.plot(times[-k:], overlay_ind["ema_fast"].values[-k:], label="EMA fast")
            if "ema_slow" in overlay_ind.columns:
                ax.plot(times[-k:], overlay_ind["ema_slow"].values[-k:], label="EMA slow")
            if "atr" in overlay_ind.columns:
                atr = overlay_ind["atr"].values[-k:]
                base = closes[-k:]
                ax.plot(times[-k:], base + 2.0*atr, label="ATR+2")
                ax.plot(times[-k:], base - 2.0*atr, label="ATR-2")

    # level overlays
    for g in lines.get('grid', []): ax.axhline(g, linestyle='--', linewidth=1, label='grid')
    for t in lines.get('tp', []):   ax.axhline(t, linestyle='-',  linewidth=1, label='tp')
    for s in lines.get('sl', []):   ax.axhline(s, linestyle='-.', linewidth=1, label='sl')
    for p in lines.get('price', []):ax.axhline(p, linestyle=':',  linewidth=1.5, label='current')

    ax.set_title(f"{sym} {strat_cfg.timeframe} {title_note}".strip())
    ax.set_xlabel("Time")
    ax.set_ylabel("Price")

    # de-duplicate legend labels
    handles, labels = ax.get_legend_handles_labels()
    uniq = dict(zip(labels, handles))
    ax.legend(uniq.values(), uniq.keys(), loc='best')

    fig.autofmt_xdate()
    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format='png', dpi=140)
    plt.close(fig)
    buf.seek(0)

    cid = get_chat_id()
    if cid is None:
        return
    try:
        await app.bot.send_photo(chat_id=cid, photo=buf, caption=f"{sym} grid/TP/SL & price")
    except Exception as e:
        logging.error(f"Telegram send_photo error: {e}")

# ---- trade log helpers ----
def log_new_trade(symbol: str, mode: str, side: str, entry: float, stop: float, size: float) -> str:
    trade_id = str(uuid.uuid4())[:8]
    risk_per_unit = abs(entry - stop)
    state['trades'].append({
        'id': trade_id,
        'symbol': symbol,
        'mode': mode,
        'side': side,
        'entry': entry,
        'stop': stop,
        'size': size,
        'risk_per_unit': risk_per_unit,
        'open': True,
        'exit': None,
        'pnl': 0.0,
        'R': 0.0
    })
    save_state(state)
    return trade_id

def close_trade_by_symbol(symbol: str, exit_px: float):
    for tr in reversed(state['trades']):
        if tr['symbol'] == symbol and tr['open']:
            tr['open'] = False
            tr['exit'] = exit_px
            pnl = (exit_px - tr['entry']) * tr['size'] if tr['side'] == 'long' else (tr['entry'] - exit_px) * tr['size']
            denom = max(1e-9, tr['risk_per_unit'] * tr['size'])
            tr['pnl'] = pnl
            tr['R'] = pnl / denom
            save_state(state)
            return tr
    return None

# ---- commands ----
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not safe_chat(update): return
    get_chat_id(update)
    await update.message.reply_text(
        "🤖 Hybrid Trend+Grid Bot\n"
        "Commands:\n"
        "  /status /watchlist /add SYMBOL /rm SYMBOL\n"
        "  /mode auto|trend|grid  /risk 0.005..0.02  /pause  /resume\n"
        "  /alerts  /alert SYMBOL dip|breakout PRICE  /auto_dip on|off  /auto_breakout on|off\n"
        "  /plot SYMBOL [TF]      /close SYMBOL   /pnl\n"
        "  /balance               /wallet\n"
        f"  (env={ENV_MODE}, simulated={SIMULATED})"
    )

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not safe_chat(update): return
    pos_lines = []
    for sym, pos in state['open_positions'].items():
        if pos['mode'] == 'trend':
            pos_lines.append(f"• {sym} {pos['side']} TREND size={pos['size']:.6f} entry={pos['entry']:.6f} stop={pos.get('stop'):.6f} trail={pos.get('trail')}")
        elif pos['mode'] == 'grid':
            pos_lines.append(f"• {sym} GRID orders={len(pos.get('grid_orders', []))} step={pos.get('grid',{}).get('step'):.8f}")
    text = (
        f"Mode: {strat_cfg.mode}\n"
        f"Auto: dip={state['config'].get('auto_dip')} breakout={state['config'].get('auto_breakout')}\n"
        f"Watchlist: {', '.join(WATCHLIST)}\n"
        "Positions:\n" + ("\n".join(pos_lines) if pos_lines else "(none)")
    )
    await update.message.reply_text(text)

async def cmd_watchlist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not safe_chat(update): return
    await update.message.reply_text(f"Watchlist: {', '.join(WATCHLIST)}")

async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not safe_chat(update): return
    if not ctx.args: return await update.message.reply_text("Usage: /add SYMBOL-USDT")
    sym = ctx.args[0].upper()
    if sym not in WATCHLIST: WATCHLIST.append(sym)
    await update.message.reply_text(f"Added {sym}. Now: {', '.join(WATCHLIST)}")

async def cmd_rm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not safe_chat(update): return
    if not ctx.args: return await update.message.reply_text("Usage: /rm SYMBOL-USDT")
    sym = ctx.args[0].upper()
    if sym in WATCHLIST: WATCHLIST.remove(sym)
    await update.message.reply_text(f"Removed {sym}. Now: {', '.join(WATCHLIST)}")

async def cmd_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not safe_chat(update): return
    if not ctx.args or ctx.args[0] not in ('auto', 'trend', 'grid'):
        return await update.message.reply_text("Usage: /mode auto|trend|grid")
    strat_cfg.mode = ctx.args[0]
    await update.message.reply_text(f"Mode set to {strat_cfg.mode}")

async def cmd_risk(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not safe_chat(update): return
    if not ctx.args: return await update.message.reply_text("Usage: /risk 0.01 (1%)")
    try:
        r = float(ctx.args[0])
        if 0.001 <= r <= 0.05:
            risk_cfg.risk_per_trade = r
            await update.message.reply_text(f"Risk per trade set to {r*100:.2f}%")
        else:
            await update.message.reply_text("Out of range (0.1%–5%)")
    except:
        await update.message.reply_text("Invalid number")

async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global RUN_ENABLED
    RUN_ENABLED = False
    await update.message.reply_text("⏸️ Execution paused")

async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global RUN_ENABLED
    RUN_ENABLED = True
    await update.message.reply_text("▶️ Execution resumed")

async def cmd_alerts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not safe_chat(update): return
    lines = []
    for sym, a in state['alerts'].items():
        lines.append(f"{sym}: dip={a.get('dip', [])} breakout={a.get('breakout', [])}")
    await update.message.reply_text("Alerts:\n" + ("\n".join(lines) if lines else "(none)"))

async def cmd_alert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not safe_chat(update): return
    if len(ctx.args) < 3: return await update.message.reply_text("Usage: /alert SYMBOL dip|breakout PRICE")
    sym, kind, price = ctx.args[0].upper(), ctx.args[1], ctx.args[2]
    try:
        price = float(price)
    except:
        return await update.message.reply_text("Price must be a number")
    a = state['alerts'].setdefault(sym, {'dip': [], 'breakout': []})
    if kind not in a: return await update.message.reply_text("Kind must be dip or breakout")
    a[kind].append(price)
    save_state(state)
    await update.message.reply_text(f"Alert added: {sym} {kind} {price}")

async def cmd_auto_dip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not safe_chat(update): return
    if not ctx.args or ctx.args[0] not in ('on', 'off'):
        return await update.message.reply_text("Usage: /auto_dip on|off")
    state['config']['auto_dip'] = (ctx.args[0] == 'on')
    save_state(state)
    await update.message.reply_text(f"Auto DIP set to {state['config']['auto_dip']}")

async def cmd_auto_breakout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not safe_chat(update): return
    if not ctx.args or ctx.args[0] not in ('on', 'off'):
        return await update.message.reply_text("Usage: /auto_breakout on|off")
    state['config']['auto_breakout'] = (ctx.args[0] == 'on')
    save_state(state)
    await update.message.reply_text(f"Auto BREAKOUT set to {state['config']['auto_breakout']}")

async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not safe_chat(update): return
    if not ctx.args: return await update.message.reply_text("Usage: /clear SYMBOL")
    sym = ctx.args[0].upper()
    state['alerts'].pop(sym, None)
    save_state(state)
    await update.message.reply_text(f"Cleared alerts for {sym}")

async def cmd_close(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not safe_chat(update): return
    if not ctx.args: return await update.message.reply_text("Usage: /close SYMBOL")
    sym = ctx.args[0].upper()
    pos = state['open_positions'].get(sym)
    if not pos: return await update.message.reply_text("No open position")
    tk = client.ticker(sym)
    last = float(tk.get('last', '0')) if tk else pos.get('entry', 0)
    tr = close_trade_by_symbol(sym, last)
    await update.message.reply_text(
        f"Closed {sym} at {last:.6f}. R≈{tr['R']:.2f} PnL≈{tr['pnl']:.2f} {BASE_QUOTE}" if tr else f"Closed {sym}"
    )
    state['open_positions'].pop(sym, None)
    save_state(state)

async def cmd_pnl(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not safe_chat(update): return
    await send_daily_pnl(app=ctx.application)

async def cmd_plot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not safe_chat(update): return
    if not ctx.args: return await update.message.reply_text("Usage: /plot SYMBOL-USDT [TF]")
    sym = ctx.args[0].upper()
    tf = parse_tf_arg(ctx.args[1:], strat_cfg.timeframe)
    df = client.candles(sym, bar=tf, limit=300)
    tk = client.ticker(sym)
    last = float(tk.get('last', df.iloc[-1]['close'])) if not df.empty else float(tk.get('last', '0'))
    pos = state['open_positions'].get(sym, {})
    lines = {'grid': [], 'tp': [], 'sl': [], 'price': [last]}
    ind = compute_indicators(df, StrategyConfig(timeframe=tf, mode=strat_cfg.mode)) if not df.empty else pd.DataFrame()
    if pos and pos.get('mode') == 'grid':
        for od in pos.get('grid_orders', []):
            lines['grid'].append(float(od['buy']))
            lines['tp'].append(float(od['tp']))
            lines['sl'].append(float(od['sl']))
    elif pos and pos.get('mode') == 'trend':
        lines['sl'].append(float(pos.get('stop', last)))
    await send_plot(ctx.application, sym, df, lines, title_note=f"(manual /plot {tf})", overlay_ind=ind)

async def cmd_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not safe_chat(update): return
    usdt = client.balance(BASE_QUOTE)
    await update.message.reply_text(f"Balance: {usdt:.2f} {BASE_QUOTE} (env={ENV_MODE}, simulated={SIMULATED})")

async def cmd_wallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not safe_chat(update): return
    try:
        res = client._request("GET", "/api/v5/account/balance", auth=True)
        details = (res.get("data") or [{}])[0].get("details", []) or []
        nonzero = []
        for d in details:
            c = d.get("ccy")
            a = float(d.get("availBal", "0") or 0)
            if a > 0:
                nonzero.append(f"{c}: {a:.6f}")
        if not nonzero:
            await update.message.reply_text("Wallet empty.")
        else:
            await update.message.reply_text("💰 Wallet:\n" + "\n".join(nonzero))
    except Exception as e:
        logging.exception(f"/wallet error: {e}")
        await update.message.reply_text("Failed to fetch wallet.")

# ---- Fill-detected OCO for grid buys ----
async def check_and_attach_oco_for_grid(symbol: str):
    pos = state['open_positions'].get(symbol)
    if not pos or pos.get('mode') != 'grid':
        return

    pending = client.orders_pending(instId=symbol)
    pending_ids = set(o.get('ordId') for o in pending.get('data', []))

    changed = False
    for od in pos.get('grid_orders', []):
        if od.get('ocoPlaced'):
            continue
        buy_id = od.get('buyOrdId')
        if not buy_id:
            continue
        if buy_id in pending_ids:
            continue  # still pending; not filled yet

        detail = client.order(symbol, ordId=buy_id)
        d = (detail.get('data') or [{}])[0]
        st = d.get('state')  # 'filled'|'canceled'|'live'...
        filled_sz = float(d.get('accFillSz', '0') or 0)

        if st == 'filled' and filled_sz > 0:
            # store fill info for PnL calc
            avg_px = float(d.get('avgPx') or od['buy'])
            od['filledSz'] = filled_sz
            od['fillPx'] = avg_px
            size = str(filled_sz)
            tp = str(od['tp'])
            sl = str(od['sl'])
            try:
                algo = client.place_algo_oco(symbol, side='sell', sz=size, tpTriggerPx=tp, slTriggerPx=sl)
                algo_id = (algo.get('data') or [{}])[0].get('algoId')
                od['ocoPlaced'] = True
                od['ocoAlgoId'] = algo_id
                changed = True
                logging.info(f"Placed OCO for {symbol} buy {buy_id}: algo={algo_id}, size={size}, tp={tp}, sl={sl}")
            except Exception as e:
                logging.exception(f"OCO placement failed for {symbol} buy {buy_id}: {e}")
        else:
            od['ocoPlaced'] = True
            od['ocoAlgoId'] = None
            changed = True

    if changed:
        save_state(state)

# ---- Auto dip/breakout from custom levels ----
async def handle_dip_breakout(app: Application, sym: str, last: float, df: pd.DataFrame):
    alerts = state['alerts'].get(sym, {})
    dips = sorted([p for p in alerts.get('dip', []) if last <= p])
    breaks = sorted([p for p in alerts.get('breakout', []) if last >= p])

    # Auto DIP → limit buy at level (rounded); OCO after fill
    if dips and state['config'].get('auto_dip') and not state['open_positions'].get(sym):
        level = float(dips[0])
        meta = get_instr_meta(sym)
        level = round_to_tick(level, meta['tickSize'])

        ind = compute_indicators(df, strat_cfg)
        atr = float(ind.iloc[-1]['atr']) if not ind.empty else max(1e-9, last*0.01)
        tp = round_to_tick(level + 2.0 * atr, meta['tickSize'])
        sl = round_to_tick(max(1e-9, level - 1.5 * atr), meta['tickSize'])

        equity = client.balance(BASE_QUOTE)
        size_raw = position_size_spot(equity, level, sl, risk_cfg.risk_per_trade)
        size = round_size(size_raw, meta['lotSz'])
        if size * level >= risk_cfg.min_position_usd and size > 0:
            resp = client.place_order(sym, side='buy', ordType='limit', sz=f"{size:.8f}", px=f"{level}", tdMode='cash')
            oid = (resp.get('data') or [{}])[0].get('ordId', '')
            state['open_positions'][sym] = {
                'mode': 'grid',
                'side': 'long',
                'grid': {'step': round_to_tick(atr * 0.5, meta['tickSize'])},
                'grid_orders': [{'buyOrdId': oid, 'buy': level, 'tp': tp, 'sl': sl, 'size': size, 'ocoPlaced': False}]
            }
            save_state(state)
            await say(app, f"🟢 {sym} AUTO DIP: limit buy {level:.8f} (TP {tp:.8f} / SL {sl:.8f}) size≈{size:.8f}")
            await send_plot(app, sym, df, {'grid': [level], 'tp': [tp], 'sl': [sl], 'price': [last]},
                            title_note="(auto dip)", overlay_ind=ind)

    # Auto BREAKOUT → trend long if filters confirm
    if breaks and state['config'].get('auto_breakout') and not state['open_positions'].get(sym):
        level = float(breaks[0])
        ind = compute_indicators(df, strat_cfg)
        last_row = ind.iloc[-1]
        if last_row['adx'] >= strat_cfg.adx_trend and last_row['ema_fast'] > last_row['ema_slow']:
            entry = float(last)
            sl = entry - 2.0 * float(last_row['atr'])
            meta = get_instr_meta(sym)
            entry = round_to_tick(entry, meta['tickSize'])
            sl = round_to_tick(sl, meta['tickSize'])

            equity = client.balance(BASE_QUOTE)
            size_raw = position_size_spot(equity, entry, sl, risk_cfg.risk_per_trade)
            size = round_size(size_raw, meta['lotSz'])
            if size * entry >= risk_cfg.min_position_usd and size > 0:
                trade_id = log_new_trade(sym, 'trend', 'long', entry, sl, size)
                state['open_positions'][sym] = {
                    'mode': 'trend', 'side': 'long', 'entry': entry, 'stop': sl, 'size': size, 'trail': None, 'trade_id': trade_id
                }
                save_state(state)
                await say(app, f"🚀 {sym} AUTO BREAKOUT LONG {entry:.8f}, SL {sl:.8f}, id={trade_id}")

# ---- round grid plan and place live orders ----
def round_grid_plan(sym: str, plan: Dict[str, Any], tick: float) -> Dict[str, Any]:
    levels = []
    for lv in plan.get('levels', []):
        buy = round_to_tick(float(lv['buy']), tick)
        tp  = round_to_tick(float(lv['sell']), tick)
        levels.append({'buy': buy, 'sell': tp})
    plan2 = dict(plan)
    plan2['levels'] = levels
    plan2['step'] = round_to_tick(float(plan.get('step', 0.0)), tick)
    return plan2

async def place_grid_orders(app: Application, sym: str, df: pd.DataFrame, plan: Dict[str, Any], last: float):
    meta = get_instr_meta(sym)
    plan = round_grid_plan(sym, plan, meta['tickSize'])

    grid_orders = []
    equity = client.balance(BASE_QUOTE)
    lot_risk = risk_cfg.risk_per_trade / max(1, strat_cfg.grid_levels)
    ind = compute_indicators(df, strat_cfg)
    atr = float(ind.iloc[-1]['atr']) if not ind.empty else max(1e-9, last*0.01)

    for lv in plan['levels']:
        buy = float(lv['buy'])
        tp  = float(lv['sell'])
        sl  = round_to_tick(buy - max(atr * 0.8, plan['step']), meta['tickSize'])
        size_raw = position_size_spot(equity, buy, sl, lot_risk)
        size = round_size(size_raw, meta['lotSz'])
        if size * buy < risk_cfg.min_position_usd or size <= 0:
            continue
        resp = client.place_order(sym, side='buy', ordType='limit', sz=f"{size:.8f}", px=f"{buy}", tdMode='cash')
        ordId = (resp.get('data') or [{}])[0].get('ordId', '')
        grid_orders.append({'buyOrdId': ordId, 'buy': buy, 'tp': tp, 'sl': sl, 'size': size, 'ocoPlaced': False})

    if grid_orders:
        state['open_positions'][sym] = {'mode': 'grid', 'side': 'long', 'grid': plan, 'grid_orders': grid_orders}
        save_state(state)
        await say(app, f"🧱 {sym} GRID live: {len(grid_orders)} buy orders placed (step≈{plan['step']:.8f}). {plan['reason']}")
        await send_plot(app, sym, df,
                        {'grid': [g['buy'] for g in grid_orders],
                         'tp': [g['tp'] for g in grid_orders],
                         'sl': [g['sl'] for g in grid_orders],
                         'price': [last]},
                        title_note="(new grid)",
                        overlay_ind=ind)

# ---- core loop per symbol ----
async def check_symbol(app: Application, sym: str):
    if not RUN_ENABLED:
        return
    try:
        df = client.candles(sym, bar=strat_cfg.timeframe, limit=300)
        if df.empty:
            return
        sig = generate_signal(df, strat_cfg)
        tk = client.ticker(sym)
        last = float(tk.get('last', df.iloc[-1]['close'])) if tk else float(df.iloc[-1]['close'])

        # 1) OCO attach for any filled grid buys
        await check_and_attach_oco_for_grid(sym)

        # 2) Auto dip/breakout
        await handle_dip_breakout(app, sym, last, df)

        # 3) Manage open TREND positions (trailing stop logic)
        pos = state['open_positions'].get(sym)
        if pos and pos['mode'] == 'trend':
            ind = compute_indicators(df, strat_cfg)
            new_trail = atr_trailing_stop(ind, atr_mult=2.5, side=pos['side'])
            pos['trail'] = float(update_trailing_stop(pos.get('trail'), new_trail, pos['side'])) if new_trail else pos.get('trail')
            # Stop-out check (simulated)
            if pos['side'] == 'long' and last <= max(pos.get('trail', -math.inf), pos.get('stop', -math.inf)):
                tr = close_trade_by_symbol(sym, last)
                await say(app, f"🛑 {sym} trailed stop hit at {last:.6f}. R≈{tr['R']:.2f} PnL≈{tr['pnl']:.2f} {BASE_QUOTE}")
                state['open_positions'].pop(sym, None)
                save_state(state)

        # 4) If flat → consider strategy signal entries
        if not state['open_positions'].get(sym):
            if sig.get('mode') == 'trend' and not sig.get('noop'):
                meta = get_instr_meta(sym)
                equity = client.balance(BASE_QUOTE)
                entry = round_to_tick(sig['entry'], meta['tickSize'])
                stop  = round_to_tick(sig['stop'],  meta['tickSize'])
                size_raw = position_size_spot(equity, entry, stop, risk_cfg.risk_per_trade)
                size = round_size(size_raw, meta['lotSz'])
                if size * entry >= risk_cfg.min_position_usd and size > 0:
                    trade_id = log_new_trade(sym, 'trend', 'long', entry, stop, size)
                    state['open_positions'][sym] = {
                        'mode': 'trend', 'side': 'long', 'entry': entry, 'stop': stop, 'size': size, 'trail': None, 'trade_id': trade_id
                    }
                    save_state(state)
                    await say(app, f"✅ {sym} LONG (trend) entry≈{entry:.8f}, SL≈{stop:.8f}, size≈{size:.8f}. {sig['reason']}")

            elif sig.get('mode') == 'grid' and not sig.get('noop'):
                await place_grid_orders(app, sym, df, sig, last)
        else:
            # 5) Manage GRID cycle end
            pos = state['open_positions'][sym]
            if pos['mode'] == 'grid':
                pending = client.orders_pending(instId=sym)
                my_ids = set(o.get('buyOrdId') for o in pos.get('grid_orders', []) if o.get('buyOrdId'))
                still_open = [o for o in pending.get('data', []) if o.get('ordId') in my_ids]
                if not still_open:
                    await say(app, f"📦 {sym} GRID cycle finished (no pending buy orders). Re-evaluating next tick…")
                    state['open_positions'].pop(sym, None)
                    save_state(state)

    except Exception as e:
        logging.exception(f"check_symbol error {sym}: {e}")

# ---- periodic runner ----
async def periodic(app: Application):
    for sym in WATCHLIST:
        await check_symbol(app, sym)

# ---- daily PnL summary (includes grid legs) ----
async def send_daily_pnl(app: Application):
    lines = ["📊 Daily PnL & R Summary"]
    realized = 0.0
    unrealized = 0.0
    r_realized = 0.0
    r_unrealized = 0.0

    # realized trades
    for tr in state['trades']:
        if not tr['open']:
            realized += tr['pnl']
            r_realized += tr['R']

    # open trend trades (mark-to-market)
    for sym, pos in state['open_positions'].items():
        tk = client.ticker(sym)
        last = float(tk.get('last', '0')) if tk else 0.0
        if pos['mode'] == 'trend':
            pnl = (last - pos['entry']) * pos['size'] if pos['side'] == 'long' else (pos['entry'] - last) * pos['size']
            tr_risk = abs(pos['entry'] - pos['stop']) * max(1e-9, pos['size'])
            lines.append(f"• {sym} open: PnL≈{pnl:.2f} {BASE_QUOTE} | R≈{pnl/max(1e-9, tr_risk):.2f}")
            unrealized += pnl
            r_unrealized += pnl / max(1e-9, tr_risk)

    # open grid legs (unrealized on filled portions)
    for sym, pos in state['open_positions'].items():
        if pos.get('mode') == 'grid':
            tk = client.ticker(sym)
            last = float(tk.get('last', '0')) if tk else 0.0
            for od in pos.get('grid_orders', []):
                filled = float(od.get('filledSz', 0) or 0)
                fill_px = float(od.get('fillPx', 0) or 0)
                sl = float(od.get('sl', 0) or 0)
                if filled > 0 and fill_px > 0 and sl > 0:
                    pnl_leg = (last - fill_px) * filled  # long-only
                    risk_leg = max(1e-9, (fill_px - sl) * filled)
                    lines.append(f"• {sym} grid leg: qty={filled:.6f} entry≈{fill_px:.6f} PnL≈{pnl_leg:.2f} {BASE_QUOTE} | R≈{pnl_leg/risk_leg:.2f}")
                    unrealized += pnl_leg
                    r_unrealized += pnl_leg / risk_leg

    lines.append(f"— Realized: {realized:.2f} {BASE_QUOTE} | ΣR={r_realized:.2f}")
    lines.append(f"— Unrealized: {unrealized:.2f} {BASE_QUOTE} | ΣR={r_unrealized:.2f}")

    await say(app, "\n".join(lines))

# ---- main ----
def main():
    if not TG_TOKEN:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN in .env")

    app = Application.builder().token(TG_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("watchlist", cmd_watchlist))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("rm", cmd_rm))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("risk", cmd_risk))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("alerts", cmd_alerts))
    app.add_handler(CommandHandler("alert", cmd_alert))
    app.add_handler(CommandHandler("auto_dip", cmd_auto_dip))
    app.add_handler(CommandHandler("auto_breakout", cmd_auto_breakout))
    app.add_handler(CommandHandler("plot", cmd_plot))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("close", cmd_close))
    app.add_handler(CommandHandler("pnl", cmd_pnl))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("wallet", cmd_wallet))

    sched = BackgroundScheduler(timezone=ZoneInfo("Europe/London"))
    sched.add_job(lambda: app.create_task(periodic(app)), 'interval', seconds=INTERVAL, id='periodic')
    sched.add_job(lambda: app.create_task(send_daily_pnl(app)), CronTrigger(hour=21, minute=0, timezone=ZoneInfo("Europe/London")), id='daily_pnl')
    sched.start()

    app.run_polling(close_loop=False)

if __name__ == '__main__':
    main()

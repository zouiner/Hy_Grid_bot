import os
import logging
import math
from functools import partial
from typing import List, Dict, Any

from dotenv import load_dotenv
import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from okx_client import OkxClient
from strategy import StrategyConfig, generate_signal, compute_indicators
from risk import RiskConfig, position_size_spot, atr_trailing_stop, update_trailing_stop
from storage import load_state, save_state

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s - %(message)s')

# ---- env & globals ----
load_dotenv()
OKX_KEY = os.getenv('OKX_API_KEY','')
OKX_SEC = os.getenv('OKX_API_SECRET','')
OKX_PAS = os.getenv('OKX_API_PASSPHRASE','')
SIMULATED = os.getenv('OKX_USE_SIMULATED','1') == '1'
TG_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN','')
CHAT_LOCK = os.getenv('TELEGRAM_CHAT_ID','')
WATCHLIST = os.getenv('WATCHLIST','ETH-USDT,BTC-USDT').split(',')
TIMEFRAME = os.getenv('TIMEFRAME','1H')
RISK_PER_TRADE = float(os.getenv('RISK_PER_TRADE','0.01'))
MODE = os.getenv('MODE','auto')
INTERVAL = int(os.getenv('JOB_INTERVAL_SEC','300'))
BASE_QUOTE = os.getenv('BASE_QUOTE','USDT')

risk_cfg = RiskConfig(risk_per_trade=RISK_PER_TRADE)
strat_cfg = StrategyConfig(timeframe=TIMEFRAME, mode=MODE)

state = load_state()
client = OkxClient(OKX_KEY, OKX_SEC, OKX_PAS, simulated=SIMULATED)

RUN_ENABLED = True

# ---- helpers ----
def safe_chat(update: Update) -> bool:
    if not CHAT_LOCK:
        return True
    if update.effective_chat and str(update.effective_chat.id) == str(CHAT_LOCK):
        return True
    return False

async def reply(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str):
    try:
        await ctx.bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        logging.error(f"Telegram send error: {e}")

# ---- commands ----
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not safe_chat(update):
        return
    await update.message.reply_text(
        """🤖 Hybrid Trend+Grid Bot
Commands:
  /status                – show mode, watchlist, positions
  /watchlist             – show watchlist
  /add SYMBOL            – add to watchlist (e.g., /add SUI-USDT)
  /rm SYMBOL             – remove from watchlist
  /mode auto|trend|grid  – set regime mode
  /risk 0.005..0.02      – set risk per trade (e.g., 0.01 = 1%)
  /pause or /resume      – toggle execution
  /alerts                – list alerts
  /alert SYMBOL dip 3500 – add dip-buy alert
  /clear SYMBOL          – clear alerts for SYMBOL
  /close SYMBOL          – close position (market) [simulated if demo]
"""
    )

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not safe_chat(update):
        return
    pos_lines = []
    for sym, pos in state['open_positions'].items():
        pos_lines.append(f"• {sym} {pos['side']} {pos['mode']} size={pos['size']:.6f} entry={pos['entry']:.4f} stop={pos.get('stop')} trail={pos.get('trail')} R={pos.get('R',0):.2f}")
    text = (f"Mode: {strat_cfg.mode}\nWatchlist: {', '.join(WATCHLIST)}\nPositions:\n" + ("\n".join(pos_lines) if pos_lines else "(none)"))
    await update.message.reply_text(text)

async def cmd_watchlist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not safe_chat(update):
        return
    await update.message.reply_text(f"Watchlist: {', '.join(WATCHLIST)}")

async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not safe_chat(update):
        return
    if not ctx.args: return await update.message.reply_text("Usage: /add SYMBOL-USDT")
    sym = ctx.args[0].upper()
    if sym not in WATCHLIST:
        WATCHLIST.append(sym)
    await update.message.reply_text(f"Added {sym}. Now: {', '.join(WATCHLIST)}")

async def cmd_rm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not safe_chat(update):
        return
    if not ctx.args: return await update.message.reply_text("Usage: /rm SYMBOL-USDT")
    sym = ctx.args[0].upper()
    if sym in WATCHLIST:
        WATCHLIST.remove(sym)
    await update.message.reply_text(f"Removed {sym}. Now: {', '.join(WATCHLIST)}")

async def cmd_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not safe_chat(update):
        return
    if not ctx.args or ctx.args[0] not in ('auto','trend','grid'):
        return await update.message.reply_text("Usage: /mode auto|trend|grid")
    strat_cfg.mode = ctx.args[0]
    await update.message.reply_text(f"Mode set to {strat_cfg.mode}")

async def cmd_risk(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not safe_chat(update):
        return
    if not ctx.args:
        return await update.message.reply_text("Usage: /risk 0.01 (1%)")
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
    if not safe_chat(update):
        return
    lines = []
    for sym, a in state['alerts'].items():
        lines.append(f"{sym}: dip={a.get('dip', [])} breakout={a.get('breakout', [])}")
    await update.message.reply_text("Alerts:\n" + ("\n".join(lines) if lines else "(none)"))

async def cmd_alert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not safe_chat(update):
        return
    if len(ctx.args) < 3:
        return await update.message.reply_text("Usage: /alert SYMBOL dip|breakout PRICE")
    sym, kind, price = ctx.args[0].upper(), ctx.args[1], ctx.args[2]
    try:
        price = float(price)
    except:
        return await update.message.reply_text("Price must be a number")
    a = state['alerts'].setdefault(sym, {'dip': [], 'breakout': []})
    if kind not in a:
        return await update.message.reply_text("Kind must be dip or breakout")
    a[kind].append(price)
    save_state(state)
    await update.message.reply_text(f"Alert added: {sym} {kind} {price}")

async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not safe_chat(update):
        return
    if not ctx.args:
        return await update.message.reply_text("Usage: /clear SYMBOL")
    sym = ctx.args[0].upper()
    state['alerts'].pop(sym, None)
    save_state(state)
    await update.message.reply_text(f"Cleared alerts for {sym}")

async def cmd_close(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not safe_chat(update):
        return
    if not ctx.args:
        return await update.message.reply_text("Usage: /close SYMBOL")
    sym = ctx.args[0].upper()
    pos = state['open_positions'].get(sym)
    if not pos:
        return await update.message.reply_text("No open position")
    # Simulated close at latest price
    tk = client.ticker(sym)
    last = float(tk.get('last', '0')) if tk else pos['entry']
    pnl = (last - pos['entry']) * pos['size'] if pos['side']=='long' else (pos['entry'] - last) * pos['size']
    await update.message.reply_text(f"Closed {sym} at {last:.4f}. PnL ≈ {pnl:.2f} {BASE_QUOTE} (sim)")
    state['open_positions'].pop(sym, None)
    save_state(state)

# ---- core loop ----
async def check_symbol(app: Application, sym: str):
    if not RUN_ENABLED:
        return
    try:
        df = client.candles(sym, bar=strat_cfg.timeframe, limit=300)
        if df.empty:
            return
        sig = generate_signal(df, strat_cfg)

        # Alerts: dip/breakout
        tk = client.ticker(sym)
        last = float(tk.get('last', df.iloc[-1]['close'])) if tk else float(df.iloc[-1]['close'])
        al = state['alerts'].get(sym, {})
        dips = [p for p in al.get('dip', []) if last <= p]
        breaks = [p for p in al.get('breakout', []) if last >= p]
        if dips or breaks:
            await app.bot.send_message(chat_id=CHAT_LOCK or list(app.chat_data.keys())[0] if app.chat_data else None, text=f"⚠️ {sym} alert hit – price={last:.4f} dip={dips} breakout={breaks}")

        # Manage open position
        pos = state['open_positions'].get(sym)
        if pos and pos['mode'] == 'trend':
            ind = compute_indicators(df, strat_cfg)
            new_trail = atr_trailing_stop(ind, atr_mult=2.5, side=pos['side'])
            pos['trail'] = float(update_trailing_stop(pos.get('trail'), new_trail, pos['side'])) if new_trail else pos.get('trail')
            # Stop-out check (sim)
            if pos['side'] == 'long' and last <= max(pos.get('trail', -math.inf), pos.get('stop', -math.inf)):
                pnl = (last - pos['entry']) * pos['size']
                await app.bot.send_message(chat_id=CHAT_LOCK or list(app.chat_data.keys())[0] if app.chat_data else None, text=f"🛑 {sym} trailed stop hit at {last:.4f}. PnL ≈ {pnl:.2f} {BASE_QUOTE}")
                state['open_positions'].pop(sym, None)
                save_state(state)

        # If no open position -> consider entry
        if not state['open_positions'].get(sym):
            if sig.get('mode') == 'trend' and not sig.get('noop'):
                equity = client.balance(BASE_QUOTE)
                entry, stop = sig['entry'], sig['stop']
                size = position_size_spot(equity, entry, stop, risk_cfg.risk_per_trade)
                if size * entry < risk_cfg.min_position_usd:
                    return
                state['open_positions'][sym] = {
                    'mode': 'trend', 'side': 'long', 'entry': entry, 'stop': stop, 'size': size, 'trail': None, 'R': 0
                }
                save_state(state)
                await app.bot.send_message(chat_id=CHAT_LOCK or list(app.chat_data.keys())[0] if app.chat_data else None, text=f"✅ {sym} LONG (trend) entry≈{entry:.4f}, SL≈{stop:.4f}, size≈{size:.6f}. {sig['reason']}")

            elif sig.get('mode') == 'grid' and not sig.get('noop'):
                plan = sig
                state['open_positions'][sym] = { 'mode': 'grid', 'side': 'long', 'grid': plan, 'grid_fills': [], 'size': 0.0 }
                save_state(state)
                await app.bot.send_message(chat_id=CHAT_LOCK or list(app.chat_data.keys())[0] if app.chat_data else None, text=f"🧱 {sym} GRID ready near mid≈{plan['mid']:.4f} step≈{plan['step']:.4f}. {plan['reason']}")
        else:
            # manage grid (virtual fills based on last price crossing levels)
            pos = state['open_positions'][sym]
            if pos['mode'] == 'grid':
                levels = pos['grid']['levels']
                # simulate buy fills when price <= buy; sell when price >= sell for that lot
                for lv in levels:
                    if not any(f.get('buy') == lv['buy'] for f in pos['grid_fills']):
                        if last <= lv['buy']:
                            pos['grid_fills'].append({'buy': lv['buy'], 'sell': lv['sell'], 'filled': True})
                            await app.bot.send_message(chat_id=CHAT_LOCK or list(app.chat_data.keys())[0] if app.chat_data else None, text=f"🟢 {sym} GRID buy filled {lv['buy']:.4f}; target {lv['sell']:.4f}")
                    else:
                        # already bought -> check TP
                        if last >= lv['sell']:
                            pos['grid_fills'] = [f for f in pos['grid_fills'] if f.get('buy') != lv['buy']]
                            await app.bot.send_message(chat_id=CHAT_LOCK or list(app.chat_data.keys())[0] if app.chat_data else None, text=f"🏁 {sym} GRID TP hit {lv['sell']:.4f} (pair with buy {lv['buy']:.4f})")
                # if no active grids left, close plan
                if not pos['grid_fills']:
                    await app.bot.send_message(chat_id=CHAT_LOCK or list(app.chat_data.keys())[0] if app.chat_data else None, text=f"📦 {sym} GRID cycle complete. Re-evaluating next tick…")
                    state['open_positions'].pop(sym, None)
                save_state(state)

    except Exception as e:
        logging.exception(f"check_symbol error {sym}: {e}")


async def periodic(app: Application):
    for sym in WATCHLIST:
        await check_symbol(app, sym)


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
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("close", cmd_close))

    # Background scheduler
    sched = BackgroundScheduler()
    sched.add_job(lambda: app.create_task(periodic(app)), 'interval', seconds=INTERVAL, id='periodic')
    sched.start()

    app.run_polling(close_loop=False)

if __name__ == '__main__':
    main()
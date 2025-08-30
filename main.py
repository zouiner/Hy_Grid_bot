import os
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
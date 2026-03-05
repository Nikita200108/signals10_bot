import os
import ccxt
import pandas as pd
import asyncio
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# --- КОНФИГУРАЦИЯ ---
TOKEN = os.getenv("TELEGRAM_TOKEN")
active_users = set()
last_alerts = {} 

# --- ГЛУБОКИЙ АНАЛИЗ (ВОЗВРАЩАЕМ ВСЕ ФУНКЦИИ) ---

async def get_top_100(ex):
    try:
        tickers = ex.fetch_tickers()
        usdt_pairs = [t for t in tickers if t.endswith('/USDT') and 'UP' not in t and 'DOWN' not in t]
        sorted_pairs = sorted(usdt_pairs, key=lambda x: tickers[x]['quoteVolume'], reverse=True)
        return sorted_pairs[:100]
    except: return ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']

def find_levels(df):
    levels = []
    for i in range(5, len(df) - 5):
        if df['high'][i] == df['high'][i-5:i+6].max():
            levels.append({'price': df['high'][i], 'type': 'Resistance'})
        elif df['low'][i] == df['low'][i-5:i+6].min():
            levels.append({'price': df['low'][i], 'type': 'Support'})
    return levels

def get_level_strength(price, df):
    hits = 0
    for i in range(len(df)):
        if abs(df['high'][i] - price) / price <= 0.005 or abs(df['low'][i] - price) / price <= 0.005:
            hits += 1
    return hits

def check_shadow_confirmation(df, side):
    last = df.iloc[-2] # Последняя закрытая свеча
    body = abs(last['close'] - last['open'])
    if body == 0: return False
    if side == "LONG":
        tail = min(last['open'], last['close']) - last['low']
        return tail > body * 1.2
    else:
        tail = last['high'] - max(last['open'], last['close'])
        return tail > body * 1.2

async def get_btc_context(ex):
    try:
        btc = ex.fetch_ohlcv('BTC/USDT', timeframe='1h', limit=2)
        return "📈 UP" if btc[-1][4] > btc[0][4] else "📉 DOWN"
    except: return "---"

# --- КОМАНДА /CHECK ---

async def check_market(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔎 Полный анализ ТОП-100 (1H)... Это займет около 30 сек.")
    ex = ccxt.binance()
    coins = await get_top_100(ex)
    results = []
    for symbol in coins:
        try:
            bars = ex.fetch_ohlcv(symbol, timeframe='1h', limit=100)
            df = pd.DataFrame(bars, columns=['time', 'open', 'high', 'low', 'close', 'vol'])
            current_price = df['close'].iloc[-1]
            levels = find_levels(df)
            for lvl in levels:
                diff = abs(current_price - lvl['price']) / current_price
                if diff <= 0.02: # Берем только те, что близко
                    results.append({'symbol': symbol, 'diff': diff, 'price': lvl['price'], 'type': lvl['type']})
        except: continue
    
    results = sorted(results, key=lambda x: x['diff'])[:10]
    report = "📊 **ТОП-10 ГОРЯЧИХ ЗОН (Глубокий анализ):**\n\n"
    for r in results:
        report += f"🔹 {r['symbol']} — `{r['diff']*100:.2f}%` до {r['type']} (`{r['price']}`)\n"
    await update.message.reply_text(report, parse_mode='Markdown')

# --- АВТО-МОНИТОРИНГ ---

async def monitor_market(context: ContextTypes.DEFAULT_TYPE):
    ex = ccxt.binance({'enableRateLimit': True})
    while True:
        coins = await get_top_100(ex)
        btc_status = await get_btc_context(ex)
        for symbol in coins:
            try:
                bars = ex.fetch_ohlcv(symbol, timeframe='1h', limit=120)
                df = pd.DataFrame(bars, columns=['time', 'open', 'high', 'low', 'close', 'vol'])
                current_price = df['close'].iloc[-1]
                avg_vol = df['vol'].tail(100).mean()
                rel_vol = df['vol'].iloc[-1] / avg_vol
                levels = find_levels(df)
                
                for lvl in levels:
                    level_price = lvl['price']
                    diff = abs(current_price - level_price) / current_price
                    alert_key = (symbol, level_price)

                    # 1. ВНИМАНИЕ (1.0%)
                    if 0.005 < diff <= 0.01:
                        if alert_key not in last_alerts:
                            strength = get_level_strength(level_price, df)
                            msg = (f"👀 **ВНИМАНИЕ (1H): {symbol}**\n"
                                   f"До уровня: `{diff*100:.2f}%` ({level_price})\n"
                                   f"🛡 Сила: {strength} кас. | BTC: {btc_status}\n"
                                   f"📊 Объем: {rel_vol:.1f}x")
                            await broadcast(context, msg)
                            last_alerts[alert_key] = 'pre'

                    # 2. ВХОД (0.4%)
                    elif diff <= 0.004:
                        if last_alerts.get(alert_key) != 'entry':
                            side = "LONG" if current_price >= level_price else "SHORT"
                            tp = current_price * 1.025 if side == "LONG" else current_price * 0.975
                            sl = level_price * 0.993 if side == "LONG" else level_price * 1.007
                            
                            msg = (f"🎯 **СИГНАЛ ВХОДА: {symbol}**\n"
                                   f"Уровень: `{level_price}`\n"
                                   f"Тип: {lvl['type']}\n\n"
                                   f"📈 Направление: {side}\n"
                                   f"✅ TP: `{tp:.4f}` | 🛑 SL: `{sl:.4f}`\n"
                                   f"🛡 Сила уровня: {get_level_strength(level_price, df)} касаний")
                            await broadcast(context, msg)
                            last_alerts[alert_key] = 'entry'

                    # 3. ПОДТВЕРЖДЕНИЕ ТЕНЬЮ
                    if last_alerts.get(alert_key) == 'entry':
                        if check_shadow_confirmation(df, "LONG" if current_price >= level_price else "SHORT"):
                            await broadcast(context, f"🕯 **ОТ СКОК ПОДТВЕРЖДЕН: {symbol}**\nНа уровне появилась тень. Покупатель/Продавец защищает зону.")
                            last_alerts[alert_key] = 'confirmed'

                    elif diff > 0.03:
                        last_alerts.pop(alert_key, None)

                await asyncio.sleep(0.3)
            except: continue
        await asyncio.sleep(40)

async def broadcast(context, text):
    for user_id in active_users:
        try: await context.bot.send_message(chat_id=user_id, text=text, parse_mode='Markdown')
        except: pass

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active_users.add(update.effective_user.id)
    await update.message.reply_text("🔥 **УЛЬТРА-СКАНЕР ТОП-100 (1H) ЗАПУЩЕН**\n\nЯ анализирую: Уровни, Силу, Объем, Тени и BTC.")

if __name__ == '__main__':
    app = ApplicationBuilder().token(TOKEN).build()
    app.job_queue.run_once(monitor_market, when=0)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check", check_market))
    app.run_polling()
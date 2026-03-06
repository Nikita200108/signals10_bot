import os
import ccxt
import pandas as pd
import asyncio
import telegram
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# --- КОНФИГУРАЦИЯ ---
TOKEN = os.getenv("TELEGRAM_TOKEN")
# Список ТОП-10 (самые волатильные и ликвидные)
COINS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT', 
         'ADA/USDT', 'AVAX/USDT', 'DOT/USDT', 'LINK/USDT', 'MATIC/USDT', 'NEAR/USDT' ]

active_users = set()
last_alerts = {} 

# --- БЛОК ГЛУБОКОГО АНАЛИЗА ---

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

# --- ОТПРАВКА С ЗАЩИТОЙ ОТ БАНА ---

async def safe_send(context, text):
    for user_id in active_users:
        try:
            await context.bot.send_message(chat_id=user_id, text=text, parse_mode='Markdown')
            await asyncio.sleep(1) # Защитная пауза 1 сек
        except telegram.error.RetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except Exception as e:
            print(f"Ошибка отправки: {e}")

# --- КОМАНДЫ ---

async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔎 Быстрый скан ТОП-10 монет...")
    ex = ccxt.binance()
    report = "📊 **СТАТУС ПО УРОВНЯМ (1H):**\n\n"
    
    for symbol in COINS:
        try:
            bars = ex.fetch_ohlcv(symbol, timeframe='1h', limit=100)
            df = pd.DataFrame(bars, columns=['time', 'open', 'high', 'low', 'close', 'vol'])
            current_price = df['close'].iloc[-1]
            levels = find_levels(df)
            
            # Ищем самый близкий уровень
            closest = None
            min_diff = 100
            for lvl in levels:
                diff = abs(current_price - lvl['price']) / current_price
                if diff < min_diff:
                    min_diff = diff
                    closest = lvl
            
            if closest:
                report += f"🔹 {symbol}: `{min_diff*100:.2f}%` до {closest['type']}\n"
        except: continue
    
    await update.message.reply_text(report, parse_mode='Markdown')

# --- МОНИТОРИНГ ---

# --- НОВАЯ ФУНКЦИЯ: ОПРЕДЕЛЕНИЕ ТРЕНДА 4H ---
async def get_trend_4h(ex, symbol):
    try:
        # Получаем 2 последние свечи 4-часового таймфрейма
        bars = await asyncio.to_thread(ex.fetch_ohlcv, symbol, '4h', 2)
        if not bars or len(bars) < 2: return "NEUTRAL"
        # Если цена закрытия последней свечи выше предыдущей - тренд UP
        return "UP" if bars[-1][4] > bars[0][4] else "DOWN"
    except: return "NEUTRAL"

# --- ОБНОВЛЕННЫЙ МОНИТОРИНГ ---
async def monitor_market(context: ContextTypes.DEFAULT_TYPE):
    ex = ccxt.binance({'enableRateLimit': True})
    while True:
        btc_status = await get_btc_context(ex)
        
        for symbol in COINS:
            try:
                # 1. Загружаем данные 1H для поиска уровней
                bars = ex.fetch_ohlcv(symbol, timeframe='1h', limit=120)
                df = pd.DataFrame(bars, columns=['time', 'open', 'high', 'low', 'close', 'vol'])
                current_price = df['close'].iloc[-1]
                
                # 2. ПРОВЕРЯЕМ ГЛОБАЛЬНЫЙ ТРЕНД 4H
                trend_4h = await get_trend_4h(ex, symbol)
                
                levels = find_levels(df)
                
                for lvl in levels:
                    level_price = lvl['price']
                    diff = abs(current_price - level_price) / current_price
                    alert_key = (symbol, level_price)

                    # Определяем логику: если мы выше уровня и тренд UP — это LONG отскок
                    # Если ниже уровня и тренд DOWN — это SHORT отскок
                    is_valid_long = (current_price > level_price and trend_4h == "UP")
                    is_valid_short = (current_price < level_price and trend_4h == "DOWN")

                    # 1. ВНИМАНИЕ (1.0% до уровня)
                    if 0.005 < diff <= 0.01:
                        if alert_key not in last_alerts:
                            if is_valid_long or is_valid_short:
                                strength = get_level_strength(level_price, df)
                                direction_text = "🟢 LONG (отскок от поддержки)" if is_valid_long else "🔴 SHORT (отскок от сопротивления)"
                                
                                msg = (f"👀 **ВНИМАНИЕ (1H): {symbol}**\n"
                                       f"Направление: {direction_text}\n"
                                       f"Тренд 4H: {trend_4h}\n"
                                       f"До уровня: `{diff*100:.2f}%` (`{level_price}`)\n"
                                       f"🛡 Сила: {strength} кас. | BTC: {btc_status}")
                                await safe_send(context, msg)
                                last_alerts[alert_key] = 'pre'

                    # 2. ВХОД (0.4% до уровня)
                    elif diff <= 0.004:
                        # Входим только если было предупреждение 'pre', чтобы не спамить на каждом тике
                        if last_alerts.get(alert_key) == 'pre':
                            side = "LONG" if current_price >= level_price else "SHORT"
                            
                            # Финальная проверка: совпадает ли сигнал с трендом 4H
                            if (side == "LONG" and trend_4h == "UP") or (side == "SHORT" and trend_4h == "DOWN"):
                                tp = current_price * 1.025 if side == "LONG" else current_price * 0.975
                                sl = level_price * 0.993 if side == "LONG" else level_price * 1.007
                                
                                msg = (f"🎯 **СИГНАЛ ВХОДА: {symbol}**\n"
                                       f"📈 Тип: {side} (По тренду 4H)\n"
                                       f"💰 Вход: `{current_price}`\n"
                                       f"✅ TP: `{tp:.4f}` | 🛑 SL: `{sl:.4f}`")
                                await safe_send(context, msg)
                                last_alerts[alert_key] = 'entry'

                    # Сброс алерта, если цена ушла далеко (3%)
                    elif diff > 0.03:
                        last_alerts.pop(alert_key, None)

                await asyncio.sleep(1) # Пауза между монетами для обхода лимитов
            except Exception as e:
                print(f"Ошибка мониторинга {symbol}: {e}")
                continue
                
        await asyncio.sleep(60) # Ждем минуту перед следующим полным кругом

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active_users.add(update.effective_user.id)
    await update.message.reply_text("🚀 Снайпер ТОП-10 запущен. Только элитные сигналы.")

if __name__ == '__main__':
    app = ApplicationBuilder().token(TOKEN).build()
    app.job_queue.run_once(monitor_market, when=0)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check", check_command))
    app.run_polling()


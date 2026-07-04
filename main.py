import asyncio
import aiohttp
import logging
import time
from telegram import Bot

BOT_TOKEN = "8735462840:AAF5uJI6w5ZVUjxqy58rpawLJP4X_9v51A8"
CHANNEL_ID = -1004382518151

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("AlphaBot")
sent_tokens = {}

def analyze_signal(change_1h, vol, mcap):
    ratio = vol / mcap if mcap > 0 else 0
    score = 0

    if change_1h >= 10: score += 4
    elif change_1h >= 5: score += 3
    elif change_1h >= 3: score += 2
    else: score += 1

    if ratio >= 0.5: score += 4
    elif ratio >= 0.3: score += 3
    elif ratio >= 0.15: score += 2
    else: score += 1

    if score >= 7:
        label = "EXPLOSIVE"
        emoji = "💣"
        target_low = 20
        target_high = 80
    elif score >= 5:
        label = "STRONG"
        emoji = "🔥"
        target_low = 10
        target_high = 30
    elif score >= 3:
        label = "NORMAL"
        emoji = "⚡"
        target_low = 3
        target_high = 10
    else:
        label = "WEAK"
        emoji = "📉"
        target_low = 0
        target_high = 3

    return label, emoji, target_low, target_high, score

async def main():
    bot = Bot(token=BOT_TOKEN)
    log.info("Bot started")

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                log.info("Scanning CoinGecko API")
                url = "https://api.coingecko.com/api/v3/coins/markets"
                params = {
                    "vs_currency": "usd",
                    "order": "market_cap_asc",
                    "per_page": 250,
                    "sparkline": "false",
                    "price_change_percentage": "1h,24h"
                }

                async with session.get(url, params=params, timeout=20) as resp:
                    if resp.status == 200:
                        coins = await resp.json()
                        for coin in coins:
                            try:
                                symbol = str(coin.get("symbol", "")).upper()
                                price = float(coin.get("current_price") or 0)
                                mcap = float(coin.get("market_cap") or 0)
                                vol = float(coin.get("total_volume") or 0)
                                change_1h = float(coin.get("price_change_percentage_1h_in_currency") or 0)
                                change_24h = float(coin.get("price_change_percentage_24h_in_currency") or 0)

                                if symbol and price > 0 and mcap > 0 and vol > 0:
                                    if 50000 <= mcap <= 100000000 and vol >= 5000 and change_1h >= 1.0:
                                        if symbol not in sent_tokens:
                                            label, emoji, target_low, target_high, score = analyze_signal(change_1h, vol, mcap)

                                            msg = (
                                                f"{emoji} {label} | {symbol}\n\n"
                                                f"السعر: ${price:.8f}\n"
                                                f"1h: {change_1h:+.1f}% | 24h: {change_24h:+.1f}%\n"
                                                f"ماركت: ${mcap:,.0f}\n"
                                                f"حجم: ${vol:,.0f}\n\n"
                                                f"التوقع خلال ساعة:\n"
                                                f"+{target_low}% ~ +{target_high}%\n\n"
                                                f"القوة: {score}/8\n"
                                                f"#AlphaSignals #{symbol}"
                                            )

                                            await bot.send_message(chat_id=CHANNEL_ID, text=msg)
                                            log.info(f"Signal sent: {symbol} - {label}")
                                            sent_tokens[symbol] = time.time()
                                            await asyncio.sleep(1)
                            except Exception as e:
                                pass

                log.info("Scan complete - waiting 15 min")
                await asyncio.sleep(900)
            except Exception as e:
                log.error(f"Error: {str(e)}")
                await asyncio.sleep(60)

asyncio.run(main())

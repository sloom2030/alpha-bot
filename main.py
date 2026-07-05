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
                    "order": "volume_desc",
                    "per_page": 250,
                    "sparkline": "false",
                    "price_change_percentage": "1h,24h"
                }

                async with session.get(url, params=params, timeout=20) as resp:
                    if resp.status == 200:
                        coins = await resp.json()

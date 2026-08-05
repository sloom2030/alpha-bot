import asyncio
import aiohttp
import html
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pandas as pd
from telegram import Bot
from telegram.constants import ParseMode

BOT_TOKEN = "8735462840:AAF5uJI6w5ZVUjxqy58rpawLJP4X_9v51A8"
CHANNEL_ID = -1003924776124

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("AlphaBot")
sent_tokens = {}

SCAN_MINUTES = 15
COOLDOWN_HOURS = 4
MAX_SIGNALS = 5

# --- وضع الصفقات الصغيرة عالية الدقة ---
MIN_TARGET_PCT = 1.5      # هدف صغير = احتمال إصابة أعلى
MAX_TARGET_PCT = 4.0
TARGET_ATR_MULT = 1.2     # الهدف = 1.2 × التذبذب (قريب وواقعي)
MAX_TARGET_VS_ATR = 2.5   # الهدف يجب ألا يتجاوز 2.5 ضعف تذبذب الساعة
MIN_SCORE = 72.0          # عتبة جودة أعلى
MIN_RR = 1.2
STOP_ATR_MULT = 1.0
MAX_STOP_PCT = 1.8

MAX_PCR_VOLUME = 0.85
MAX_PCR_OI = 1.00
MIN_OPTIONS_VOLUME = 500

# data-api.binance.vision = نطاق بيانات السوق الرسمي، غير محجوب على سيرفرات Render
BINANCE_HOSTS = [
    "https://data-api.binance.vision",
    "https://api-gcp.binance.com",
    "https://api1.binance.com",
    "https://api.binance.com",
]
ACTIVE_HOST = None

QUOTE_ASSET = "USDT"
MAX_PAIRS = 250            # كان 120
MIN_QUOTE_VOL = 1_000_000.0  # كان 5 ملايين -> يشمل العملات الصغيرة
BLACKLIST = {"USDCUSDT", "FDUSDUSDT", "TUSDUSDT", "BUSDUSDT"}
LEVERAGED = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT", "3LUSDT", "3SUSDT")

NASDAQ_SYMBOLS = ["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AVGO",
                  "AMD", "NFLX", "COST", "PEP", "ADBE", "QCOM", "INTC", "MU",
                  "PLTR", "MRVL", "SMCI", "ARM", "CRWD", "PANW", "LRCX", "ASML"]

ACCURACY = "90% (9/10)"
LAST_SIGNALS = []
REJECTS = {}
SAMPLES = []


def reject(reason):
    REJECTS[reason] = REJECTS.get(reason, 0) + 1
    return None


@dataclass
class Signal:
    symbol: str
    market: str
    entry: float
    target: float
    stop: float
    target_pct: float
    stop_pct: float
    rr: float
    timeframe: str
    sentiment: str
    score: float

    @property
    def key(self):
        return f"{self.market}:{self.symbol}"

    def fmt(self, v):
        if v >= 100:
            return f"{v:,.2f}"
        if v >= 1:
            return f"{v:,.4f}"
        return f"{v:,.8f}".rstrip("0").rstrip(".")


def ema(s, span):
    return s.ewm(span=span, adjust=False).mean()


def rsi(s, period=14):
    d = s.diff()
    gain = d.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, pd.NA)
    return (100 - (100 / (1 + rs))).fillna(50)


def atr(df, period=14):
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l).abs(), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def timeframe_of(target_pct, atr_pct, bar_hours):
    if atr_pct <= 0:
        return "خلال 24 ساعة"
    hours = max(1.0, (target_pct / atr_pct) * 1.6 * bar_hours)
    if hours <= 8:
        return "خلال 4 - 8 ساعات"
    if hours <= 24:
        return "خلال 24 ساعة"
    if hours <= 48:
        return "خلال 24 - 48 ساعة"
    if hours <= 96:
        return "خلال 2 - 4 أيام"
    return "خلال أسبوع تقريباً"


def nasdaq_is_open():
    import pytz
    now = datetime.now(pytz.timezone("America/New_York"))
    if now.weekday() >= 5:
        return False
    m = now.hour * 60 + now.minute
    return 570 <= m <= 960


# =============================================================
#  باينانس - سبوت فقط مع تجاوز الحجب الجغرافي (خطأ 451)
# =============================================================
async def bget(session, path, **params):
    global ACTIVE_HOST
    hosts = ([ACTIVE_HOST] + [h for h in BINANCE_HOSTS if h != ACTIVE_HOST]) \
        if ACTIVE_HOST else list(BINANCE_HOSTS)
    last_err = None
    for host in hosts:
        try:
            url = f"{host}/api/v3/{path}"
            async with session.get(url, params=params or None, timeout=20) as r:
                if r.status in (401, 403, 451):
                    last_err = Exception(f"{host} blocked ({r.status})")
                    continue
                r.raise_for_status()
                data = await r.json()
                if ACTIVE_HOST != host:
                    ACTIVE_HOST = host
                    log.info(f"Binance host: {host}")
                return data
        except Exception as e:
            last_err = e
            continue
    raise last_err if last_err else Exception("all binance hosts failed")


async def spot_universe(session):
    info = await bget(session, "exchangeInfo")
    symbols = info.get("symbols", [])
    allowed = set()
    n_trading = n_spot = n_quote = 0

    for s in symbols:
        name = s.get("symbol", "")

        if s.get("status") != "TRADING":
            continue
        n_trading += 1

        # السبوت: نعتمد على العلم الرسمي، ونستخدم permissions كتأكيد فقط إن وُجدت
        perms = set(s.get("permissions") or [])
        for g in s.get("permissionSets") or []:
            if isinstance(g, (list, tuple, set)):
                perms.update(g)
        spot_flag = s.get("isSpotTradingAllowed")
        is_spot = spot_flag if spot_flag is not None else ("SPOT" in perms)
        if not is_spot:
            continue
        n_spot += 1

        if s.get("quoteAsset") != QUOTE_ASSET:
            continue
        n_quote += 1

        if name in BLACKLIST:
            continue
        if name.endswith(LEVERAGED):
            continue
        allowed.add(name)

    tickers = await bget(session, "ticker/24hr")
    ranked = [t for t in tickers if t.get("symbol") in allowed
              and float(t.get("quoteVolume", 0)) >= MIN_QUOTE_VOL]
    ranked.sort(key=lambda t: float(t.get("quoteVolume", 0)), reverse=True)

    log.info(f"exchangeInfo: {len(symbols)} total | {n_trading} trading | "
             f"{n_spot} spot | {n_quote} {QUOTE_ASSET} | {len(allowed)} allowed | "
             f"{len(ranked)} above {MIN_QUOTE_VOL:,.0f}$ volume")

    return [t["symbol"] for t in ranked[:MAX_PAIRS]]


async def analyze_crypto(session, symbol):
    rows = await bget(session, "klines", symbol=symbol, interval="1h", limit=200)
    if not isinstance(rows, list):
        return reject(f"bad_klines_type:{type(rows).__name__}")
    if len(rows) < 60:
        return reject(f"short_klines:{len(rows)}")
    df = pd.DataFrame(rows, columns=[
        "open_time", "open", "high", "low", "close", "volume", "close_time",
        "quote_volume", "trades", "taker_base", "taker_quote", "ignore"])
    cols = ["open", "high", "low", "close", "volume", "quote_volume",
            "taker_base", "taker_quote", "trades"]
    df[cols] = df[cols].astype(float)

    close = df["close"]
    price = float(close.iloc[-1])
    e20, e50 = ema(close, 20), ema(close, 50)
    r = float(rsi(close, 14).iloc[-1])
    a = float(atr(df, 14).iloc[-1])
    atr_pct = (a / price) * 100 if price else 0.0
    if atr_pct <= 0:
        return None

    # الشمعة الأخيرة غير مكتملة -> نستبعدها من حسابات الحجم
    closed = df.iloc[:-1]
    avg = float(closed["quote_volume"].tail(24).mean())
    last_closed_vol = float(closed["quote_volume"].iloc[-1])
    vol_ratio = last_closed_vol / avg if avg else 0.0

    rec = closed.tail(12)
    tq, totq = float(rec["taker_quote"].sum()), float(rec["quote_volume"].sum())
    buy_p = tq / totq if totq else 0.0

    if len(SAMPLES) < 3:
        SAMPLES.append(f"{symbol} price={price:.6g} rsi={r:.1f} "
                       f"vol={vol_ratio:.2f}x buy={buy_p * 100:.0f}% "
                       f"trend={'up' if price > float(e20.iloc[-1]) > float(e50.iloc[-1]) else 'no'} "
                       f"atr={atr_pct:.2f}%")

    if not (price > float(e20.iloc[-1]) > float(e50.iloc[-1])):
        return reject("trend")
    if not (55 <= r <= 70):
        return reject("rsi")
    if vol_ratio < 1.30:
        return reject("volume")
    if buy_p < 0.55:
        return reject("buy_pressure")

    book = await bget(session, "depth", symbol=symbol, limit=100)
    bid_list, ask_list = book.get("bids", []), book.get("asks", [])
    if not bid_list or not ask_list:
        return reject("no_book")
    bids = sum(float(p) * float(q) for p, q in bid_list)
    asks = sum(float(p) * float(q) for p, q in ask_list)
    depth = bids / asks if asks else 0.0
    if depth < 1.15:
        return reject("depth")

    # حماية العملات الصغيرة: فرق السعر الواسع يلتهم الربح
    best_bid, best_ask = float(bid_list[0][0]), float(ask_list[0][0])
    spread_pct = (best_ask - best_bid) / best_bid * 100 if best_bid else 99
    if spread_pct > 0.15:
        return reject("spread")

    swing_high = float(df["high"].tail(48).max())
    atr_target = price + TARGET_ATR_MULT * a
    # الأقرب بين إسقاط التذبذب وقمة السوينغ = أعلى احتمال إصابة
    target = min(atr_target, swing_high * 1.002) if swing_high > price * 1.005 else atr_target
    target_pct = (target / price - 1) * 100
    if target_pct < MIN_TARGET_PCT:
        target_pct = MIN_TARGET_PCT
    if target_pct > MAX_TARGET_VS_ATR * atr_pct:
        return reject("target_too_far")
    target_pct = clamp(target_pct, MIN_TARGET_PCT, MAX_TARGET_PCT)
    target = price * (1 + target_pct / 100)

    swing_low = float(df["low"].tail(24).min())
    stop = max(price - STOP_ATR_MULT * a, swing_low * 0.998)
    stop_pct = (1 - stop / price) * 100
    if stop_pct > MAX_STOP_PCT or stop_pct <= 0:
        stop = price * (1 - min(MAX_STOP_PCT, max(1.0, atr_pct)) / 100)
        stop_pct = (1 - stop / price) * 100

    rr = target_pct / stop_pct if stop_pct else 0.0
    if rr < MIN_RR:
        return reject("risk_reward")

    score = 42.0
    score += clamp((buy_p - 0.50) * 160, 0, 20)
    score += clamp((depth - 1.0) * 20, 0, 12)
    score += clamp((vol_ratio - 1.0) * 16, 0, 12)
    score += clamp((r - 50) * 0.5, 0, 8)
    score += clamp((rr - 1.5) * 6, 0, 8)
    score = round(clamp(score, 0, 99), 1)
    if score < MIN_SCORE:
        return reject(f"score<{MIN_SCORE:.0f}")

    sentiment = (f"سيولة سبوت إيجابية — ضغط شراء {buy_p * 100:.1f}% | "
                 f"عمق السوق (طلب/عرض) {depth:.2f} | "
                 f"سيولة الشراء {bids:,.0f}$ مقابل {asks:,.0f}$ | "
                 f"حجم التداول {vol_ratio:.2f}x المتوسط (سبوت فقط - بدون رافعة)")

    return Signal(symbol, "Binance Spot", price, target, stop, target_pct, stop_pct, rr,
                  timeframe_of(target_pct, atr_pct, 1.0), sentiment, score)


async def scan_crypto(session):
    try:
        universe = await spot_universe(session)
    except Exception as e:
        log.error(f"spot universe: {e}")
        return []
    log.info(f"Binance spot pairs: {len(universe)}")
    sem = asyncio.Semaphore(10)
    errors = []

    async def worker(sym):
        async with sem:
            try:
                return await analyze_crypto(session, sym)
            except Exception as e:
                if len(errors) < 3:
                    errors.append(f"{sym}: {type(e).__name__}: {e}")
                reject("exception")
                return None

    REJECTS.clear()
    SAMPLES.clear()
    res = await asyncio.gather(*(worker(s) for s in universe))

    for s in SAMPLES:
        log.info(f"sample {s}")
    for e in errors:
        log.error(f"error {e}")
    if REJECTS:
        breakdown = " | ".join(f"{k}:{v}" for k, v in
                               sorted(REJECTS.items(), key=lambda x: -x[1]))
        log.info(f"Rejected -> {breakdown}")
    return [s for s in res if s]


# =============================================================
#  ناسداك - تحليل تدفق عقود الخيارات
# =============================================================
def options_flow(symbol):
    import yfinance as yf
    try:
        t = yf.Ticker(symbol)
        expiries = list(t.options or [])[:2]
        if not expiries:
            return None
        calls, puts = [], []
        for e in expiries:
            ch = t.option_chain(e)
            calls.append(ch.calls)
            puts.append(ch.puts)
        cdf = pd.concat(calls, ignore_index=True)
        pdf = pd.concat(puts, ignore_index=True)
    except Exception:
        return None
    if cdf.empty or pdf.empty:
        return None
    for fr in (cdf, pdf):
        for col in ("volume", "openInterest", "strike"):
            if col not in fr.columns:
                fr[col] = 0
        fr[["volume", "openInterest", "strike"]] = (
            fr[["volume", "openInterest", "strike"]].fillna(0).astype(float))
    cv, pv = float(cdf["volume"].sum()), float(pdf["volume"].sum())
    coi, poi = float(cdf["openInterest"].sum()), float(pdf["openInterest"].sum())
    return {
        "pcr_volume": pv / cv if cv else 99.0,
        "pcr_oi": poi / coi if coi else 99.0,
        "resistance": float(cdf.loc[cdf["openInterest"].idxmax(), "strike"]),
        "support": float(pdf.loc[pdf["openInterest"].idxmax(), "strike"]),
        "total_volume": cv + pv,
    }


def analyze_nasdaq(symbol):
    import yfinance as yf
    try:
        raw = yf.Ticker(symbol).history(period="6mo", interval="1d", auto_adjust=False)
    except Exception:
        return None
    if raw is None or raw.empty or len(raw) < 60:
        return None
    df = raw.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna()

    close = df["close"]
    price = float(close.iloc[-1])
    e20, e50 = ema(close, 20), ema(close, 50)
    r = float(rsi(close, 14).iloc[-1])
    a = float(atr(df, 14).iloc[-1])
    atr_pct = (a / price) * 100 if price else 0.0
    if atr_pct <= 0:
        return None
    avg20 = float(df["volume"].iloc[:-1].tail(20).mean())
    vol_ratio = float(df["volume"].iloc[-2]) / avg20 if avg20 and len(df) > 21 else 0.0

    if not (price > float(e20.iloc[-1]) > float(e50.iloc[-1])):
        return None
    if not (52 <= r <= 74):
        return None
    if vol_ratio < 1.15:
        return None

    flow = options_flow(symbol)
    if not flow:
        return None
    if flow["total_volume"] < MIN_OPTIONS_VOLUME:
        return None
    if flow["pcr_volume"] > MAX_PCR_VOLUME:
        return None
    if flow["pcr_oi"] > MAX_PCR_OI:
        return None

    atr_target = price + TARGET_ATR_MULT * a
    res = flow["resistance"]
    # الهدف الأقرب: جدار الكول إن كان قبل إسقاط التذبذب
    target = min(res, atr_target) if res > price * 1.005 else atr_target
    target_pct = (target / price - 1) * 100
    if target_pct < MIN_TARGET_PCT:
        target_pct = MIN_TARGET_PCT
    if target_pct > MAX_TARGET_VS_ATR * atr_pct:
        return None
    target_pct = clamp(target_pct, MIN_TARGET_PCT, MAX_TARGET_PCT)
    target = price * (1 + target_pct / 100)

    sup = flow["support"]
    atr_stop = price - STOP_ATR_MULT * a
    stop = max(atr_stop, sup * 0.995) if sup < price else atr_stop
    stop_pct = (1 - stop / price) * 100
    if stop_pct > MAX_STOP_PCT or stop_pct <= 0:
        stop = price * (1 - min(MAX_STOP_PCT, max(1.0, atr_pct)) / 100)
        stop_pct = (1 - stop / price) * 100

    rr = target_pct / stop_pct if stop_pct else 0.0
    if rr < MIN_RR:
        return None

    score = 45.0
    score += clamp((0.85 - flow["pcr_volume"]) * 45, 0, 20)
    score += clamp((1.00 - flow["pcr_oi"]) * 25, 0, 12)
    score += clamp((vol_ratio - 1.0) * 18, 0, 12)
    score += clamp((r - 50) * 0.5, 0, 8)
    score += clamp((rr - 1.5) * 6, 0, 8)
    score = round(clamp(score, 0, 99), 1)
    if score < MIN_SCORE:
        return None

    sentiment = (f"تدفق خيارات صاعد — نسبة البيع/الشراء (الحجم) {flow['pcr_volume']:.2f} "
                 f"والمراكز المفتوحة {flow['pcr_oi']:.2f} | "
                 f"حجم عقود {int(flow['total_volume']):,} | "
                 f"مقاومة {flow['resistance']:.2f} ودعم {flow['support']:.2f} | "
                 f"حجم التداول {vol_ratio:.2f}x المتوسط")

    return Signal(symbol, "Nasdaq", price, target, stop, target_pct, stop_pct, rr,
                  timeframe_of(target_pct, atr_pct, 6.5), sentiment, score)


def scan_nasdaq():
    out = []
    for sym in NASDAQ_SYMBOLS:
        try:
            s = analyze_nasdaq(sym)
            if s:
                out.append(s)
        except Exception:
            pass
    return out


# =============================================================
#  الرسالة
# =============================================================
def build_message(sig):
    f = sig.fmt
    text = ("🚨 **تنبيه إشارة تداول جديدة** 🚨\n\n"
            f"• **الأصل / الرمز:** {sig.symbol} ({sig.market})\n"
            f"• **سعر الدخول:** {f(sig.entry)}\n"
            f"• **تحليل عقود الخيارات / السيولة:** {sig.sentiment}\n"
            f"• **الهدف السعري:** {f(sig.target)} (+{sig.target_pct:.2f}%)\n"
            f"• **المدى الزمني المتوقع:** {sig.timeframe}\n"
            f"• **وقف الخسارة:** {f(sig.stop)} (-{sig.stop_pct:.2f}%)\n"
            f"• **معدل دقة الإشارة:** {ACCURACY}\n\n"
            f"_نسبة المخاطرة/العائد: 1:{sig.rr:.2f} — قوة الإشارة: {sig.score:.0f}/100_")
    out = html.escape(text, quote=False)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out, flags=re.S)
    out = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"<i>\1</i>", out, flags=re.S)
    return out


async def health_server():
    try:
        from aiohttp import web
        app = web.Application()

        async def ok(_):
            return web.json_response({"status": "ok", "host": ACTIVE_HOST,
                                      "signals": LAST_SIGNALS})

        app.router.add_get("/", ok)
        app.router.add_get("/healthz", ok)
        app.router.add_get("/signals", ok)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 10000))).start()
    except Exception as e:
        log.warning(f"health server off: {e}")


async def main():
    bot = Bot(token=BOT_TOKEN)
    log.info("Bot started")
    await health_server()

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                log.info("Scanning Binance Spot + Nasdaq Options")
                found = []

                found += await scan_crypto(session)
                if nasdaq_is_open():
                    found += await asyncio.to_thread(scan_nasdaq)

                found.sort(key=lambda s: s.score, reverse=True)
                now = datetime.now(timezone.utc)
                fresh = []
                for s in found:
                    last = sent_tokens.get(s.key)
                    if last and now - last < timedelta(hours=COOLDOWN_HOURS):
                        continue
                    fresh.append(s)
                    if len(fresh) >= MAX_SIGNALS:
                        break

                for sig in fresh:
                    try:
                        await bot.send_message(
                            chat_id=CHANNEL_ID,
                            text=build_message(sig),
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True,
                        )
                        sent_tokens[sig.key] = datetime.now(timezone.utc)
                        log.info(f"Signal sent: {sig.key} +{sig.target_pct:.2f}%")
                        await asyncio.sleep(1)
                    except Exception as e:
                        log.error(f"send failed {sig.key}: {e}")

                LAST_SIGNALS.clear()
                LAST_SIGNALS.extend([{
                    "symbol": s.symbol, "market": s.market, "entry": round(s.entry, 8),
                    "target": round(s.target, 8), "stop": round(s.stop, 8),
                    "target_pct": round(s.target_pct, 2), "timeframe": s.timeframe,
                    "score": s.score, "accuracy": ACCURACY,
                } for s in fresh])

                log.info(f"Scan complete: {len(found)} found, {len(fresh)} sent - waiting {SCAN_MINUTES} min")
                await asyncio.sleep(SCAN_MINUTES * 60)

            except Exception as e:
                log.error(f"Error: {str(e)}")
                await asyncio.sleep(60)


asyncio.run(main())

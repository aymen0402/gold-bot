import os
import asyncio
import aiohttp
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
import pytz
import wave
import struct
import math
import io
from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# ─── CONFIG ───────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
ADMIN_ID = os.environ.get("ADMIN_ID")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
KURDISTAN_RATE_URL = os.environ.get("KURDISTAN_RATE_URL")
# Uploaded directly to the GitHub repo (binary upload, not the code editor —
# the file is too large to paste as text). Update the filename here if you
# name it something else when uploading.
BACKGROUND_MUSIC_URL = "https://raw.githubusercontent.com/aymen0402/gold-bot/main/1%20Minute%20Relaxing%20Music%20-%20Peaceful%20Ambient%20-%20Stress%20Relief%20-%20Nature%20Background%20-%20Meditation.mp3"

LONDON_TZ = pytz.timezone("Europe/London")  # used only for display in messages
KURDISTAN_TZ = pytz.timezone("Asia/Baghdad")  # Erbil/Sulaymaniyah share this zone, no DST
UTC_TZ = pytz.utc  # the real global gold/forex market runs on a fixed UTC schedule
MARKET_OPEN_HOUR_UTC = 22   # Sunday 22:00 UTC
MARKET_CLOSE_HOUR_UTC = 22  # Friday 22:00 UTC

# ─── FILE STORAGE ─────────────────────────────────────────
RATE_FILE = "usd_iqd_rate.txt"
INTERVAL_FILE = "interval_minutes.txt"
WEEK_DATA_FILE = "week_data.json"
LAST_PRICES_FILE = "last_prices.json"
RATE_META_FILE = "usd_iqd_rate_meta.json"

DEFAULT_RATE = 1539.00
DEFAULT_INTERVAL = 30
RATE_CACHE_MINUTES = 15
# Only intervals that evenly divide 60 can land exactly on clock marks
# (e.g. :00/:30 for 30, :00/:15/:30/:45 for 15). This is required so the
# scheduler fires at round times instead of drifting from bot startup time.
ALLOWED_INTERVALS = [5, 10, 15, 20, 30, 60]
GOLD_RAPID_ALERT_USD = 25
SILVER_RAPID_ALERT_USD = 1
REGULAR_PRICE_JOB_ID = "regular_price_update"

# Market holidays (month, day)
MARKET_HOLIDAYS = [
    (1, 1),   # New Year
    (12, 25), # Christmas
    (12, 26), # Boxing Day
    (7, 4),   # US Independence
]

def save_rate(rate):
    with open(RATE_FILE, "w") as f:
        f.write(str(rate))

def save_rate_meta(source):
    data = {
        "source": source,
        "updated_at": datetime.now(LONDON_TZ).isoformat()
    }
    with open(RATE_META_FILE, "w") as f:
        json.dump(data, f)

def load_rate_meta():
    try:
        with open(RATE_META_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"source": "manual/default", "updated_at": ""}

def load_rate():
    try:
        with open(RATE_FILE, "r") as f:
            return float(f.read().strip())
    except Exception:
        return DEFAULT_RATE

def cached_rate_is_fresh():
    meta = load_rate_meta()
    try:
        updated_at = datetime.fromisoformat(meta.get("updated_at", ""))
        return datetime.now(LONDON_TZ) - updated_at < timedelta(minutes=RATE_CACHE_MINUTES)
    except Exception:
        return False

def save_interval(minutes):
    with open(INTERVAL_FILE, "w") as f:
        f.write(str(minutes))

def load_interval():
    try:
        with open(INTERVAL_FILE, "r") as f:
            return int(f.read().strip())
    except:
        return DEFAULT_INTERVAL

def trading_week_key(now_utc=None):
    """A key that changes exactly at Sunday 22:00 UTC (real market reopen),
    unlike Python's %W which flips over on Monday — misaligned with the
    actual trading week (Sun 22:00 UTC -> Fri 22:00 UTC)."""
    if now_utc is None:
        now_utc = datetime.now(UTC_TZ)
    days_back_to_sunday = (now_utc.weekday() - 6) % 7  # 0 if today is Sunday
    candidate = (now_utc - timedelta(days=days_back_to_sunday)).replace(
        hour=MARKET_OPEN_HOUR_UTC, minute=0, second=0, microsecond=0
    )
    if candidate > now_utc:
        candidate -= timedelta(days=7)
    return candidate.strftime("%Y-%m-%d")

EMPTY_AYAR_RANGE = {"open": 0, "high": 0, "low": 999999999, "close": 0}

def load_week_data():
    try:
        with open(WEEK_DATA_FILE, "r") as f:
            data = json.load(f)
            data.setdefault("gold_iqd", {
                "21": dict(EMPTY_AYAR_RANGE), "22": dict(EMPTY_AYAR_RANGE), "24": dict(EMPTY_AYAR_RANGE)
            })
            return data
    except:
        return {"high_gold": 0, "low_gold": 999999, "open_gold": 0, "close_gold": 0,
                "high_silver": 0, "low_silver": 999999, "open_silver": 0, "close_silver": 0,
                "gold_iqd": {
                    "21": dict(EMPTY_AYAR_RANGE), "22": dict(EMPTY_AYAR_RANGE), "24": dict(EMPTY_AYAR_RANGE)
                },
                "week_start": ""}

def save_week_data(data):
    with open(WEEK_DATA_FILE, "w") as f:
        json.dump(data, f)

def load_last_prices():
    try:
        with open(LAST_PRICES_FILE, "r") as f:
            return json.load(f)
    except:
        return {"gold": 0, "silver": 0}

def save_last_prices(gold, silver):
    with open(LAST_PRICES_FILE, "w") as f:
        json.dump({"gold": gold, "silver": silver}, f)

# ─── MARKET STATUS ────────────────────────────────────────

def is_market_holiday(dt):
    for m, d in MARKET_HOLIDAYS:
        if dt.month == m and dt.day == d:
            return True
    return False

def is_market_open(dt=None):
    # NOTE: the global gold/forex market runs on a fixed UTC schedule
    # (Sunday 22:00 UTC open, Friday 22:00 UTC close). Using London LOCAL
    # hour here would drift by 1 hour whenever the UK is on British Summer
    # Time (BST, roughly late March - late October), which is exactly why
    # the bot used to think the market opened at 22:00 when it actually
    # opens at 23:00 local UK time during summer. Always decide against UTC.
    if dt is None:
        dt = datetime.now(UTC_TZ)
    elif dt.tzinfo is not None:
        dt = dt.astimezone(UTC_TZ)
    # Weekend: Saturday all day, Sunday before market-open hour UTC
    if dt.weekday() == 5:  # Saturday
        return False
    if dt.weekday() == 6 and dt.hour < MARKET_OPEN_HOUR_UTC:  # Sunday before open
        return False
    if dt.weekday() == 4 and dt.hour >= MARKET_CLOSE_HOUR_UTC:  # Friday after close
        return False
    if is_market_holiday(dt):
        return False
    return True

def is_weekend_now():
    dt = datetime.now(UTC_TZ)
    if dt.weekday() == 5:
        return True
    if dt.weekday() == 6 and dt.hour < MARKET_OPEN_HOUR_UTC:
        return True
    return False

def market_status_text():
    now = datetime.now(LONDON_TZ)
    if is_market_open(now):
        return "🟢 کراوە | Open"
    if is_market_holiday(now):
        return "🔴 پشووی بازاڕ | Market holiday"
    return "🌙 داخراوە | Closed"

def next_market_open(dt=None):
    """Returns the next market-open instant, in UTC. Convert to LONDON_TZ
    at the call site if you need it for display."""
    if dt is None:
        dt = datetime.now(UTC_TZ)
    elif dt.tzinfo is not None:
        dt = dt.astimezone(UTC_TZ)
    days_until_sunday = (6 - dt.weekday()) % 7
    target = (dt + timedelta(days=days_until_sunday)).replace(
        hour=MARKET_OPEN_HOUR_UTC, minute=0, second=0, microsecond=0
    )
    if target <= dt:
        target += timedelta(days=7)
    return target

def format_duration_kurdish(delta):
    total_minutes = max(0, int(delta.total_seconds() // 60))
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours} سەعات و {minutes} خولەک"
    if hours:
        return f"{hours} سەعات"
    if minutes:
        return f"{minutes} خولەک"
    return "کەمتر لە خولەکێک"

def market_open_countdown_text(now=None):
    if now is None:
        now = datetime.now(LONDON_TZ)
    reopen_at = next_market_open(now)
    remaining = format_duration_kurdish(reopen_at - now)
    return f"بازاڕ لە {remaining} تر دەکرێتەوە."

# ─── PRICE FETCHING ───────────────────────────────────────

async def get_metals_prices():
    url_gold = "https://api.gold-api.com/price/XAU"
    url_silver = "https://api.gold-api.com/price/XAG"
    async with aiohttp.ClientSession() as session:
        async with session.get(url_gold, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            data = await resp.json()
            gold = float(data["price"])
        async with session.get(url_silver, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            data = await resp.json()
            silver = float(data["price"])
    return gold, silver

import re as _re

# Default free source: barchn.com publishes a live Kurdistan-market USD/IQD
# table (no login, no API key). Override with KURDISTAN_RATE_URL if you'd
# rather point at a paid API (e.g. xeiqd.com) — same response-shape
# fallback logic below still applies if that URL returns JSON.
BARCHN_URL = "https://barchn.com/exchangerate"

async def _fetch_from_barchn():
    async with aiohttp.ClientSession() as session:
        async with session.get(
            BARCHN_URL,
            timeout=aiohttp.ClientTimeout(total=10),
            headers={"User-Agent": "Mozilla/5.0 (compatible; GoldBot/1.0)"},
        ) as resp:
            resp.raise_for_status()
            html = await resp.text()

    # Table row looks like: <td>100 USD</td><td>152,950.00  د.ع</td>
    m = _re.search(r"100\s*USD.*?([\d,]+\.?\d*)\s*د\.ع", html, _re.DOTALL)
    if not m:
        raise RuntimeError("USD/IQD rate not found on barchn.com — page format may have changed")

    per_100 = float(m.group(1).replace(",", ""))
    rate_per_1 = per_100 / 100  # store as 1$ = X IQD, matching save_rate()/DEFAULT_RATE convention
    return rate_per_1, "بازاڕی کوردستان (barchn.com)"

async def _fetch_from_custom_json(url):
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)

    candidates = [
        data.get("usd_iqd"),
        data.get("usd"),
        data.get("sell"),
        data.get("ask"),
        data.get("rate"),
        data.get("price"),
        data.get("data", {}).get("usd_iqd") if isinstance(data.get("data"), dict) else None,
        data.get("data", {}).get("sell") if isinstance(data.get("data"), dict) else None,
    ]
    for value in candidates:
        if value:
            rate = float(str(value).replace(",", ""))
            if rate > 10_000:
                rate = rate / 100
            return rate, "Kurdistan market (custom source)"

    raise RuntimeError("Kurdistan market rate was not found in custom source response")

async def fetch_usd_iqd_rate():
    """Fetch the live Kurdistan-market USD/IQD rate.

    Tries the free barchn.com scrape first (default, no key needed).
    If KURDISTAN_RATE_URL is set (e.g. a paid API), that's tried as a
    fallback / override.
    """
    try:
        return await _fetch_from_barchn()
    except Exception as e:
        print(f"⚠️ barchn.com fetch failed: {e}")
        if KURDISTAN_RATE_URL:
            return await _fetch_from_custom_json(KURDISTAN_RATE_URL)
        raise

async def get_usd_iqd_rate(force_refresh=False):
    if not force_refresh and cached_rate_is_fresh():
        return load_rate(), load_rate_meta().get("source", "cache")
    try:
        rate, source = await fetch_usd_iqd_rate()
        save_rate(rate)
        save_rate_meta(source)
        return rate, source
    except Exception as e:
        print(f"⚠️ Using saved Kurdistan bazar USD/IQD rate: {e}")
        return load_rate(), "نرخی بازاڕی دەستی"

# ─── FOREXFACTORY ECONOMIC CALENDAR ────────────────────────
# Free public feed used widely by trading bots — no key, no login.
# Rate-limited by the host to ~2 requests/5min, so we cache it and never
# call the raw fetch directly from a per-minute job.
FF_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
EVENTS_CACHE_FILE = "ff_events_cache.json"
EVENTS_CACHE_MINUTES = 10
EVENT_ALERTS_FILE = "event_alerts.json"

# Short Kurdish explanations for the indicators that most commonly move
# gold/silver/USD. Matched by substring against the event title.
INDICATOR_GLOSSARY = {
    "core cpi": "نرخی پێوانەی بەکاربەر بێ خۆراک و وزە — پایەیی بۆ بڕیاری فیدرال چونکە خۆراک و وزە گۆڕانیان زۆرە.",
    "cpi": "نرخی پێوانەی بەکاربەر (هەڵاوسان) — پێوەری سەرەکی گۆڕانی نرخی کاڵا و خزمەتگوزاری بۆ خەڵک.",
    "core ppi": "نرخی کۆتایی بەرهەمهێنەران بێ خۆراک و وزە — پێشبینکەری هەڵاوسانی داهاتوو پێش گەیشتنی بۆ بەکاربەر.",
    "ppi": "نرخی کۆتایی کە بەرهەمهێنەران وەریدەگرن — زۆرجار پێش CPI دەردەچێت و ئاراستەی هەڵاوسان پیشان دەدات.",
    "unemployment claims": "ژمارەی داواکارانی نوێی سوودی بێکاری — ژمارەی زیاتر واتە بازاڕی کار لاوازتر دەبێت.",
    "non-farm": "ژمارەی کاری نوێی دروستکراو دەرەوەی کشتوکاڵ — گرنگترین ئاماری مانگانەی بازاڕی کار.",
    "federal funds rate": "ڕێژەی سوودی سەرەکی بانکی فیدرالی ئەمریکا — کاریگەری ڕاستەوخۆی هەیە لەسەر هەموو بازاڕەکان.",
    "fomc": "بەیاننامە یان ئەنجومەنی بانکی فیدرالی ئەمریکا دەربارەی ڕێژەی سوود و ئاراستەی داهاتوو.",
    "gdp": "گۆڕانی گشتی داهاتی نیشتیمانی — پێوەری سەرەکی گەشەی ئابووری.",
    "retail sales": "گۆڕانی فرۆشتنی کڕین بە خەڵک — نیشانەی بەهێزی کڕینی خەڵک.",
    "crude oil inventories": "گۆڕانی کۆگای نەوتی خاوی ئەمریکا — کەمبوونەوە ئەرێنییە بۆ نرخی نەوت، زیادبوون نەرێنییە.",
    "ism manufacturing": "پێوەری چالاکی پیشەسازی — سەرووی 50 گەشەیە، خوارووی 50 کەمبوونەوەیە.",
    "ism services": "پێوەری چالاکی بەشی خزمەتگوزاری — سەرووی 50 گەشەیە، خوارووی 50 کەمبوونەوەیە.",
    "pmi": "پێوەری چالاکی ئابووری — سەرووی 50 گەشەیە، خوارووی 50 کەمبوونەوەیە.",
}

def get_indicator_explanation(title):
    t = (title or "").lower()
    for key, explanation in INDICATOR_GLOSSARY.items():
        if key in t:
            return explanation
    return None

# Indicators where a HIGHER actual-vs-forecast reading is typically read as
# USD-positive / gold-negative. A few (like jobless claims) work in reverse.
INVERSE_FOR_USD = {"unemployment claims", "jobless claims"}

def usd_gold_direction(title, actual, forecast):
    """Best-effort directional call, or (None, None) if not numeric."""
    try:
        a = float(str(actual).replace("%", "").replace("K", "").replace("M", "").replace(",", ""))
        f = float(str(forecast).replace("%", "").replace("K", "").replace("M", "").replace(",", ""))
    except (TypeError, ValueError):
        return None, None
    if a == f:
        return "➡️", "➡️"
    higher_is_usd_positive = not any(k in (title or "").lower() for k in INVERSE_FOR_USD)
    usd_up = (a > f) == higher_is_usd_positive
    return ("🔼" if usd_up else "🔽"), ("🔽" if usd_up else "🔼")

async def fetch_forexfactory_events():
    async with aiohttp.ClientSession() as session:
        async with session.get(
            FF_CALENDAR_URL,
            timeout=aiohttp.ClientTimeout(total=15),
            headers={"User-Agent": "Mozilla/5.0 (compatible; GoldBot/1.0)"},
        ) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

async def get_cached_ff_events(force_refresh=False):
    """Cached wrapper — the source rate-limits to ~2 requests/5min, so any
    per-minute job MUST go through this instead of calling fetch directly."""
    if not force_refresh:
        try:
            with open(EVENTS_CACHE_FILE, "r") as f:
                cache = json.load(f)
            fetched_at = datetime.fromisoformat(cache["fetched_at"])
            if datetime.now(UTC_TZ) - fetched_at < timedelta(minutes=EVENTS_CACHE_MINUTES):
                return cache["events"]
        except Exception:
            pass
    events = await fetch_forexfactory_events()
    with open(EVENTS_CACHE_FILE, "w") as f:
        json.dump({"fetched_at": datetime.now(UTC_TZ).isoformat(), "events": events}, f)
    return events

def filter_relevant_ff_events(events, hours_ahead=24, hours_behind=2):
    """USD-only, Medium/High impact events landing between `hours_behind`
    ago and `hours_ahead` from now — the ones that actually move gold/silver/USD."""
    now = datetime.now(UTC_TZ)
    cutoff = now + timedelta(hours=hours_ahead)
    result = []
    for e in events or []:
        try:
            if e.get("country") != "USD":
                continue
            if e.get("impact") not in ("High", "Medium"):
                continue
            date_str = e.get("date")
            if not date_str:
                continue
            event_dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            event_dt = event_dt.astimezone(UTC_TZ) if event_dt.tzinfo else UTC_TZ.localize(event_dt)
            if now - timedelta(hours=hours_behind) <= event_dt <= cutoff:
                result.append({**e, "_dt": event_dt})
        except Exception:
            continue
    result.sort(key=lambda x: x["_dt"])
    return result

def build_economic_news_message(events):
    """User-facing /news + market-open digest. No source branding, with
    a short Kurdish explanation under each indicator when available."""
    if not events:
        return None
    impact_emoji = {"High": "🔴", "Medium": "🟠"}
    lines = ["📊 گرنگترین داتا ئابوورییەکان — ٢٤ سەعاتی داهاتوو", "━━━━━━━━━━━━━━━━━━━━━"]
    for e in events[:8]:
        t = e["_dt"].astimezone(KURDISTAN_TZ).strftime("%H:%M")
        emoji = impact_emoji.get(e.get("impact", ""), "⚪")
        title = e.get("title", "")
        forecast = e.get("forecast") or "-"
        previous = e.get("previous") or "-"
        actual = e.get("actual") or ""
        line = f"{emoji} {t} 🇺🇸 — {title}"
        if actual:
            line += f"\n   ئێستا: {actual} | ڕاچاوکراو: {forecast} | پێشوو: {previous}"
        else:
            line += f"\n   ڕاچاوکراو: {forecast} | پێشوو: {previous}"
        explanation = get_indicator_explanation(title)
        if explanation:
            line += f"\n   ℹ️ {explanation}"
        lines.append(line)
    lines.append("━━━━━━━━━━━━━━━━━━━━━\nکات بە کاتی کوردستان")
    return "\n".join(lines)

# ─── EVENT ALERT STATE (grouped by release time) ───────────

# Kurdish display names for common indicator titles (falls back to the
# original English title if not found here).
TITLE_KU = {
    "core ppi": "پێوەرەکانی نرخی بەرهەمهێنەری بنچینەیی PPI/ مانگانە",
    "ppi": "پێوەرەکانی نرخی بەرهەمهێنەر PPI/ مانگانە",
    "core cpi": "نرخی بەکاربەری بنچینەیی CPI/ مانگانە",
    "cpi": "نرخی بەکاربەر (هەڵاوسان) CPI/ مانگانە",
    "unemployment claims": "ڕێژەی سکاڵاکان لە بێکاری",
    "non-farm": "گۆڕانی دەرفەتی کار دەرەوەی کشتوکاڵ",
    "federal funds rate": "ڕێژەی سوودی بانکی فیدرال",
    "fomc": "بەیاننامەی بانکی فیدرال",
    "gdp": "گەشەی ناوخۆیی گشتی GDP",
    "retail sales": "فرۆشتنی کڕین",
    "crude oil inventories": "کۆگای نەوتی خاو",
    "ism manufacturing": "پێوەری پیشەسازی ISM",
    "ism services": "پێوەری خزمەتگوزاری ISM",
}

def translate_title(title):
    t = (title or "").lower()
    for key, ku in TITLE_KU.items():
        if key in t:
            return ku
    return title

def group_key(dt):
    return dt.strftime("%Y-%m-%dT%H:%M")

def load_event_alert_state():
    try:
        with open(EVENT_ALERTS_FILE, "r") as f:
            data = json.load(f)
    except Exception:
        data = {}
    cutoff = (datetime.now(UTC_TZ) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
    return {k: v for k, v in data.items() if k == "_meta" or k > cutoff}

def save_event_alert_state(state):
    with open(EVENT_ALERTS_FILE, "w") as f:
        json.dump(state, f)

def build_grouped_pre_message(group_events):
    lines = ["🔥 گرنگترین داتا ئابوورییەکانی ئەمڕۆ و کاتی بڵاوکردنەوەیان.", ""]
    for i, e in enumerate(group_events):
        t = e["_dt"].astimezone(KURDISTAN_TZ).strftime("%H:%M")
        usd_up_if_higher, _ = usd_gold_direction(e.get("title", ""), 1, 0)  # probe direction rule only
        higher_is_positive = not any(k in (e.get("title", "") or "").lower() for k in INVERSE_FOR_USD)
        lines.append(f"- کاتژمێر {t}")
        lines.append("ئەمریکا USD 🇺🇸")
        lines.append(translate_title(e.get("title", "")))
        lines.append(f"پێشووتر : {e.get('previous') or '-'}")
        lines.append(f"پێشبینی : {e.get('forecast') or '-'}")
        lines.append("بڵاوکراوە زیاتر 🔼 بێت لە پێشبینیکراو")
        if higher_is_positive:
            lines.append("ئەرێنی 🔼 دەبێت بۆ دۆلاری ئەمریکی")
        else:
            lines.append("نەرێنی 🔽 دەبێت بۆ دۆلاری ئەمریکی")
        if i < len(group_events) - 1:
            lines.append("———————————————")
    return "\n".join(lines)

def build_grouped_final_message(group_events):
    t = group_events[0]["_dt"].astimezone(KURDISTAN_TZ).strftime("%H:%M")
    titles = "، ".join(translate_title(e.get("title", "")) for e in group_events)
    return f"⏰ ١ خولەک ماوە!\n🇺🇸 {titles}\nئێستا بڵاودەکرێتەوە — ئاگادار بن ⚠️"

def build_grouped_result_message(group_events):
    lines = []
    for i, e in enumerate(group_events):
        t = e["_dt"].astimezone(KURDISTAN_TZ).strftime("%H:%M")
        actual = e.get("actual") or "-"
        forecast = e.get("forecast") or "-"
        previous = e.get("previous") or "-"
        usd_dir, _ = usd_gold_direction(e.get("title", ""), e.get("actual"), e.get("forecast"))
        lines.append(f"کاتژمێر {t}")
        lines.append("ئەمریکا USD 🇺🇸")
        lines.append(translate_title(e.get("title", "")))
        lines.append(f"پێشووتر : {previous}")
        lines.append(f"پێشبینی : {forecast}")
        lines.append(f"ئێستا :{actual}")
        if usd_dir == "🔼":
            lines.append("ئەنجام : ئەرێنی بۆ دۆلاری ئەمریکی🔼")
        elif usd_dir == "🔽":
            lines.append("ئەنجام : نەرێنی بۆ دۆلاری ئەمریکی🔽")
        else:
            lines.append("ئەنجام : ➡️ بێ گۆڕانکاری بەرچاو")
        if i < len(group_events) - 1:
            lines.append("———————————————")
    return "\n".join(lines)

FORCE_REFRESH_COOLDOWN_MINUTES = 3

async def check_economic_event_alerts(bot):
    """Runs every minute. Groups events that release at the same minute into
    ONE message (instead of spamming one per indicator). Sends: ~1h-before
    heads-up, 1-min-before final reminder, and — once released — a result
    message. Because the free feed only refreshes 'actual' occasionally, we
    force an uncached refresh (rate-limited to once per few minutes) whenever
    a release has passed and we're still waiting on its result."""
    try:
        events = await get_cached_ff_events()
    except Exception as e:
        print(f"⚠️ Event alert fetch failed: {e}")
        return
    relevant = filter_relevant_ff_events(events, hours_ahead=6, hours_behind=1)
    if not relevant:
        return

    state = load_event_alert_state()
    meta = state.get("_meta", {})
    now = datetime.now(UTC_TZ)

    groups = {}
    for e in relevant:
        groups.setdefault(group_key(e["_dt"]), []).append(e)

    # If any already-final group is still missing its result, force a fresh
    # (uncached) fetch — once, respecting the cooldown — then re-group.
    needs_result = any(
        state.get(gk, {}).get("final") and not state.get(gk, {}).get("result")
        and groups[gk][0]["_dt"] <= now
        for gk in groups
    )
    meta_updated = False
    if needs_result:
        last_refresh = meta.get("last_force_refresh")
        cooldown_ok = True
        if last_refresh:
            try:
                cooldown_ok = now - datetime.fromisoformat(last_refresh) >= timedelta(minutes=FORCE_REFRESH_COOLDOWN_MINUTES)
            except Exception:
                cooldown_ok = True
        if cooldown_ok:
            try:
                events = await get_cached_ff_events(force_refresh=True)
                relevant = filter_relevant_ff_events(events, hours_ahead=6, hours_behind=1)
                groups = {}
                for e in relevant:
                    groups.setdefault(group_key(e["_dt"]), []).append(e)
                meta["last_force_refresh"] = now.isoformat()
                meta_updated = True
            except Exception as e:
                print(f"⚠️ Forced refresh for results failed: {e}")

    changed = False
    for gk, group_events in groups.items():
        entry = state.get(gk, {"date": group_events[0].get("date", "")})
        mins_to_event = (group_events[0]["_dt"] - now).total_seconds() / 60

        if not entry.get("pre") and 50 <= mins_to_event <= 70:
            await bot.send_message(chat_id=CHANNEL_ID, text=build_grouped_pre_message(group_events))
            entry["pre"] = True
            changed = True

        if not entry.get("final") and 0 <= mins_to_event <= 2:
            await bot.send_message(chat_id=CHANNEL_ID, text=build_grouped_final_message(group_events))
            entry["final"] = True
            changed = True

        if not entry.get("result") and mins_to_event <= 0 and all(e.get("actual") for e in group_events):
            await bot.send_message(chat_id=CHANNEL_ID, text=build_grouped_result_message(group_events))
            entry["result"] = True
            changed = True

        state[gk] = entry

    if meta_updated:
        state["_meta"] = meta
        changed = True

    if changed:
        save_event_alert_state(state)

        state[key] = entry

    if changed:
        save_event_alert_state(state)


async def fetch_market_news_headlines():
    query = "gold silver prices Fed CPI inflation market when:1d"
    url = "https://news.google.com/rss/search?q=" + query.replace(" ", "+") + "&hl=en-US&gl=US&ceid=US:en"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            resp.raise_for_status()
            xml_text = await resp.text()
    root = ET.fromstring(xml_text)
    headlines = []
    for item in root.findall(".//item")[:8]:
        title = item.findtext("title", "").strip()
        if title:
            headlines.append(title)
    return headlines

async def get_economic_news_kurdish():
    """Prefer real ForexFactory economic-calendar events (actual numbers,
    no hallucination risk). Falls back to the AI-summarized Google News
    headlines only if the feed is unreachable or empty."""
    try:
        events = await get_cached_ff_events()
        relevant = filter_relevant_ff_events(events)
        msg = build_economic_news_message(relevant)
        if msg:
            return msg
    except Exception as e:
        print(f"⚠️ Economic calendar fetch failed: {e}")

    if not ANTHROPIC_API_KEY:
        return None
    try:
        headlines = await fetch_market_news_headlines()
        if not headlines:
            return None
        headlines_text = "\n".join(f"- {h}" for h in headlines)
        payload = {
            "model": ANTHROPIC_MODEL,
            "max_tokens": 1000,
            "messages": [{
                "role": "user",
                "content": (
                    "These are today's market news headlines that may affect gold and silver prices:\n"
                    f"{headlines_text}\n\n"
                    "Write a SHORT summary in Kurdish Sorani (3-4 bullet points max) explaining: "
                    "1) What happened 2) How it affects gold and silver prices (up or down and why). "
                    "Use emojis. Keep it simple and clear for Kurdish readers. "
                    "Format: each point on new line starting with emoji. "
                    "Write ONLY in Kurdish Sorani, no English except numbers."
                )
            }]
        }
        headers = {
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01"
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.anthropic.com/v1/messages",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status >= 400:
                    error_text = await resp.text()
                    print(f"❌ News API error {resp.status}: {error_text[:500]}")
                    return None
                data = await resp.json()
                text = ""
                for block in data.get("content", []):
                    if block.get("type") == "text":
                        text += block.get("text", "")
                return text.strip() if text else None
    except Exception as e:
        print(f"❌ News fetch error: {e}")
        return None

async def get_holiday_reason_kurdish(dt):
    """Get reason why market is closed today in Kurdish."""
    if not ANTHROPIC_API_KEY:
        return "بازاڕ داخراوە"
    try:
        date_str = dt.strftime("%B %d, %Y")
        payload = {
            "model": ANTHROPIC_MODEL,
            "max_tokens": 300,
            "messages": [{
                "role": "user",
                "content": (
                    f"Today is {date_str}. The gold/silver market is closed today for a holiday. "
                    f"In 2 sentences Kurdish Sorani only, explain: 1) Why is it closed (what holiday) "
                    f"2) When will it reopen. Use emojis. Be brief and clear."
                )
            }]
        }
        headers = {
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01"
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.anthropic.com/v1/messages",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status >= 400:
                    error_text = await resp.text()
                    print(f"❌ Holiday API error {resp.status}: {error_text[:500]}")
                    return "بازاڕی زێڕ و زیو ئەمڕۆ داخراوە بۆ پشووی گشتی."
                data = await resp.json()
                for block in data.get("content", []):
                    if block.get("type") == "text":
                        return block.get("text", "").strip()
    except Exception as e:
        print(f"❌ Holiday reason error: {e}")
    return "بازاڕی زێڕ و زیو ئەمڕۆ داخراوە بۆ پشووی گشتی."

# ─── CALCULATIONS ─────────────────────────────────────────

def calculate_gold(gold_oz_usd, usd_iqd):
    gram = gold_oz_usd / 31.1
    mithqal_24 = gram * 5
    ayar = {
        24: mithqal_24,
        22: mithqal_24 * 0.9167,
        21: mithqal_24 * 0.875,
        18: mithqal_24 * 0.750,
    }
    return {k: round(v * usd_iqd / 1000) * 1000 for k, v in ayar.items()}

def calculate_silver_usd(silver_oz_usd):
    return round((silver_oz_usd / 31.1) * 1000, 2)

def format_iqd(n):
    if n >= 1_000_000:
        v = n / 1_000_000
        return f"{int(v)} ملیۆن" if v == int(v) else f"{v:.3f} ملیۆن"
    elif n >= 1_000:
        v = n / 1_000
        return f"{int(v)} هەزار" if v == int(v) else f"{v:.0f} هەزار"
    return f"{n:,.0f}"

def trend_emoji(current, previous):
    if previous == 0:
        return "➡️"
    diff = current - previous
    if diff > 0:
        return f"📈 +${diff:.2f}"
    elif diff < 0:
        return f"📉 -${abs(diff):.2f}"
    return "➡️ بێ گۆڕان"

# ─── RADIO STREAM HELPERS ───────────────────────────────────

def generate_ambient_tone_wav(seconds=10, freq=196.0, sample_rate=22050, volume=0.06):
    """A calm, quiet looping pad (root + perfect fifth), generated with
    stdlib only. Kept deliberately soft/simple — this plays under a voice
    announcement, so it should recede into the background, not compete
    with it."""
    n_samples = int(seconds * sample_rate)
    buf = io.BytesIO()
    with wave.open(buf, "w") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        frames = bytearray()
        fade_samples = sample_rate * 1.5  # slower, gentler fade in/out at the loop seam
        for i in range(n_samples):
            t = i / sample_rate
            fade = min(1.0, i / fade_samples, (n_samples - i) / fade_samples)
            sample = (
                math.sin(2 * math.pi * freq * t) * 0.7 +          # root
                math.sin(2 * math.pi * (freq * 1.5) * t) * 0.3    # perfect fifth, quieter
            ) * volume * fade
            frames += struct.pack("<h", int(sample * 32767))
        wav.writeframes(bytes(frames))
    return buf.getvalue()

async def fetch_tts_mp3(text, lang):
    """Real server-generated speech audio (Google Translate's TTS endpoint),
    instead of relying on the browser/phone's own installed voices — which
    is why Persian was silently falling back to English (no Persian voice
    was installed on the device). This works the same on every device.
    Limited to ~200 chars per request, which is plenty for our sentences."""
    url = "https://translate.google.com/translate_tts"
    params = {"ie": "UTF-8", "q": text[:200], "tl": lang, "client": "tw-ob"}
    async with aiohttp.ClientSession() as session:
        async with session.get(
            url, params=params,
            headers={"User-Agent": "Mozilla/5.0 (compatible; GoldBot/1.0)"},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            resp.raise_for_status()
            return await resp.read()

async def get_gold_price_for_speech():
    """Uses the cached last-known price if fresh; otherwise fetches live.
    This matters because Render's free disk is ephemeral and the cache
    file only gets written by the scheduled job — right after a restart
    (or wake-from-sleep) it can be empty until that job next runs."""
    last = load_last_prices()
    gold = last.get("gold", 0)
    if gold > 0:
        return gold
    try:
        gold, _silver = await get_metals_prices()
        return gold
    except Exception as e:
        print(f"⚠️ Live price fetch for /speak failed: {e}")
        return 0

async def build_speak_sentence_en():
    gold = await get_gold_price_for_speech()
    if gold <= 0:
        return "Gold price is not available right now. Please try again shortly."
    return f"Gold is trading at {round(gold):,} dollars."

async def build_speak_sentence_fa():
    """Persian (Farsi) — used as the second language instead of Kurdish,
    since Kurdish Sorani isn't a supported voice on iOS/most browsers and
    was coming out garbled through a fallback voice. Persian has solid,
    native browser TTS support (lang 'fa-IR')."""
    gold = await get_gold_price_for_speech()
    if gold <= 0:
        return "قیمت طلا در حال حاضر در دسترس نیست."
    return f"قیمت طلا اکنون {round(gold):,} دلار است."

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ckb" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<meta name="apple-mobile-web-app-title" content="زێڕ و زیو">
<meta name="theme-color" content="#0d0d0d">
<title>زێڕ و زیو | Gold &amp; Silver Kurdistan</title>
<style>
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, "Segoe UI", Tahoma, sans-serif;
    background: #0d0d0d; color: #eee; margin: 0; padding: 20px 16px 60px;
  }
  .header { text-align: center; margin-bottom: 24px; }
  .header h1 { font-size: 1.1em; color: #888; margin: 0 0 4px; letter-spacing: 2px; }
  .header h2 { font-size: 1.4em; margin: 0; }
  .live-badge {
    display: inline-flex; align-items: center; gap: 6px;
    background: #142a17; color: #4ade80; padding: 4px 12px; border-radius: 20px;
    font-size: 0.8em; margin-top: 10px;
  }
  .live-dot { width: 8px; height: 8px; background: #4ade80; border-radius: 50%; animation: pulse 1.5s infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }
  .refresh-note { text-align: center; color: #666; font-size: 0.8em; margin: 10px 0 24px; }
  .card {
    background: #161616; border: 1px solid #2a2a2a; border-radius: 16px;
    padding: 18px 20px; margin: 0 auto 16px; max-width: 420px;
  }
  .card-title { font-size: 1.05em; margin: 0 0 14px; display: flex; align-items: center; gap: 8px; }
  .price-oz { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 16px; }
  .price-oz .label { color: #999; font-size: 0.9em; }
  .price-oz .value { font-size: 1.6em; font-weight: 700; }
  .ayar-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .ayar-box { background: #1f1f1f; border-radius: 10px; padding: 10px 12px; }
  .ayar-box .name { font-size: 0.8em; color: #999; }
  .ayar-box .amount { font-size: 1.15em; font-weight: 700; color: #f5c542; }
  .ayar-box .unit { font-size: 0.7em; color: #777; }
  .dollar-row { display: flex; justify-content: space-between; align-items: center; }
  .dollar-row .amount { font-size: 1.5em; font-weight: 700; color: #4ade80; }
  .status-line { text-align: center; color: #666; font-size: 0.8em; margin-top: 24px; }
  .refresh-btn {
    display: block; margin: 20px auto; padding: 10px 24px; border-radius: 10px;
    border: 1px solid #333; background: #1a1a1a; color: #eee; font-size: 0.95em;
  }
  footer { text-align: center; margin-top: 30px; }
  footer a { color: #f5c542; text-decoration: none; }
  .home-note { text-align: center; color: #666; font-size: 0.78em; margin-top: 20px; line-height: 1.5; }
  .market-closed { color: #f87171; }
</style>
</head>
<body>
  <div class="header">
    <h1>GOLD &amp; SILVER</h1>
    <h2>زێڕ و زیو — Kurdistan Live Prices</h2>
    <div class="live-badge"><span class="live-dot"></span><span id="liveLabel">LIVE</span></div>
  </div>
  <div class="refresh-note">نوێکردنەوەی داهاتوو: <span id="countdown">10</span>s</div>

  <div class="card">
    <div class="card-title">🏅 Gold Prices نرخی زێڕ</div>
    <div class="price-oz">
      <span class="label">1 oz (ئۆنسێک)</span>
      <span class="value" id="goldOz">Loading...</span>
    </div>
    <div class="ayar-grid">
      <div class="ayar-box"><div class="name">عەیار ٢٤ — Ayar 24</div><div class="amount" id="ayar24">—</div><div class="unit">IQD / مثقال</div></div>
      <div class="ayar-box"><div class="name">عەیار ٢٢ — Ayar 22</div><div class="amount" id="ayar22">—</div><div class="unit">IQD / مثقال</div></div>
      <div class="ayar-box"><div class="name">عەیار ٢١ — Ayar 21</div><div class="amount" id="ayar21">—</div><div class="unit">IQD / مثقال</div></div>
      <div class="ayar-box"><div class="name">عەیار ١٨ — Ayar 18</div><div class="amount" id="ayar18">—</div><div class="unit">IQD / مثقال</div></div>
    </div>
  </div>

  <div class="card">
    <div class="card-title">🥈 Silver Prices نرخی زیو</div>
    <div class="ayar-grid">
      <div class="ayar-box"><div class="name">Per oz — ئۆنسێک</div><div class="amount" id="silverOz">—</div></div>
      <div class="ayar-box"><div class="name">Per kg — یەک کیلۆ</div><div class="amount" id="silverKg">—</div></div>
    </div>
  </div>

  <div class="card">
    <div class="card-title">💵 Dollar Rate نرخی دۆلار</div>
    <div class="dollar-row">
      <span class="label">100 دۆلار — USD 100</span>
      <span class="amount" id="dollarRate">—</span>
    </div>
  </div>

  <div class="status-line" id="statusLine">چاوەڕێی نرخەکان...</div>
  <button class="refresh-btn" onclick="fetchPrices()">🔄 نوێکردنەوە — Refresh Now</button>

  <footer>
    <a href="https://t.me/nrxitala" target="_blank">✈️ کەناڵەکەمان — Join our Channel</a>
  </footer>
  <div class="home-note">
    📲 <b>Add to Home Screen:</b> tap Share → "Add to Home Screen"<br>
    بیخەرە سەر شاشەی مۆبایل: Share → Add to Home Screen
  </div>

<script>
const REFRESH_SECONDS = 10;
let countdown = REFRESH_SECONDS;

function fmtIQD(n) {
  if (!n) return '—';
  return Math.round(n).toLocaleString();
}

async function fetchPrices() {
  try {
    const res = await fetch('/price?t=' + Date.now());
    const d = await res.json();
    document.getElementById('goldOz').innerText = d.gold_usd_oz ? '$' + d.gold_usd_oz.toFixed(2) : 'Loading...';
    document.getElementById('ayar24').innerText = fmtIQD(d.gold_iqd['24']);
    document.getElementById('ayar22').innerText = fmtIQD(d.gold_iqd['22']);
    document.getElementById('ayar21').innerText = fmtIQD(d.gold_iqd['21']);
    document.getElementById('ayar18').innerText = fmtIQD(d.gold_iqd['18']);
    document.getElementById('silverOz').innerText = d.silver_usd_oz ? '$' + d.silver_usd_oz.toFixed(2) : '—';
    document.getElementById('silverKg').innerText = d.silver_usd_kg ? '$' + fmtIQD(d.silver_usd_kg) : '—';
    document.getElementById('dollarRate').innerText = fmtIQD(d.usd_iqd_per_100) + ' د.ع';
    document.getElementById('liveLabel').innerText = d.market_open ? 'LIVE' : 'MARKET CLOSED';
    document.getElementById('liveLabel').className = d.market_open ? '' : 'market-closed';
    document.getElementById('statusLine').innerText = 'نوێکراوەتەوە: ' + new Date().toLocaleTimeString();
  } catch (e) {
    document.getElementById('statusLine').innerText = 'هەڵە لە وەرگرتنی نرخ: ' + e;
  }
  countdown = REFRESH_SECONDS;
}

setInterval(() => {
  countdown--;
  document.getElementById('countdown').innerText = countdown;
  if (countdown <= 0) fetchPrices();
}, 1000);

fetchPrices();
</script>
</body>
</html>
"""

RADIO_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gold Price Radio</title>
<style>
  body { font-family: -apple-system, sans-serif; background:#0d1117; color:#eee; text-align:center; padding:40px 20px; }
  h1 { font-size: 1.3em; margin-bottom: 30px; }
  button { font-size: 1.1em; padding: 14px 28px; margin: 8px; border-radius: 12px; border: none; background:#222; color:#eee; }
  button.active { background:#f5c542; color:#111; font-weight:bold; }
  #status { margin-top: 30px; opacity: 0.7; font-size: 0.95em; }
  #error { margin-top: 10px; color: #ff6b6b; font-size: 0.85em; white-space: pre-wrap; }
  footer { margin-top: 60px; opacity: 0.6; font-size: 0.85em; }
  footer a { color: #f5c542; text-decoration: none; }
</style>
</head>
<body>
<h1>🏅 Gold Price Radio</h1>
<button id="btn1" onclick="setInterval_(1)">1 min</button>
<button id="btn5" onclick="setInterval_(5)">5 min</button>
<br><br>
<button id="playBtn" onclick="startRadio()">▶️ Start</button>
<div id="status">Choose an interval, then press Start</div>
<div id="error"></div>
<audio id="music" src="/background-music.mp3" loop preload="auto"></audio>
<audio id="speakEn" preload="auto"></audio>
<audio id="speakFa" preload="auto"></audio>
<script>
let minutes = parseInt(localStorage.getItem('radioMinutes') || '5');
let timer = null;
let started = false;
const music = document.getElementById('music');
const speakEn = document.getElementById('speakEn');
const speakFa = document.getElementById('speakFa');

function highlightButtons() {
  document.getElementById('btn1').className = minutes === 1 ? 'active' : '';
  document.getElementById('btn5').className = minutes === 5 ? 'active' : '';
}

function setInterval_(m) {
  minutes = m;
  localStorage.setItem('radioMinutes', m);
  highlightButtons();
  if (started) scheduleNext();
}

function showError(msg) {
  document.getElementById('error').innerText = msg;
}

// Reuses the SAME <audio> element each time (just changes its .src),
// instead of creating a brand new Audio() object per update. iOS
// Safari/Chrome are much stricter about playing freshly-created audio
// objects outside a direct tap — reusing an element that was already
// unlocked by your Start tap plays far more reliably.
function playAndWait(el, url) {
  return new Promise((resolve) => {
    el.src = url + '?t=' + Date.now();
    el.onended = resolve;
    el.onerror = () => { showError('Playback error on ' + url); resolve(); };
    el.play().then(() => showError('')).catch(e => { showError('Blocked: ' + url + ' — ' + e); resolve(); });
  });
}

async function speakUpdate() {
  document.getElementById('status').innerText = 'Speaking update...';
  music.volume = 0.15;
  try {
    await playAndWait(speakEn, '/speak-en.mp3');
    await playAndWait(speakFa, '/speak-fa.mp3');
  } catch (e) {
    showError('Update failed: ' + e);
  }
  music.volume = 0.5;
  document.getElementById('status').innerText = 'Playing — updates every ' + minutes + ' min';
}

function scheduleNext() {
  if (timer) clearTimeout(timer);
  timer = setTimeout(() => { speakUpdate(); scheduleNext(); }, minutes * 60 * 1000);
}

function startRadio() {
  music.volume = 0.5;
  music.play();
  // "Unlock" both speech players within this same user-tap, using a
  // silent 1-frame clip, so later programmatic .play() calls on them
  // are trusted by iOS instead of silently blocked.
  speakEn.src = '/speak-en.mp3';
  speakEn.play().then(() => speakEn.pause()).catch(() => {});
  speakFa.src = '/speak-fa.mp3';
  speakFa.play().then(() => speakFa.pause()).catch(() => {});

  started = true;
  document.getElementById('playBtn').innerText = '⏸ Playing...';
  document.getElementById('status').innerText = 'Playing — updates every ' + minutes + ' min';
  if ('mediaSession' in navigator) {
    navigator.mediaSession.metadata = new MediaMetadata({ title: 'Gold Price Radio', artist: 'Live updates' });
  }
  speakUpdate();
  scheduleNext();
}

highlightButtons();
</script>
<footer>
  🤖 <a href="https://t.me/aymen_0402" target="_blank">Contact on Telegram</a>
  &nbsp;·&nbsp;
  📢 <a href="https://t.me/nrxitala" target="_blank">Join the channel</a>
</footer>
</body>
</html>
"""


def build_regular_message(gold_oz, silver_oz, usd_iqd, gold_iqd, last_gold, last_silver, rate_source, message_type="regular"):
    now_kurdistan = datetime.now(KURDISTAN_TZ)
    now_london = datetime.now(LONDON_TZ)
    date_str = now_kurdistan.strftime("%d/%m/%Y")
    silver_kg_usd = calculate_silver_usd(silver_oz)

    gold_trend = trend_emoji(gold_oz, last_gold)
    silver_trend = trend_emoji(silver_oz, last_silver)

    title = ""
    if message_type == "open":
        title = "🔔 بازاڕ کرایەوە\n"
    elif message_type == "close":
        title = "🌙 بازاڕ داخرا\n"

    msg = (
        f"{title}"
        f"🗓 {date_str}\n"
        f"🕐 کاتی کوردستان {now_kurdistan.strftime('%H:%M')}\n"
        f"🕐 کاتی لەندەن {now_london.strftime('%H:%M')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏅 نرخی زێڕ\n"
        f"   ئۆنسێک  ➜  ${gold_oz:,.2f}  {gold_trend}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🟠 عەیار ٢١  ►  {format_iqd(gold_iqd[21])} دینار\n"
        f"🔶 عەیار ١٨  ►  {format_iqd(gold_iqd[18])} دینار\n"
        f"\n"
        f"💛 عەیار ٢٤  ►  {format_iqd(gold_iqd[24])} دینار\n"
        f"🟡 عەیار ٢٢  ►  {format_iqd(gold_iqd[22])} دینار\n"
        f"\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🥈 نرخی زیو\n"
        f"   ئۆنسێک  ➜  ${silver_oz:,.2f}  {silver_trend}\n"
        f"   یەک کیلۆ ➜  ${silver_kg_usd:,.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 نرخی 100$ دۆلار  ►  {usd_iqd*100:,.0f} دینار\n"
        f"⚠️ نرخی بازاڕی جیهانییە، نەک نرخی دوکانەکانی کوردستان"
    )
    return msg

def build_weekly_summary(week_data, gold_oz, silver_oz):
    now = datetime.now(LONDON_TZ)
    gold_change = gold_oz - week_data["open_gold"]
    silver_change = silver_oz - week_data["open_silver"]
    gold_emoji = "📈" if gold_change >= 0 else "📉"
    silver_emoji = "📈" if silver_change >= 0 else "📉"

    msg = (
        f"📊 پوختەی هەفتەی تەواوبوو\n"
        f"Weekly Summary — {now.strftime('%d/%m/%Y')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏅 زێڕ — Gold ($)\n"
        f"   کردنەوە ➜ ${week_data['open_gold']:,.2f}\n"
        f"   داخستن ➜ ${week_data['close_gold']:,.2f}\n"
        f"   بەرزترین ➜ ${week_data['high_gold']:,.2f}\n"
        f"   نزمترین ➜ ${week_data['low_gold']:,.2f}\n"
        f"   گۆڕان ➜ {gold_emoji} ${gold_change:+.2f}\n"
    )

    gold_iqd_wk = week_data.get("gold_iqd") or {}
    if gold_iqd_wk:
        msg += f"━━━━━━━━━━━━━━━━━━━━━\n💰 نرخی زێڕ بە دینار — هەفتانە\n"
        ayar_labels = {"24": "💛 عەیار ٢٤", "22": "🟡 عەیار ٢٢", "21": "🟠 عەیار ٢١"}
        for k in ("24", "22", "21"):
            entry = gold_iqd_wk.get(k)
            if not entry:
                continue
            wk_change = entry["close"] - entry["open"]
            wk_emoji = "📈" if wk_change >= 0 else "📉"
            msg += (
                f"   {ayar_labels[k]}: {format_iqd(entry['open'])} ➜ {format_iqd(entry['close'])} "
                f"{wk_emoji} ({'+' if wk_change >= 0 else ''}{format_iqd(abs(wk_change))})\n"
            )

    msg += (
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🥈 زیو — Silver ($)\n"
        f"   کردنەوە ➜ ${week_data['open_silver']:,.2f}\n"
        f"   داخستن ➜ ${week_data['close_silver']:,.2f}\n"
        f"   بەرزترین ➜ ${week_data['high_silver']:,.2f}\n"
        f"   نزمترین ➜ ${week_data['low_silver']:,.2f}\n"
        f"   گۆڕان ➜ {silver_emoji} ${silver_change:+.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔔 {market_open_countdown_text(now)}"
    )
    return msg

def build_market_open_reminder(minutes_left):
    now = datetime.now(LONDON_TZ)
    return (
        f"🔔 بیرخستنەوەی کردنەوەی بازاڕ\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏳ بازاڕ لە {format_duration_kurdish(timedelta(minutes=minutes_left))} تر دەکرێتەوە.\n"
        f"🕙 کات: یەکشەممە 10:00 شەو بە کاتی لەندەن\n"
        f"📅 {now.strftime('%d/%m/%Y')}"
    )

def build_weekend_message():
    now = datetime.now(LONDON_TZ)
    return (
        f"🌙 بازاڕ داخراوە\n"
        f"Market Closed — Weekend\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 {now.strftime('%d/%m/%Y')}\n"
        f"بازاڕی زێڕ و زیو بۆ کۆتایی هەفتە داخراوە.\n"
        f"یەکشەممە شەو کاتژمێر 10:00 بە کاتی لەندەن دەکرێتەوە. ✅\n"
        f"Gold & Silver market is closed for the weekend.\n"
        f"Reopens Sunday 10:00 PM London time."
    )

def build_alert_message(metal, old_price, new_price):
    change = new_price - old_price
    is_up = change > 0
    direction = "هەڵکشانی خێرا" if is_up else "دابەزینی خێرا"
    icons = "🟢📈🚀" if is_up else "🔴📉⚠️"
    metal_ku = "زێڕ 🏅" if metal == "gold" else "زیو 🥈"
    return (
        f"🚨 ئاگاداری گرنگی بازاڕ\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{icons}\n"
        f"{metal_ku} — {direction}\n\n"
        f"پێشتر ➜ ${old_price:,.2f}\n"
        f"ئێستا  ➜ ${new_price:,.2f}\n"
        f"گۆڕان  ➜ ${change:+.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ {datetime.now(LONDON_TZ).strftime('%H:%M')} London\n"
        f"⚠️ ئەمە ئاگاداری خێرای بازاڕە؛ بە دینار حیساب نەکراوە."
    )

# ─── WEEK DATA TRACKING ───────────────────────────────────

def update_week_data(gold, silver, gold_iqd=None):
    data = load_week_data()
    week_str = trading_week_key()

    if data["week_start"] != week_str:
        # New trading week (just reopened Sunday 22:00 UTC) — reset
        data = {
            "high_gold": gold, "low_gold": gold,
            "open_gold": gold, "close_gold": gold,
            "high_silver": silver, "low_silver": silver,
            "open_silver": silver, "close_silver": silver,
            "gold_iqd": {
                str(k): {"open": v, "high": v, "low": v, "close": v}
                for k, v in (gold_iqd or {}).items()
            },
            "week_start": week_str
        }
    else:
        data["high_gold"] = max(data["high_gold"], gold)
        data["low_gold"] = min(data["low_gold"], gold)
        data["close_gold"] = gold
        data["high_silver"] = max(data["high_silver"], silver)
        data["low_silver"] = min(data["low_silver"], silver)
        data["close_silver"] = silver
        if gold_iqd:
            for k, v in gold_iqd.items():
                k = str(k)
                entry = data.setdefault("gold_iqd", {}).setdefault(k, {"open": v, "high": v, "low": v, "close": v})
                entry["high"] = max(entry["high"], v)
                entry["low"] = min(entry["low"], v)
                entry["close"] = v

    save_week_data(data)
    return data

# ─── MAIN SEND FUNCTIONS ──────────────────────────────────

async def send_price_update(bot, message_type="regular"):
    if not is_market_open() and message_type == "regular":
        print("⏭️ Market closed — skipping")
        return

    try:
        gold_oz, silver_oz = await get_metals_prices()
        usd_iqd, rate_source = await get_usd_iqd_rate()
        last = load_last_prices()

        # Check price alerts
        if last["gold"] > 0:
            if abs(gold_oz - last["gold"]) >= GOLD_RAPID_ALERT_USD:
                alert = build_alert_message("gold", last["gold"], gold_oz)
                await bot.send_message(chat_id=CHANNEL_ID, text=alert)
            if abs(silver_oz - last["silver"]) >= SILVER_RAPID_ALERT_USD:
                alert = build_alert_message("silver", last["silver"], silver_oz)
                await bot.send_message(chat_id=CHANNEL_ID, text=alert)

        gold_iqd = calculate_gold(gold_oz, usd_iqd)
        message = build_regular_message(gold_oz, silver_oz, usd_iqd, gold_iqd,
                                         last["gold"], last["silver"], rate_source, message_type)

        # Add news on market open
        if message_type == "open":
            news = await get_economic_news_kurdish()
            if news:
                message += f"\n━━━━━━━━━━━━━━━━━━━━━\n📰 هەواڵی ئابووری ئەمڕۆ\n{news}"

        await bot.send_message(chat_id=CHANNEL_ID, text=message)

        update_week_data(gold_oz, silver_oz, gold_iqd)
        save_last_prices(gold_oz, silver_oz)
        print(f"✅ [{message_type}] Sent at {datetime.now(LONDON_TZ).strftime('%H:%M')}")

    except Exception as e:
        print(f"❌ Error: {e}")

async def send_weekend_close(bot):
    try:
        await bot.send_message(chat_id=CHANNEL_ID, text=build_weekend_message())
        print("✅ Weekend close message sent")
    except Exception as e:
        print(f"❌ Error: {e}")

async def send_weekly_summary(bot):
    try:
        gold_oz, silver_oz = await get_metals_prices()
        week_data = load_week_data()
        msg = build_weekly_summary(week_data, gold_oz, silver_oz)
        await bot.send_message(chat_id=CHANNEL_ID, text=msg)
        print("✅ Weekly summary sent")
    except Exception as e:
        print(f"❌ Error: {e}")

async def send_holiday_notice(bot):
    dt = datetime.now(LONDON_TZ)
    if is_market_holiday(dt):
        reason = await get_holiday_reason_kurdish(dt)
        msg = (
            f"🔴 بازاڕ ئەمڕۆ داخراوە\n"
            f"Market Holiday — {dt.strftime('%d/%m/%Y')}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"{reason}"
        )
        await bot.send_message(chat_id=CHANNEL_ID, text=msg)

async def send_market_open_reminder(bot, minutes_left):
    try:
        await bot.send_message(chat_id=CHANNEL_ID, text=build_market_open_reminder(minutes_left))
        print(f"✅ Market open reminder sent: {minutes_left} min")
    except Exception as e:
        print(f"❌ Error: {e}")

async def scheduled_check(bot):
    """Runs every minute for special market notices.
    All weekday/hour checks are against UTC — the real market schedule —
    not London local time, which drifts 1h during British Summer Time."""
    now = datetime.now(UTC_TZ)
    minute = now.minute
    hour = now.hour
    weekday = now.weekday()

    # 1hr before market opens = weekly summary
    if weekday == 6 and hour == (MARKET_OPEN_HOUR_UTC - 1) % 24 and minute == 0:
        await send_weekly_summary(bot)
        return

    # Reminders before market open
    if weekday == 6 and hour == (MARKET_OPEN_HOUR_UTC - 1) % 24 and minute == 30:
        await send_market_open_reminder(bot, 30)
        return
    if weekday == 6 and hour == (MARKET_OPEN_HOUR_UTC - 1) % 24 and minute == 55:
        await send_market_open_reminder(bot, 5)
        return
    if weekday == 6 and hour == (MARKET_OPEN_HOUR_UTC - 1) % 24 and minute == 59:
        await send_market_open_reminder(bot, 1)
        return

    # Sunday market open
    if weekday == 6 and hour == MARKET_OPEN_HOUR_UTC and minute == 0:
        await send_price_update(bot, "open")
        return

    # Friday market closes for weekend
    if weekday == 4 and hour == MARKET_CLOSE_HOUR_UTC and minute == 0:
        await send_weekend_close(bot)
        return

    # Holiday check at 9am UTC
    if hour == 9 and minute == 0 and is_market_holiday(now):
        await send_holiday_notice(bot)
        return

# ─── ADMIN CHECK ──────────────────────────────────────────

def is_admin(update):
    return not ADMIN_ID or str(update.effective_user.id) == ADMIN_ID

# ─── COMMANDS ─────────────────────────────────────────────

async def cmd_setrate(update, context):
    if not is_admin(update):
        await update.message.reply_text("❌ مۆڵەتت نییە.")
        return
    if not context.args:
        await update.message.reply_text("⚠️ نمونە: /setrate 1552.50")
        return
    try:
        rate = float(context.args[0].replace(",", ""))
        if rate > 10_000:
            rate = rate / 100
        save_rate(rate)
        save_rate_meta("نرخی بازاڕی دەستی")
        await update.message.reply_text(f"✅ نرخی دۆلار نوێ کرا!\n💵 100$ = {rate*100:,.0f} دینار")
    except Exception:
        await update.message.reply_text("❌ ژمارەی دروست بنووسە.")

def build_price_trigger(minutes):
    """Clock-aligned trigger: fires at :00, :minutes, :2*minutes... every hour."""
    return CronTrigger(minute=f"*/{minutes}", timezone=UTC_TZ)

async def cmd_setinterval(update, context, scheduler, bot):
    if not is_admin(update):
        await update.message.reply_text("❌ مۆڵەتت نییە.")
        return
    if not context.args:
        options = ", ".join(str(x) for x in ALLOWED_INTERVALS)
        await update.message.reply_text(
            f"⚠️ نمونە: /setinterval 30\nبژاردەکان: {options}\nئێستا: {load_interval()} خولەک"
        )
        return
    try:
        minutes = int(context.args[0])
        if minutes not in ALLOWED_INTERVALS:
            options = ", ".join(str(x) for x in ALLOWED_INTERVALS)
            await update.message.reply_text(
                f"❌ تەنها ئەم ژمارانە دەتوانرێت (بۆ ئەوەی هەمیشە لەگەڵ کاتژمێری ڕاست بێت): {options}"
            )
            return
        save_interval(minutes)
        scheduler.reschedule_job(REGULAR_PRICE_JOB_ID, trigger=build_price_trigger(minutes))
        await update.message.reply_text(
            f"✅ کاتی ناردنی نرخ گۆڕا.\n⏱ هەر {minutes} خولەک لەسەر کاتژمێر (بۆ نموونە :00, :{minutes:02d}) نرخ دەنێردرێت."
        )
    except Exception as e:
        print(f"❌ setinterval error: {e}")
        await update.message.reply_text("❌ نمونەی دروست: /setinterval 30")

async def cmd_rate(update, context):
    rate, source = await get_usd_iqd_rate(force_refresh=True)
    await update.message.reply_text(
        f"💵 نرخی دۆلار — بازاڕی کوردستان\n"
        f"1$ = {rate:,.2f} دینار\n"
        f"100$ = {rate*100:,.0f} دینار\n"
        f"🔄 سەرچاوە: {source}\n"
        f"✍️ بۆ گۆڕین: /setrate {rate:.2f}"
    )

async def cmd_price(update, context):
    if not is_admin(update):
        await update.message.reply_text("❌ مۆڵەتت نییە.")
        return
    await update.message.reply_text("⏳ چاوەڕێ بکە...")
    await send_price_update(context.bot, "regular")

async def cmd_news(update, context):
    if not is_admin(update):
        await update.message.reply_text("❌ مۆڵەتت نییە.")
        return
    await update.message.reply_text("⏳ هەواڵ دەگرێت...")
    news = await get_economic_news_kurdish()
    if news:
        await context.bot.send_message(chat_id=CHANNEL_ID, text=f"📰 هەواڵی ئابووری:\n\n{news}")
        await update.message.reply_text("✅ نێردرا بۆ کەناڵ.")
    else:
        await update.message.reply_text("❌ هەواڵ نەگرا.")

async def cmd_summary(update, context):
    if not is_admin(update):
        await update.message.reply_text("❌ مۆڵەتت نییە.")
        return
    await send_weekly_summary(context.bot)

async def cmd_status(update, context):
    if not is_admin(update):
        await update.message.reply_text("❌ مۆڵەتت نییە.")
        return
    rate = load_rate()
    rate_meta = load_rate_meta()
    last = load_last_prices()
    now = datetime.now(LONDON_TZ)
    msg = (
        f"📌 دۆخی بۆت\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"بازاڕ: {market_status_text()}\n"
        f"کات: {now.strftime('%d/%m/%Y %H:%M')} London\n"
        f"دۆلار: {rate*100:,.0f} دینار بۆ 100$\n"
        f"سەرچاوەی دۆلار: {rate_meta.get('source', 'manual/default')}\n"
        f"دوایین زێڕ: ${last.get('gold', 0):,.2f}\n"
        f"دوایین زیو: ${last.get('silver', 0):,.2f}\n"
        f"ناردنی نرخ: هەر {load_interval()} خولەک جارێک\n"
        f"AI News: {'✅' if ANTHROPIC_API_KEY else '❌'}"
    )
    await update.message.reply_text(msg)

async def cmd_help(update, context):
    msg = (
        "🤖 فەرمانەکانی بۆت:\n\n"
        "/start — دەستپێکردن\n"
        "/status — دۆخی بۆت و بازاڕ\n"
        "/rate — نرخی دۆلار لە بازاڕی کوردستان\n"
        "/price — ئێستا نرخەکان بنێرە\n"
        "/news — هەواڵی ئابووری ئەمڕۆ\n"
        "/summary — پوختەی هەفتە\n"
        "/setrate 1552.50 — نرخی دۆلار بەدەستی بگۆڕە\n"
        "/setinterval 60 — کاتی ناردنی نرخ بگۆڕە\n"
        "/help — ئەم لیستە"
    )
    await update.message.reply_text(msg)

async def cmd_start(update, context):
    await cmd_help(update, context)

async def setup_bot_commands(app):
    commands = [
        BotCommand("start", "دەستپێکردن"),
        BotCommand("status", "دۆخی بۆت و بازاڕ"),
        BotCommand("rate", "نرخی دۆلار لە بازاڕی کوردستان"),
        BotCommand("price", "نرخی زێڕ و زیو بنێرە"),
        BotCommand("news", "هەواڵی ئابووری ئەمڕۆ"),
        BotCommand("summary", "پوختەی هەفتە"),
        BotCommand("setrate", "نرخی دۆلار بەدەستی بگۆڕە"),
        BotCommand("setinterval", "کاتی ناردنی نرخ بگۆڕە"),
        BotCommand("help", "فەرمانەکان"),
    ]
    await app.bot.set_my_commands(commands)

def validate_config():
    missing = []
    if not TELEGRAM_TOKEN:
        missing.append("TELEGRAM_TOKEN")
    if not CHANNEL_ID:
        missing.append("CHANNEL_ID")
    if missing:
        raise RuntimeError("Missing environment variables: " + ", ".join(missing))
    if not ADMIN_ID:
        print("⚠️ ADMIN_ID is not set. Admin commands are open to everyone.")
    if not ANTHROPIC_API_KEY:
        print("⚠️ ANTHROPIC_API_KEY is not set. /news will be disabled.")

# ─── MAIN ─────────────────────────────────────────────────

async def main():
    print("🚀 Gold & Silver Bot started!")

    # Bind the health-check port FIRST, before anything else — so Render's
    # port scanner sees it immediately regardless of what happens later
    # (config validation, Telegram API calls, etc.).
    port = os.environ.get("PORT") or "10000"
    try:
        from aiohttp import web as _web
        async def _health(request):
            return _web.Response(text="ok")

        async def _speak(request):
            """Plain-text sentence for iOS Shortcuts: fetch this URL, feed
            the raw response straight into a 'Speak Text' action — no JSON
            parsing needed. (Shortcuts uses iOS's own TTS engine directly,
            so this text-only version is fine and doesn't need the mp3 fix.)"""
            return _web.Response(text=await build_speak_sentence_en())

        async def _speak_fa(request):
            return _web.Response(text=await build_speak_sentence_fa(), charset="utf-8")

        async def _speak_en_mp3(request):
            """Real generated speech audio for the /radio page — works the
            same on every device, unlike relying on the browser's own
            (often missing) voices."""
            text = await build_speak_sentence_en()
            try:
                mp3 = await fetch_tts_mp3(text, "en")
                return _web.Response(body=mp3, content_type="audio/mpeg")
            except Exception as e:
                print(f"⚠️ TTS (en) failed: {e}")
                return _web.Response(status=502, text="TTS failed")

        async def _speak_fa_mp3(request):
            text = await build_speak_sentence_fa()
            try:
                mp3 = await fetch_tts_mp3(text, "fa")
                return _web.Response(body=mp3, content_type="audio/mpeg")
            except Exception as e:
                print(f"⚠️ TTS (fa) failed: {e}")
                return _web.Response(status=502, text="TTS failed")

        _music_cache = {"bytes": None}

        async def _background_music(request):
            """Fetches the music file you uploaded to GitHub and serves it
            ourselves (cached after the first request) with a proper
            audio/mpeg content-type — GitHub's raw file server doesn't
            reliably send that header, which is likely why direct redirect
            playback was failing, especially on iPhone Safari."""
            if _music_cache["bytes"] is None:
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            BACKGROUND_MUSIC_URL,
                            headers={"User-Agent": "Mozilla/5.0 (compatible; GoldBot/1.0)"},
                            timeout=aiohttp.ClientTimeout(total=20),
                        ) as resp:
                            resp.raise_for_status()
                            _music_cache["bytes"] = await resp.read()
                            print(f"🎵 Background music cached: {len(_music_cache['bytes'])} bytes")
                except Exception as e:
                    print(f"⚠️ Could not fetch background music: {e}")
                    return _web.Response(status=502, text=f"Could not fetch music: {e}")
            return _web.Response(body=_music_cache["bytes"], content_type="audio/mpeg")

        async def _radio_page(request):
            return _web.Response(text=RADIO_HTML, content_type="text/html")

        async def _price_json(request):
            """Full price breakdown for the /dashboard page (and anyone
            else who wants raw numbers)."""
            last = load_last_prices()
            rate = load_rate()
            gold = last.get("gold", 0)
            silver = last.get("silver", 0)
            gold_iqd = calculate_gold(gold, rate) if gold > 0 else {24: 0, 22: 0, 21: 0, 18: 0}
            meta = load_rate_meta()
            data = {
                "gold_usd_oz": gold,
                "silver_usd_oz": silver,
                "silver_usd_kg": calculate_silver_usd(silver) if silver > 0 else 0,
                "usd_iqd": rate,
                "usd_iqd_per_100": rate * 100,
                "gold_iqd": {str(k): v for k, v in gold_iqd.items()},
                "updated_at": meta.get("updated_at", ""),
                "market_open": is_market_open(),
            }
            resp = _web.json_response(data)
            resp.headers["Access-Control-Allow-Origin"] = "*"
            return resp

        async def _dashboard_page(request):
            return _web.Response(text=DASHBOARD_HTML, content_type="text/html")

        _health_app = _web.Application()
        _health_app.router.add_get("/", _health)
        _health_app.router.add_get("/speak", _speak)
        _health_app.router.add_get("/speak-fa", _speak_fa)
        _health_app.router.add_get("/speak-en.mp3", _speak_en_mp3)
        _health_app.router.add_get("/speak-fa.mp3", _speak_fa_mp3)
        _health_app.router.add_get("/background-music.mp3", _background_music)
        _health_app.router.add_get("/radio", _radio_page)
        _health_app.router.add_get("/price", _price_json)
        _health_app.router.add_get("/dashboard", _dashboard_page)
        _runner = _web.AppRunner(_health_app)
        await _runner.setup()
        await _web.TCPSite(_runner, "0.0.0.0", int(port)).start()
        print(f"🌐 Health-check server listening on :{port} (PORT env was {'set' if os.environ.get('PORT') else 'NOT set — used default'})")
    except Exception as e:
        print(f"⚠️ Health-check server failed to start: {e}")

    validate_config()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    scheduler = AsyncIOScheduler(timezone=LONDON_TZ)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("setrate", cmd_setrate))
    app.add_handler(CommandHandler("rate", cmd_rate))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("setinterval",
        lambda u, c: cmd_setinterval(u, c, scheduler, app.bot)))

    # Special market notices are checked every minute. Price updates use the admin interval.
    scheduler.add_job(scheduled_check, CronTrigger(minute="*"), args=[app.bot])
    scheduler.add_job(check_economic_event_alerts, CronTrigger(minute="*"), args=[app.bot])
    saved_interval = load_interval()
    if saved_interval not in ALLOWED_INTERVALS:
        saved_interval = DEFAULT_INTERVAL
        save_interval(saved_interval)
    scheduler.add_job(
        send_price_update,
        build_price_trigger(saved_interval),
        args=[app.bot, "regular"],
        id=REGULAR_PRICE_JOB_ID,
        replace_existing=True,
    )
    scheduler.start()

    await app.initialize()
    await setup_bot_commands(app)
    await app.start()
    await app.updater.start_polling()

    print(f"🤖 Bot running! Price interval: {load_interval()} min")
    while True:
        await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())

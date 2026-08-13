import os
import asyncio
import aiohttp
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
import pytz
from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# ─── CONFIG ─────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
ADMIN_ID = os.environ.get("ADMIN_ID")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
KURDISTAN_RATE_URL = os.environ.get("KURDISTAN_RATE_URL")

LONDON_TZ = pytz.timezone("Europe/London")  # used only for display in messages
KURDISTAN_TZ = pytz.timezone("Asia/Baghdad")  # Erbil/Sulaymaniyah share this zone, no DST
UTC_TZ = pytz.utc  # the real global gold/forex market runs on a fixed UTC schedule
MARKET_OPEN_HOUR_UTC = 22   # Sunday 22:00 UTC
MARKET_CLOSE_HOUR_UTC = 22  # Friday 22:00 UTC

# ─── FILE STORAGE ─────────────────────────────────────────────
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

# ─── MARKET STATUS ────────────────────────────────────────────

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

# ─── PRICE FETCHING ──────────────────────────────────────────

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

# ─── FOREXFACTORY ECONOMIC CALENDAR ──────────────────────────
# Free public feed used widely by trading bots — no key, no login.
# Rate-limited by the host to ~2 requests/5min, so don't poll this often.
FF_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

async def fetch_forexfactory_events():
    async with aiohttp.ClientSession() as session:
        async with session.get(
            FF_CALENDAR_URL,
            timeout=aiohttp.ClientTimeout(total=15),
            headers={"User-Agent": "Mozilla/5.0 (compatible; GoldBot/1.0)"},
        ) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

def filter_relevant_ff_events(events, hours_ahead=24):
    """USD-only, Medium/High impact events (the ones that actually move
    gold/silver/dollar) landing between 2h ago and `hours_ahead` from now."""
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
            if now - timedelta(hours=2) <= event_dt <= cutoff:
                result.append({**e, "_dt": event_dt})
        except Exception:
            continue
    result.sort(key=lambda x: x["_dt"])
    return result

def build_forexfactory_message(events):
    if not events:
        return None
    impact_emoji = {"High": "🔴", "Medium": "🟠"}
    lines = ["📅 هەواڵی ئابووری — ForexFactory (٢٤ سەعاتی داهاتوو)", "━━━━━━━━━━━━━━━━━━━━━"]
    for e in events[:8]:
        t = e["_dt"].astimezone(KURDISTAN_TZ).strftime("%H:%M")
        emoji = impact_emoji.get(e.get("impact", ""), "⚪")
        title = e.get("title", "")
        forecast = e.get("forecast") or "-"
        previous = e.get("previous") or "-"
        actual = e.get("actual") or ""
        line = f"{emoji} {t} — {title}"
        if actual:
            line += f"\n   ئێستا: {actual} | ڕاچاوکراو: {forecast} | پێشوو: {previous}"
        else:
            line += f"\n   ڕاچاوکراو: {forecast} | پێشوو: {previous}"
        lines.append(line)
    lines.append("━━━━━━━━━━━━━━━━━━━━━\nکات بە کاتی کوردستان")
    return "\n".join(lines)


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
    headlines only if the ForexFactory feed is unreachable or empty."""
    try:
        events = await fetch_forexfactory_events()
        relevant = filter_relevant_ff_events(events)
        msg = build_forexfactory_message(relevant)
        if msg:
            return msg
    except Exception as e:
        print(f"⚠️ ForexFactory fetch failed: {e}")

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

# ─── CALCULATIONS ──────────────────────────────────────────

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

# ─── MESSAGE BUILDERS ───────────────────────────────────────

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

# ─── WEEK DATA TRACKING ─────────────────────────────────────

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

# ─── MAIN SEND FUNCTIONS ────────────────────────────────────

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

# ─── ADMIN CHECK ───────────────────────────────────────────

def is_admin(update):
    return not ADMIN_ID or str(update.effective_user.id) == ADMIN_ID

# ─── COMMANDS ────────────────────────────────────────────

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
        await update.message.reply_text(f"📰 هەواڵی ئابووری:\n\n{news}")
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
        f"━━━━━━━━━━━━━━━━━━━━━\n"
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

# ─── MAIN ─────────────────────────────────────────────────────────

async def main():
    print("🚀 Gold & Silver Bot started!")
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

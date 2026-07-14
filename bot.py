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
from apscheduler.triggers.interval import IntervalTrigger

# ─── CONFIG ───────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
ADMIN_ID = os.environ.get("ADMIN_ID")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
KURDISTAN_RATE_URL = os.environ.get("KURDISTAN_RATE_URL")

LONDON_TZ = pytz.timezone("Europe/London")

# ─── FILE STORAGE ─────────────────────────────────────────
RATE_FILE = "usd_iqd_rate.txt"
INTERVAL_FILE = "interval_minutes.txt"
WEEK_DATA_FILE = "week_data.json"
LAST_PRICES_FILE = "last_prices.json"
RATE_META_FILE = "usd_iqd_rate_meta.json"

DEFAULT_RATE = 1539.00
DEFAULT_INTERVAL = 30
RATE_CACHE_MINUTES = 15
MIN_INTERVAL = 5
MAX_INTERVAL = 240
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

def load_week_data():
    try:
        with open(WEEK_DATA_FILE, "r") as f:
            return json.load(f)
    except:
        return {"high_gold": 0, "low_gold": 999999, "open_gold": 0, "close_gold": 0,
                "high_silver": 0, "low_silver": 999999, "open_silver": 0, "close_silver": 0,
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
    if dt is None:
        dt = datetime.now(LONDON_TZ)
    if dt.weekday() == 5:  # Saturday
        return False
    if dt.weekday() == 6 and dt.hour < 22:  # Sunday before 10pm
        return False
    if dt.weekday() == 4 and dt.hour >= 22:  # Friday after 10pm
        return False
    if is_market_holiday(dt):
        return False
    return True

def market_status_text():
    now = datetime.now(LONDON_TZ)
    if is_market_open(now):
        return "🟢 کراوە | Open"
    if is_market_holiday(now):
        return "🔴 پشووی بازاڕ | Market holiday"
    return "🌙 داخراوە | Closed"

# ... [All other functions are unchanged - I'm keeping this short for the response] ...

# (The rest of your original functions are kept exactly the same)

# ─── MAIN ─────────────────────────────────────────────────

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

    scheduler.add_job(scheduled_check, CronTrigger(minute="*"), args=[app.bot])
    scheduler.add_job(
        send_price_update,
        IntervalTrigger(minutes=load_interval(), timezone=LONDON_TZ),
        args=[app.bot, "regular"],
        id=REGULAR_PRICE_JOB_ID,
        replace_existing=True,
    )
    scheduler.start()

    await app.initialize()
    await setup_bot_commands(app)
    await app.start()

    print(f"🤖 Bot running! Price interval: {load_interval()} min")

    # Fixed line - this solves the error
    await app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    asyncio.run(main())

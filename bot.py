import os
import asyncio
import aiohttp
import json
from datetime import datetime, timedelta
import pytz
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# ─── CONFIG ───────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
ADMIN_ID = os.environ.get("ADMIN_ID")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

LONDON_TZ = pytz.timezone("Europe/London")

# ─── FILE STORAGE ─────────────────────────────────────────
RATE_FILE = "usd_iqd_rate.txt"
INTERVAL_FILE = "interval_minutes.txt"
WEEK_DATA_FILE = "week_data.json"
LAST_PRICES_FILE = "last_prices.json"

DEFAULT_RATE = 1552.50
DEFAULT_INTERVAL = 60

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

def load_rate():
    try:
        with open(RATE_FILE, "r") as f:
            return float(f.read().strip())
    except:
        return DEFAULT_RATE

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
    # Weekend: Saturday all day, Sunday before 10pm London
    if dt.weekday() == 5:  # Saturday
        return False
    if dt.weekday() == 6 and dt.hour < 22:  # Sunday before 10pm
        return False
    if dt.weekday() == 4 and dt.hour >= 22:  # Friday after 10pm
        return False
    if is_market_holiday(dt):
        return False
    return True

def is_weekend_now():
    dt = datetime.now(LONDON_TZ)
    if dt.weekday() == 5:
        return True
    if dt.weekday() == 6 and dt.hour < 22:
        return True
    return False

# ─── PRICE FETCHING ───────────────────────────────────────

async def get_metals_prices():
    url_gold = "https://api.gold-api.com/price/XAU"
    url_silver = "https://api.gold-api.com/price/XAG"
    async with aiohttp.ClientSession() as session:
        async with session.get(url_gold, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            gold = float(data["price"])
        async with session.get(url_silver, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            silver = float(data["price"])
    return gold, silver

# ─── AI NEWS FETCH ────────────────────────────────────────

async def get_economic_news_kurdish():
    """Use Claude AI to get latest economic news affecting gold/silver in Kurdish Sorani."""
    if not ANTHROPIC_API_KEY:
        return None
    try:
        payload = {
            "model": "claude-sonnet-4-6",
            "max_tokens": 1000,
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
            "messages": [{
                "role": "user",
                "content": (
                    "Search for the latest economic news from today that affects gold and silver prices. "
                    "Include: CPI inflation data, Fed decisions, geopolitical events, market news. "
                    "Then write a SHORT summary in Kurdish Sorani (3-4 bullet points max) explaining: "
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
            "model": "claude-sonnet-4-6",
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
        return f"📉 ${diff:.2f}"
    return "➡️ بێ گۆڕان"

# ─── MESSAGE BUILDERS ─────────────────────────────────────

def build_regular_message(gold_oz, silver_oz, usd_iqd, gold_iqd, last_gold, last_silver, message_type="regular"):
    now = datetime.now(LONDON_TZ)
    date_str = now.strftime("%d / %m / %Y")
    time_str = now.strftime("%H:%M")
    silver_kg_usd = calculate_silver_usd(silver_oz)

    gold_trend = trend_emoji(gold_oz, last_gold)
    silver_trend = trend_emoji(silver_oz, last_silver)

    if message_type == "open":
        header = f"🔔 بازاڕ کرایەوە — Market Open!\n"
    elif message_type == "close":
        header = f"🌙 بازاڕ داخرا — Market Closing Soon\n"
    else:
        header = ""

    msg = (
        f"{header}"
        f"🗓 {date_str}   🕐 {time_str} (London)\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏅 نرخی زێڕ\n"
        f"   ئۆنسێک ➜ ${gold_oz:,.2f}  {gold_trend}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💛 عەیار ٢٤  ►  {format_iqd(gold_iqd[24])} دینار\n"
        f"🟡 عەیار ٢٢  ►  {format_iqd(gold_iqd[22])} دینار\n"
        f"🟠 عەیار ٢١  ►  {format_iqd(gold_iqd[21])} دینار\n"
        f"🔶 عەیار ١٨  ►  {format_iqd(gold_iqd[18])} دینار\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🥈 نرخی زیو\n"
        f"   ئۆنسێک ➜ ${silver_oz:,.2f}  {silver_trend}\n"
        f"   یەک کیلۆ ➜ ${silver_kg_usd:,.2f}\n"
        f"⚠️ نرخی بازاڕی جیهانییە، نەک نرخی دوکانەکانی کوردستان\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 نرخی دۆلار  ►  {usd_iqd*100:,.0f} دینار (بۆ 100$)"
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
        f"🏅 زێڕ — Gold\n"
        f"   کردنەوە ➜ ${week_data['open_gold']:,.2f}\n"
        f"   داخستن ➜ ${week_data['close_gold']:,.2f}\n"
        f"   بەرزترین ➜ ${week_data['high_gold']:,.2f}\n"
        f"   نزمترین ➜ ${week_data['low_gold']:,.2f}\n"
        f"   گۆڕان ➜ {gold_emoji} ${gold_change:+.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🥈 زیو — Silver\n"
        f"   کردنەوە ➜ ${week_data['open_silver']:,.2f}\n"
        f"   داخستن ➜ ${week_data['close_silver']:,.2f}\n"
        f"   بەرزترین ➜ ${week_data['high_silver']:,.2f}\n"
        f"   نزمترین ➜ ${week_data['low_silver']:,.2f}\n"
        f"   گۆڕان ➜ {silver_emoji} ${silver_change:+.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔔 بازاڕ دەکرێتەوە دوێنێ شەو — Market reopens tonight"
    )
    return msg

def build_weekend_message():
    now = datetime.now(LONDON_TZ)
    return (
        f"🌙 بازاڕ داخراوە\n"
        f"Market Closed — Weekend\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 {now.strftime('%d/%m/%Y')}\n"
        f"بازاڕی زێڕ و زیو بۆ کۆتایی هەفتە داخراوە.\n"
        f"دووشەممە دەکرێتەوە. ✅\n"
        f"Gold & Silver market is closed for the weekend.\n"
        f"Reopens Sunday 10:00 PM London time."
    )

def build_alert_message(metal, old_price, new_price):
    change = new_price - old_price
    direction = "سەرەوە🚀" if change > 0 else "خوارەوە⚠️"
    metal_ku = "زێڕ 🏅" if metal == "gold" else "زیو 🥈"
    return (
        f"🚨 ئاگادارکردنەوەی نرخ — Price Alert!\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{metal_ku} چوو {direction}\n"
        f"   پێشتر ➜ ${old_price:,.2f}\n"
        f"   ئێستا ➜ ${new_price:,.2f}\n"
        f"   گۆڕان ➜ ${change:+.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ {datetime.now(LONDON_TZ).strftime('%H:%M')} London"
    )

# ─── WEEK DATA TRACKING ───────────────────────────────────

def update_week_data(gold, silver):
    data = load_week_data()
    now = datetime.now(LONDON_TZ)
    week_str = now.strftime("%Y-W%W")

    if data["week_start"] != week_str:
        # New week — reset
        data = {
            "high_gold": gold, "low_gold": gold,
            "open_gold": gold, "close_gold": gold,
            "high_silver": silver, "low_silver": silver,
            "open_silver": silver, "close_silver": silver,
            "week_start": week_str
        }
    else:
        data["high_gold"] = max(data["high_gold"], gold)
        data["low_gold"] = min(data["low_gold"], gold)
        data["close_gold"] = gold
        data["high_silver"] = max(data["high_silver"], silver)
        data["low_silver"] = min(data["low_silver"], silver)
        data["close_silver"] = silver

    save_week_data(data)
    return data

# ─── MAIN SEND FUNCTIONS ──────────────────────────────────

async def send_price_update(bot, message_type="regular"):
    if not is_market_open() and message_type == "regular":
        print("⏭️ Market closed — skipping")
        return

    try:
        gold_oz, silver_oz = await get_metals_prices()
        usd_iqd = load_rate()
        last = load_last_prices()

        # Check price alerts
        if last["gold"] > 0:
            if abs(gold_oz - last["gold"]) >= 50:
                alert = build_alert_message("gold", last["gold"], gold_oz)
                await bot.send_message(chat_id=CHANNEL_ID, text=alert)
            if abs(silver_oz - last["silver"]) >= 2:
                alert = build_alert_message("silver", last["silver"], silver_oz)
                await bot.send_message(chat_id=CHANNEL_ID, text=alert)

        gold_iqd = calculate_gold(gold_oz, usd_iqd)
        message = build_regular_message(gold_oz, silver_oz, usd_iqd, gold_iqd,
                                         last["gold"], last["silver"], message_type)

        # Add news on market open
        if message_type == "open":
            news = await get_economic_news_kurdish()
            if news:
                message += f"\n━━━━━━━━━━━━━━━━━━━━━\n📰 هەواڵی ئابووری ئەمڕۆ\n{news}"

        await bot.send_message(chat_id=CHANNEL_ID, text=message)

        update_week_data(gold_oz, silver_oz)
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

async def scheduled_check(bot):
    """Runs every 30 min — sends at exact :00 and :30 only if market open."""
    now = datetime.now(LONDON_TZ)
    minute = now.minute
    hour = now.hour
    weekday = now.weekday()

    # Sunday 9pm = weekly summary (1hr before market opens at 10pm)
    if weekday == 6 and hour == 21 and minute == 0:
        await send_weekly_summary(bot)
        return

    # Sunday 10pm = market opens
    if weekday == 6 and hour == 22 and minute == 0:
        await send_price_update(bot, "open")
        return

    # Friday 10pm = market closes for weekend
    if weekday == 4 and hour == 22 and minute == 0:
        await send_weekend_close(bot)
        return

    # Holiday check at 9am
    if hour == 9 and minute == 0 and is_market_holiday(datetime.now(LONDON_TZ)):
        await send_holiday_notice(bot)
        return

    # Regular update — only if market is open and minute is 0 or 30
    if minute in [0, 30] and is_market_open():
        # Monday 10pm open (backup)
        if weekday == 0 and hour == 22 and minute == 0:
            await send_price_update(bot, "open")
        else:
            await send_price_update(bot, "regular")

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
        save_rate(rate)
        await update.message.reply_text(f"✅ نرخی دۆلار نوێ کرا!\n💵 100$ = {rate*100:,.0f} دینار")
    except:
        await update.message.reply_text("❌ ژمارەی دروست بنووسە.")

async def cmd_setinterval(update, context, scheduler, bot):
    if not is_admin(update):
        await update.message.reply_text("❌ مۆڵەتت نییە.")
        return
    await update.message.reply_text("ℹ️ بۆتەکە ئێستا هەر 30 خولەک چاودێری دەکات و لە :00 و :30 دەنێردرێت.")

async def cmd_rate(update, context):
    rate = load_rate()
    await update.message.reply_text(f"💵 نرخی دۆلار: {rate*100:,.0f} دینار (بۆ 100$)")

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

async def cmd_help(update, context):
    msg = (
        "🤖 فەرمانەکانی بۆت:\n\n"
        "/setrate 1552.50 — نرخی دۆلار بگۆڕە\n"
        "/rate — نرخی ئێستای دۆلار\n"
        "/price — ئێستا نرخەکان بنێرە\n"
        "/news — هەواڵی ئابووری ئەمڕۆ\n"
        "/summary — پوختەی هەفتە\n"
        "/help — ئەم لیستە"
    )
    await update.message.reply_text(msg)

# ─── MAIN ─────────────────────────────────────────────────

async def main():
    print("🚀 Gold & Silver Bot started!")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    scheduler = AsyncIOScheduler(timezone=LONDON_TZ)

    app.add_handler(CommandHandler("setrate", cmd_setrate))
    app.add_handler(CommandHandler("rate", cmd_rate))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("setinterval",
        lambda u, c: cmd_setinterval(u, c, scheduler, app.bot)))

    # Run check every 30 minutes at :00 and :30
    scheduler.add_job(scheduled_check, CronTrigger(minute="0,30"), args=[app.bot])
    scheduler.start()

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    print("🤖 Bot running! Checks every 30 min at :00 and :30")
    while True:
        await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())

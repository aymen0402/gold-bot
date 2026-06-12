import os
import asyncio
import aiohttp
from datetime import datetime
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ─── CONFIG ───────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
ADMIN_ID = os.environ.get("ADMIN_ID")

# ─── FILE STORAGE ─────────────────────────────────────────
RATE_FILE = "usd_iqd_rate.txt"
INTERVAL_FILE = "interval_minutes.txt"
DEFAULT_RATE = 1552.50
DEFAULT_INTERVAL = 60  # minutes

def save_rate(rate):
    with open(RATE_FILE, "w") as f:
        f.write(str(rate))

def load_rate():
    try:
        with open(RATE_FILE, "r") as f:
            return float(f.read().strip())
    except Exception:
        return float(DEFAULT_RATE)

def save_interval(minutes):
    with open(INTERVAL_FILE, "w") as f:
        f.write(str(minutes))

def load_interval():
    try:
        with open(INTERVAL_FILE, "r") as f:
            return int(f.read().strip())
    except Exception:
        return DEFAULT_INTERVAL

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
    ayar_iqd = {}
    for k, v in ayar.items():
        iqd = v * usd_iqd
        ayar_iqd[k] = round(iqd / 1000) * 1000
    return ayar_iqd

def calculate_silver_usd(silver_oz_usd):
    per_gram = silver_oz_usd / 31.1
    per_kg = per_gram * 1000
    return round(per_kg, 2)

def format_iqd(n):
    if n >= 1_000_000:
        val = n / 1_000_000
        return f"{int(val)} ملیۆن" if val == int(val) else f"{val:.3f} ملیۆن"
    elif n >= 1_000:
        val = n / 1_000
        return f"{int(val)} هەزار" if val == int(val) else f"{val:.0f} هەزار"
    return f"{n:,.0f}"

# ─── MESSAGE BUILDER ──────────────────────────────────────

def build_message(gold_oz, silver_oz, usd_iqd, gold_iqd):
    now = datetime.now()
    date_str = now.strftime("%d / %m / %Y")
    time_str = now.strftime("%H:%M")
    silver_kg_usd = calculate_silver_usd(silver_oz)

    msg = (
        f"🗓 {date_str}   🕐 {time_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏅 نرخی زێڕ\n"
        f"   ئۆنسێک ➜ ${gold_oz:,.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💛 عەیار ٢٤  ►  {format_iqd(gold_iqd[24])} دینار\n"
        f"🟡 عەیار ٢٢  ►  {format_iqd(gold_iqd[22])} دینار\n"
        f"🟠 عەیار ٢١  ►  {format_iqd(gold_iqd[21])} دینار\n"
        f"🔶 عەیار ١٨  ►  {format_iqd(gold_iqd[18])} دینار\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🥈 نرخی زیو\n"
        f"   ئۆنسێک ➜ ${silver_oz:,.2f}\n"
        f"   یەک کیلۆ ➜ ${silver_kg_usd:,.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 نرخی دۆلار  ►  {usd_iqd*100:,.0f} دینار (بۆ 100$)"
    )
    return msg

# ─── SEND UPDATE ──────────────────────────────────────────

async def send_price_update(bot):
    try:
        gold_oz, silver_oz = await get_metals_prices()
        usd_iqd = load_rate()
        gold_iqd = calculate_gold(gold_oz, usd_iqd)
        message = build_message(gold_oz, silver_oz, usd_iqd, gold_iqd)
        await bot.send_message(chat_id=CHANNEL_ID, text=message)
        print(f"✅ Message sent at {datetime.now().strftime('%H:%M:%S')}")
    except Exception as e:
        print(f"❌ Error: {e}")

# ─── ADMIN CHECK ──────────────────────────────────────────

def is_admin(update: Update):
    return not ADMIN_ID or str(update.effective_user.id) == ADMIN_ID

# ─── COMMANDS ─────────────────────────────────────────────

async def cmd_setrate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("❌ تۆ مۆڵەتت نییە ئەم فەرمانە بەکاربهێنیت.")
        return
    if not context.args or len(context.args) != 1:
        await update.message.reply_text("⚠️ نمونە: /setrate 1552.50")
        return
    try:
        new_rate = float(context.args[0].replace(",", ""))
        save_rate(new_rate)
        await update.message.reply_text(
            f"✅ نرخی دۆلار نوێ کرایەوە!\n"
            f"💵 100 دۆلار = {new_rate*100:,.0f} دینار"
        )
    except ValueError:
        await update.message.reply_text("❌ ژمارەیەکی دروست بنووسە. نمونە: /setrate 1552.50")

async def cmd_setinterval(update: Update, context: ContextTypes.DEFAULT_TYPE, scheduler: AsyncIOScheduler, bot):
    if not is_admin(update):
        await update.message.reply_text("❌ تۆ مۆڵەتت نییە ئەم فەرمانە بەکاربهێنیت.")
        return
    if not context.args or len(context.args) != 1:
        await update.message.reply_text("⚠️ نمونە: /setinterval 30  (ژمارە بە خولەک)")
        return
    try:
        minutes = int(context.args[0])
        if minutes < 5:
            await update.message.reply_text("❌ کەمترین ماوە 5 خولەکە.")
            return
        save_interval(minutes)

        # Reschedule the job
        scheduler.remove_all_jobs()
        scheduler.add_job(send_price_update, "interval", minutes=minutes, args=[bot])

        if minutes >= 60 and minutes % 60 == 0:
            display = f"{minutes // 60} کاتژمێر"
        else:
            display = f"{minutes} خولەک"

        await update.message.reply_text(
            f"✅ ماوەی نێردن نوێ کرایەوە!\n"
            f"🕐 ئێستا هەر {display} یەک نرخەکان دەنێردرێن"
        )
    except ValueError:
        await update.message.reply_text("❌ ژمارەیەکی دروست بنووسە. نمونە: /setinterval 30")

async def cmd_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rate = load_rate()
    await update.message.reply_text(f"💵 نرخی ئێستای دۆلار: {rate*100:,.0f} دینار (بۆ 100$)")

async def cmd_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    minutes = load_interval()
    if minutes >= 60 and minutes % 60 == 0:
        display = f"{minutes // 60} کاتژمێر"
    else:
        display = f"{minutes} خولەک"
    await update.message.reply_text(f"🕐 ماوەی ئێستا: هەر {display} یەک")

async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("❌ تۆ مۆڵەتت نییە ئەم فەرمانە بەکاربهێنیت.")
        return
    await update.message.reply_text("⏳ چاوەڕێ بکە...")
    await send_price_update(context.bot)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🤖 فەرمانەکانی بۆت:\n\n"
        "/setrate 1552.50 — نرخی دۆلار بگۆڕە\n"
        "/rate — نرخی ئێستای دۆلار ببینە\n"
        "/setinterval 30 — ماوەی نێردن بگۆڕە (بە خولەک)\n"
        "/interval — ماوەی ئێستا ببینە\n"
        "/price — ئێستا نرخەکان بنێرە\n"
        "/help — ئەم لیستە نیشان بدە"
    )
    await update.message.reply_text(msg)

# ─── MAIN ─────────────────────────────────────────────────

async def main():
    print("🚀 Gold & Silver Bot started!")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    scheduler = AsyncIOScheduler()

    # Register commands
    app.add_handler(CommandHandler("setrate", cmd_setrate))
    app.add_handler(CommandHandler("rate", cmd_rate))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("interval", cmd_interval))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("setinterval",
        lambda u, c: cmd_setinterval(u, c, scheduler, app.bot)))

    # Send first message on startup
    await send_price_update(app.bot)

    # Schedule updates
    interval = load_interval()
    scheduler.add_job(send_price_update, "interval", minutes=interval, args=[app.bot])
    scheduler.start()
    print(f"⏰ Scheduled every {interval} minutes")

    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    print("🤖 Bot is running!")

    while True:
        await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())

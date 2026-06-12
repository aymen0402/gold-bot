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
ADMIN_ID = os.environ.get("ADMIN_ID")  # your personal Telegram user ID

# ─── RATE STORAGE (in memory, survives restarts via file) ──
RATE_FILE = "usd_iqd_rate.txt"
DEFAULT_RATE = 155250

def save_rate(rate):
    with open(RATE_FILE, "w") as f:
        f.write(str(rate))

def load_rate():
    try:
        with open(RATE_FILE, "r") as f:
            return float(f.read().strip())
    except Exception:
        return float(DEFAULT_RATE)

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
        f"💵 نرخی دۆلار  ►  {usd_iqd:,.0f} دینار"
    )
    return msg

# ─── SEND HOURLY UPDATE ───────────────────────────────────

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

# ─── TELEGRAM COMMANDS ────────────────────────────────────

async def cmd_setrate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Only admin can set the rate. Usage: /setrate 155250"""
    user_id = str(update.effective_user.id)

    if ADMIN_ID and user_id != ADMIN_ID:
        await update.message.reply_text("❌ تۆ مۆڵەتت نییە ئەم فەرمانە بەکاربهێنیت.")
        return

    if not context.args or len(context.args) != 1:
        await update.message.reply_text("⚠️ نمونە: /setrate 155250")
        return

    try:
        new_rate = float(context.args[0].replace(",", ""))
        save_rate(new_rate)
        await update.message.reply_text(
            f"✅ نرخی دۆلار نوێ کرایەوە!\n"
            f"💵 1 دۆلار = {new_rate:,.0f} دینار"
        )
        print(f"✅ Rate updated to {new_rate}")
    except ValueError:
        await update.message.reply_text("❌ ژمارەیەکی دروست بنووسە. نمونە: /setrate 155250")

async def cmd_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current rate."""
    rate = load_rate()
    await update.message.reply_text(f"💵 نرخی ئێستای دۆلار: {rate:,.0f} دینار")

async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send price update immediately on demand."""
    user_id = str(update.effective_user.id)
    if ADMIN_ID and user_id != ADMIN_ID:
        await update.message.reply_text("❌ تۆ مۆڵەتت نییە ئەم فەرمانە بەکاربهێنیت.")
        return
    await update.message.reply_text("⏳ چاوەڕێ بکە...")
    await send_price_update(context.bot)

# ─── MAIN ─────────────────────────────────────────────────

async def main():
    print("🚀 Gold & Silver Bot started!")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("setrate", cmd_setrate))
    app.add_handler(CommandHandler("rate", cmd_rate))
    app.add_handler(CommandHandler("price", cmd_price))

    # Send first message on startup
    await send_price_update(app.bot)

    # Schedule hourly updates
    scheduler = AsyncIOScheduler()
    scheduler.add_job(send_price_update, "interval", hours=1, args=[app.bot])
    scheduler.start()

    # Start bot polling
    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    print("🤖 Bot is running and listening for commands...")

    while True:
        await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())

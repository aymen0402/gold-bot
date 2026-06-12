import os
import asyncio
import aiohttp
from datetime import datetime
from telegram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ─── CONFIG ───────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")  # e.g. @yourchannel or -100xxxxxxxxxx

# ─── PRICE FETCHING ───────────────────────────────────────

async def get_metals_prices():
    """Fetch live gold and silver prices in USD per oz from frankfurter/commodity API."""
    url = "https://api.gold-api.com/price/XAU"
    url_silver = "https://api.gold-api.com/price/XAG"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            gold = float(data["price"])
        async with session.get(url_silver, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            silver = float(data["price"])
    return gold, silver

async def get_usd_iqd_rate():
    """Fetch live USD to IQD exchange rate."""
    url = "https://api.exchangerate-api.com/v4/latest/USD"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            return float(data["rates"]["IQD"])

# ─── CALCULATIONS ─────────────────────────────────────────

def calculate_gold(gold_oz_usd, usd_iqd):
    """Calculate gold prices per mithqal for different ayar."""
    gram = gold_oz_usd / 31.1          # price per gram ayar 24
    mithqal_24 = gram * 5              # 1 mithqal = 5 grams

    ayar = {
        24: mithqal_24,
        22: mithqal_24 * 0.9167,
        21: mithqal_24 * 0.875,
        18: mithqal_24 * 0.750,
    }

    # Convert to IQD and round to nearest thousand
    ayar_iqd = {}
    for k, v in ayar.items():
        iqd = v * usd_iqd
        ayar_iqd[k] = round(iqd / 1000) * 1000  # round to nearest 1000

    return ayar_iqd

def calculate_silver(silver_oz_usd, usd_iqd):
    """Calculate silver price per kg in IQD."""
    per_gram = silver_oz_usd / 31.1
    per_kg_usd = per_gram * 1000
    per_kg_iqd = per_kg_usd * usd_iqd
    return round(per_kg_iqd / 1000) * 1000

def format_number(n):
    """Format number nicely: 1,250,000 → 1.250 ملیۆن or 892,000 → 892 هەزار"""
    if n >= 1_000_000:
        val = n / 1_000_000
        if val == int(val):
            return f"{int(val)} ملیۆن"
        else:
            return f"{val:.3f} ملیۆن"
    elif n >= 1_000:
        val = n / 1_000
        if val == int(val):
            return f"{int(val)} هەزار"
        else:
            return f"{val:.1f} هەزار"
    else:
        return f"{n:,.0f}"

# ─── MESSAGE BUILDER ──────────────────────────────────────

def build_message(gold_oz, silver_oz, usd_iqd, gold_iqd, silver_kg_iqd):
    now = datetime.now()
    date_str = now.strftime("%d - %m - %Y")

    usd_iqd_formatted = f"{usd_iqd:,.0f}"
    gold_oz_formatted = f"{gold_oz:,.2f}"
    silver_oz_formatted = f"{silver_oz:,.2f}"

    msg = (
        f"☀️ ئەمڕۆ {date_str}\n"
        f"▪️ ئۆنسەی زێر {gold_oz_formatted} دۆلار\n"
        f"─────────────────────────────\n"
        f"🔹 زێڕی عەیار 24 بە {format_number(gold_iqd[24])} دینار\n"
        f"🔹 زێڕی عەیار 22 بە {format_number(gold_iqd[22])} دینار\n"
        f"🔹 زێڕی عەیار 21 بە {format_number(gold_iqd[21])} دینار\n"
        f"🔹 زێڕی عەیار 18 بە {format_number(gold_iqd[18])} دینار\n"
        f"─────────────────────────────\n"
        f"🔸 نرخی یەک کیلۆ زیو بە {format_number(silver_kg_iqd)} دینار\n"
        f"▪️ ئۆنسەی زیو {silver_oz_formatted} دۆلار\n"
        f"─────────────────────────────\n"
        f"❇️ نرخی دۆلار ‌•••‌••• {usd_iqd_formatted} دینار"
    )
    return msg

# ─── SEND MESSAGE ─────────────────────────────────────────

async def send_price_update():
    try:
        gold_oz, silver_oz = await get_metals_prices()
        usd_iqd = await get_usd_iqd_rate()

        if not gold_oz or not silver_oz or not usd_iqd:
            print("⚠️ Failed to fetch prices")
            return

        gold_iqd = calculate_gold(gold_oz, usd_iqd)
        silver_kg_iqd = calculate_silver(silver_oz, usd_iqd)

        message = build_message(gold_oz, silver_oz, usd_iqd, gold_iqd, silver_kg_iqd)

        bot = Bot(token=TELEGRAM_TOKEN)
        await bot.send_message(chat_id=CHANNEL_ID, text=message)
        print(f"✅ Message sent at {datetime.now().strftime('%H:%M:%S')}")

    except Exception as e:
        print(f"❌ Error: {e}")

# ─── MAIN ─────────────────────────────────────────────────

async def main():
    print("🚀 Gold & Silver Bot started!")
    await send_price_update()  # send immediately on start

    scheduler = AsyncIOScheduler()
    scheduler.add_job(send_price_update, "interval", hours=1)
    scheduler.start()

    # Keep running forever
    while True:
        await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())

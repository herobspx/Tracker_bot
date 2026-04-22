import os
import json
import logging
import asyncio
import httpx
import websockets
from datetime import datetime, time
import pytz
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TRACKER_TOKEN"]
POLYGON_KEY    = os.environ["POLYGON_KEY"]
PRIVATE_GROUP  = -1003618409425
ADMIN_ID       = int(os.environ.get("ADMIN_ID", "0"))

active_trades = {}
TRADES_FILE   = "trades.json"
ET_TZ         = pytz.timezone("America/New_York")

# ─── Market Hours ──────────────────────────────────────────────────────────────
def is_market_open() -> bool:
    now_et = datetime.now(ET_TZ)
    if now_et.weekday() >= 5:  # Weekend
        return False
    market_open  = time(9, 30)
    market_close = time(16, 0)
    return market_open <= now_et.time() <= market_close

# ─── DB ───────────────────────────────────────────────────────────────────────
def load_trades():
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE, "r") as f:
            return json.load(f)
    return {}

def save_trades(data):
    clean = {}
    for k, v in data.items():
        clean[k] = {x: v[x] for x in ("symbol", "entry", "last_price", "type", "expiry", "opened_at", "polygon_ticker") if x in v}
    with open(TRADES_FILE, "w") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)

# ─── Format ───────────────────────────────────────────────────────────────────
def format_entry(trade: dict) -> str:
    emoji = "🔴" if trade["type"].upper() == "PUT" else "🟢"
    return (
        f"📌 *تتبع عقد جديد*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{emoji} {trade['symbol']} {trade['type'].upper()}\n"
        f"📅 {trade['expiry']}\n"
        f"💰 سعر الدخول: ${trade['entry']:.2f}\n"
        f"━━━━━━━━━━━━━━━━"
    )

def format_update(trade: dict, current: float, source: str = "") -> str:
    entry = trade["entry"]
    diff  = current - entry
    pct   = (diff / entry) * 100
    sign  = "+" if diff >= 0 else ""
    emoji = "📈" if diff > 0 else "📉"
    color = "🟢" if diff > 0 else "🔴"
    src   = f"\n🕐 {source}" if source else ""
    return (
        f"{emoji} *تحديث سعر*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📌 {trade['symbol']} {trade['type'].upper()} | {trade['expiry']}\n"
        f"💰 الدخول: ${entry:.2f}\n"
        f"💵 الحالي: ${current:.2f}\n"
        f"{color} {sign}${diff:.2f} ({sign}{pct:.1f}%){src}\n"
        f"━━━━━━━━━━━━━━━━"
    )

def format_close(trade: dict, close_price: float) -> str:
    entry  = trade["entry"]
    diff   = close_price - entry
    pct    = (diff / entry) * 100
    sign   = "+" if diff >= 0 else ""
    result = "✅ ربح" if diff > 0 else "❌ خسارة"
    return (
        f"{result}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📌 {trade['symbol']} {trade['type'].upper()} | {trade['expiry']}\n"
        f"💰 الدخول:  ${entry:.2f}\n"
        f"🏁 الخروج:  ${close_price:.2f}\n"
        f"📊 {sign}${diff:.2f} ({sign}{pct:.1f}%)\n"
        f"━━━━━━━━━━━━━━━━"
    )

# ─── REST API (outside market hours) ──────────────────────────────────────────
async def get_last_price_rest(polygon_ticker: str) -> float | None:
    url = f"https://api.polygon.io/v2/last/trade/{polygon_ticker}?apiKey={POLYGON_KEY}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                return float(data["results"]["p"])
    except Exception as e:
        logger.error(f"REST error for {polygon_ticker}: {e}")

    # Fallback: previous close
    url2 = f"https://api.polygon.io/v2/aggs/ticker/{polygon_ticker}/prev?apiKey={POLYGON_KEY}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url2)
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("results", [])
                if results:
                    return float(results[0]["c"])
    except Exception as e:
        logger.error(f"REST fallback error: {e}")
    return None

# ─── REST Polling (outside market hours) ──────────────────────────────────────
async def poll_rest(app, trade_key: str):
    logger.info(f"Starting REST polling for {trade_key}")
    while trade_key in active_trades:
        trade = active_trades[trade_key]

        if is_market_open():
            logger.info(f"Market open — switching to WebSocket for {trade_key}")
            asyncio.create_task(track_websocket(app, trade_key))
            return

        price = await get_last_price_rest(trade["polygon_ticker"])
        if price and abs(price - trade.get("last_price", trade["entry"])) >= 0.01:
            active_trades[trade_key]["last_price"] = price
            save_trades(active_trades)
            now_str = datetime.now(ET_TZ).strftime("%H:%M ET")
            await app.bot.send_message(
                chat_id=PRIVATE_GROUP,
                text=format_update(trade, price, f"آخر سعر متاح | {now_str}"),
                parse_mode="Markdown"
            )

        await asyncio.sleep(30)  # Check every 30 seconds outside market hours

# ─── WebSocket (during market hours) ──────────────────────────────────────────
async def track_websocket(app, trade_key: str):
    trade  = active_trades.get(trade_key)
    if not trade:
        return
    ticker = trade["polygon_ticker"]
    logger.info(f"Starting WebSocket for {ticker}")

    try:
        async with websockets.connect("wss://socket.polygon.io/options", ping_interval=20) as ws:
            await ws.send(json.dumps({"action": "auth", "params": POLYGON_KEY}))
            await ws.recv()
            await ws.send(json.dumps({"action": "subscribe", "params": f"T.{ticker}"}))
            logger.info(f"WebSocket subscribed: {ticker}")

            while trade_key in active_trades:
                if not is_market_open():
                    logger.info(f"Market closed — switching to REST for {trade_key}")
                    asyncio.create_task(poll_rest(app, trade_key))
                    return

                try:
                    msg  = await asyncio.wait_for(ws.recv(), timeout=30)
                    data = json.loads(msg)
                    for event in data:
                        if event.get("ev") == "T" and event.get("sym") == ticker:
                            price = float(event.get("p", 0))
                            if price <= 0:
                                continue
                            trade = active_trades.get(trade_key)
                            if not trade:
                                return
                            last = trade.get("last_price", trade["entry"])
                            if abs(price - last) >= 0.01:
                                active_trades[trade_key]["last_price"] = price
                                save_trades(active_trades)
                                await app.bot.send_message(
                                    chat_id=PRIVATE_GROUP,
                                    text=format_update(trade, price),
                                    parse_mode="Markdown"
                                )
                except asyncio.TimeoutError:
                    continue

    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        if trade_key in active_trades:
            await asyncio.sleep(5)
            if is_market_open():
                asyncio.create_task(track_websocket(app, trade_key))
            else:
                asyncio.create_task(poll_rest(app, trade_key))

# ─── Start tracking (auto pick WebSocket or REST) ──────────────────────────────
def start_tracking(app, trade_key: str):
    if is_market_open():
        asyncio.create_task(track_websocket(app, trade_key))
    else:
        asyncio.create_task(poll_rest(app, trade_key))

# ─── Build Polygon ticker ──────────────────────────────────────────────────────
def build_polygon_ticker(symbol: str, expiry: str, opt_type: str, strike: float) -> str:
    try:
        dt         = datetime.strptime(expiry, "%d%b%y")
        date_str   = dt.strftime("%y%m%d")
        type_char  = "P" if opt_type.upper() == "PUT" else "C"
        strike_str = f"{int(strike * 1000):08d}"
        return f"O:{symbol.upper()}{date_str}{type_char}{strike_str}"
    except Exception as e:
        logger.error(f"Ticker error: {e}")
        return ""

# ─── Commands ─────────────────────────────────────────────────────────────────
async def track_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args
    if len(args) < 5:
        await update.message.reply_text(
            "📌 *الاستخدام:*\n`/track SPXW 7110 Put 28Apr25 2.90`",
            parse_mode="Markdown"
        )
        return
    try:
        symbol   = args[0].upper()
        strike   = float(args[1])
        opt_type = args[2].capitalize()
        expiry   = args[3]
        entry    = float(args[4])

        polygon_ticker = build_polygon_ticker(symbol, expiry, opt_type, strike)
        if not polygon_ticker:
            await update.message.reply_text("⚠️ خطأ في تنسيق البيانات.")
            return

        trade_key = f"{symbol}_{strike}_{opt_type}_{expiry}"
        trade = {
            "symbol": symbol, "strike": strike, "type": opt_type,
            "expiry": expiry, "entry": entry, "last_price": entry,
            "polygon_ticker": polygon_ticker,
            "opened_at": datetime.now().isoformat()
        }
        active_trades[trade_key] = trade
        save_trades(active_trades)

        await context.bot.send_message(
            chat_id=PRIVATE_GROUP,
            text=format_entry(trade),
            parse_mode="Markdown"
        )

        status = "🟢 السوق مفتوح — تتبع لحظي" if is_market_open() else "🌙 السوق مغلق — تتبع كل دقيقة"
        start_tracking(context.application, trade_key)

        await update.message.reply_text(
            f"✅ بدأ التتبع\n`{polygon_ticker}`\n{status}",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ خطأ: {e}")

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args
    if not args:
        if not active_trades:
            await update.message.reply_text("لا يوجد عقود نشطة.")
            return
        lines = ["📋 *العقود النشطة:*\n"]
        for k, t in active_trades.items():
            diff = t["last_price"] - t["entry"]
            pct  = (diff / t["entry"]) * 100
            sign = "+" if diff >= 0 else ""
            lines.append(f"• `{k}` | ${t['last_price']:.2f} ({sign}{pct:.1f}%)")
        lines.append("\n`/stop TRADE_KEY سعر_الخروج`")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return
    trade_key   = args[0]
    close_price = float(args[1]) if len(args) > 1 else active_trades.get(trade_key, {}).get("last_price", 0)
    trade = active_trades.pop(trade_key, None)
    if not trade:
        await update.message.reply_text(f"⚠️ ما لقيت: `{trade_key}`", parse_mode="Markdown")
        return
    save_trades(active_trades)
    await context.bot.send_message(
        chat_id=PRIVATE_GROUP, text=format_close(trade, close_price), parse_mode="Markdown"
    )
    await update.message.reply_text("✅ تم الإغلاق.")

async def trades_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not active_trades:
        await update.message.reply_text("لا يوجد عقود نشطة.")
        return
    lines = ["📊 *العقود النشطة:*\n"]
    for k, t in active_trades.items():
        diff  = t["last_price"] - t["entry"]
        pct   = (diff / t["entry"]) * 100
        sign  = "+" if diff >= 0 else ""
        color = "🟢" if diff > 0 else "🔴"
        lines.append(f"{color} `{t['symbol']}` {t['type']} | ${t['entry']:.2f} → ${t['last_price']:.2f} ({sign}{pct:.1f}%)")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ─── Main ──────────────────────────────────────────────────────────────────────
def main():
    saved = load_trades()
    for k, t in saved.items():
        active_trades[k] = t

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("track",  track_cmd))
    app.add_handler(CommandHandler("stop",   stop_cmd))
    app.add_handler(CommandHandler("trades", trades_cmd))

    async def post_init(application):
        for trade_key in list(active_trades.keys()):
            start_tracking(application, trade_key)
            logger.info(f"Resumed: {trade_key}")

    app.post_init = post_init
    print("Tracker bot started!")
    app.run_polling()

if __name__ == "__main__":
    main()

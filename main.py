import os
import json
import logging
import asyncio
import websockets
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TRACKER_TOKEN"]
POLYGON_KEY    = os.environ["POLYGON_KEY"]
PRIVATE_GROUP  = -1003618409425
ADMIN_ID       = int(os.environ.get("ADMIN_ID", "0"))

# Active trades: {ticker: {entry, last_price, task}}
active_trades = {}
TRADES_FILE   = "trades.json"

# ─── DB ───────────────────────────────────────────────────────────────────────
def load_trades():
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE, "r") as f:
            return json.load(f)
    return {}

def save_trades(data):
    clean = {k: {x: v[x] for x in ("symbol", "entry", "last_price", "type", "expiry", "opened_at")} for k, v in data.items()}
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

def format_update(trade: dict, current: float) -> str:
    entry = trade["entry"]
    diff  = current - entry
    pct   = (diff / entry) * 100
    sign  = "+" if diff >= 0 else ""
    emoji = "📈" if diff > 0 else "📉"
    color = "🟢" if diff > 0 else "🔴"
    return (
        f"{emoji} *تحديث سعر*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📌 {trade['symbol']} {trade['type'].upper()} | {trade['expiry']}\n"
        f"💰 الدخول: ${entry:.2f}\n"
        f"💵 الحالي: ${current:.2f}\n"
        f"{color} {sign}${diff:.2f} ({sign}{pct:.1f}%)\n"
        f"━━━━━━━━━━━━━━━━"
    )

def format_close(trade: dict, close_price: float) -> str:
    entry = trade["entry"]
    diff  = close_price - entry
    pct   = (diff / entry) * 100
    sign  = "+" if diff >= 0 else ""
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

# ─── Polygon WebSocket ─────────────────────────────────────────────────────────
async def track_price(app, trade_key: str):
    trade   = active_trades[trade_key]
    ticker  = trade["polygon_ticker"]
    ws_url  = "wss://socket.polygon.io/options"

    logger.info(f"Starting WebSocket for {ticker}")

    try:
        async with websockets.connect(ws_url) as ws:
            # Auth
            await ws.send(json.dumps({"action": "auth", "params": POLYGON_KEY}))
            auth_resp = await ws.recv()
            logger.info(f"Auth: {auth_resp}")

            # Subscribe
            await ws.send(json.dumps({"action": "subscribe", "params": f"T.{ticker}"}))
            logger.info(f"Subscribed to T.{ticker}")

            while trade_key in active_trades:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=30)
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

                            # Send update if price changed
                            if abs(price - last) >= 0.01:
                                active_trades[trade_key]["last_price"] = price
                                save_trades(active_trades)

                                msg_text = format_update(trade, price)
                                await app.bot.send_message(
                                    chat_id=PRIVATE_GROUP,
                                    text=msg_text,
                                    parse_mode="Markdown"
                                )
                                logger.info(f"Update sent: {ticker} @ ${price}")

                except asyncio.TimeoutError:
                    logger.info(f"Heartbeat for {ticker}")
                    continue

    except Exception as e:
        logger.error(f"WebSocket error for {ticker}: {e}")
        # Retry after 5 seconds
        if trade_key in active_trades:
            await asyncio.sleep(5)
            asyncio.create_task(track_price(app, trade_key))

# ─── Build Polygon ticker ──────────────────────────────────────────────────────
def build_polygon_ticker(symbol: str, expiry: str, opt_type: str, strike: float) -> str:
    """
    Format: O:SPXW260422P07110000
    O:{symbol}{YYMMDD}{C/P}{strike*1000 padded to 8}
    """
    try:
        dt         = datetime.strptime(expiry, "%d%b%y")
        date_str   = dt.strftime("%y%m%d")
        type_char  = "P" if opt_type.upper() == "PUT" else "C"
        strike_str = f"{int(strike * 1000):08d}"
        return f"O:{symbol.upper()}{date_str}{type_char}{strike_str}"
    except Exception as e:
        logger.error(f"Ticker build error: {e}")
        return ""

# ─── /track command ────────────────────────────────────────────────────────────
# Usage: /track SPXW 7110 Put 28Apr25 2.90
async def track_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    args = context.args
    if len(args) < 5:
        await update.message.reply_text(
            "📌 *طريقة الاستخدام:*\n"
            "`/track SPXW 7110 Put 28Apr25 2.90`\n\n"
            "• SPXW = الرمز\n"
            "• 7110 = Strike\n"
            "• Put أو Call\n"
            "• 28Apr25 = تاريخ الانتهاء\n"
            "• 2.90 = سعر الدخول",
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
            "symbol":         symbol,
            "strike":         strike,
            "type":           opt_type,
            "expiry":         expiry,
            "entry":          entry,
            "last_price":     entry,
            "polygon_ticker": polygon_ticker,
            "opened_at":      datetime.now().isoformat()
        }

        active_trades[trade_key] = trade
        save_trades(active_trades)

        # Post entry to group
        await context.bot.send_message(
            chat_id=PRIVATE_GROUP,
            text=format_entry(trade),
            parse_mode="Markdown"
        )

        # Start tracking
        asyncio.create_task(track_price(context.application, trade_key))

        await update.message.reply_text(
            f"✅ بدأ تتبع `{polygon_ticker}`\n"
            f"سيتم إرسال تحديث لكل تغيير في السعر 🔄",
            parse_mode="Markdown"
        )

    except Exception as e:
        await update.message.reply_text(f"⚠️ خطأ: {e}")

# ─── /stop command ─────────────────────────────────────────────────────────────
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
        lines.append("\nللإغلاق: `/stop TRADE_KEY سعر_الخروج`")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    trade_key = args[0]
    try:
        close_price = float(args[1]) if len(args) > 1 else active_trades[trade_key]["last_price"]
    except:
        await update.message.reply_text("⚠️ أدخل سعر الخروج.")
        return

    trade = active_trades.pop(trade_key, None)
    if not trade:
        await update.message.reply_text(f"⚠️ ما لقيت: `{trade_key}`", parse_mode="Markdown")
        return

    save_trades(active_trades)
    await context.bot.send_message(
        chat_id=PRIVATE_GROUP,
        text=format_close(trade, close_price),
        parse_mode="Markdown"
    )
    await update.message.reply_text("✅ تم إغلاق العقد ونشر النتيجة.")

# ─── /trades command ───────────────────────────────────────────────────────────
async def trades_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not active_trades:
        await update.message.reply_text("لا يوجد عقود نشطة حالياً.")
        return
    lines = ["📊 *العقود النشطة:*\n"]
    for k, t in active_trades.items():
        diff = t["last_price"] - t["entry"]
        pct  = (diff / t["entry"]) * 100
        sign = "+" if diff >= 0 else ""
        color = "🟢" if diff > 0 else "🔴"
        lines.append(
            f"{color} `{t['symbol']}` {t['type']} | دخول: ${t['entry']:.2f} | حالي: ${t['last_price']:.2f} ({sign}{pct:.1f}%)"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ─── Main ──────────────────────────────────────────────────────────────────────
def main():
    # Load saved trades
    saved = load_trades()
    for k, t in saved.items():
        active_trades[k] = t

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("track",  track_cmd))
    app.add_handler(CommandHandler("stop",   stop_cmd))
    app.add_handler(CommandHandler("trades", trades_cmd))

    # Resume tracking for saved trades
    async def post_init(application):
        for trade_key in list(active_trades.keys()):
            asyncio.create_task(track_price(application, trade_key))
            logger.info(f"Resumed tracking: {trade_key}")

    app.post_init = post_init
    print("Tracker bot started!")
    app.run_polling()

if __name__ == "__main__":
    main()

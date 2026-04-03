import asyncio
import io
import logging
import re
import sqlite3
import time
from datetime import datetime, timezone, timedelta

import aiohttp
import qrcode

from telegram import (
    Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update, ChatMember
)
from telegram.ext import (
    ApplicationBuilder, CallbackQueryHandler, CommandHandler,
    MessageHandler, ContextTypes, filters
)
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, SessionPasswordNeededError

# ─── CONFIG ──────────────────────────────────────────────────────────────────
BOT_TOKEN      = "8565169210:AAHPKa7S7FEI11pgaMlmrDMs7ZcE59rUYdM"
ADMIN_IDS      = [5390485406]
ADMIN_GROUP_ID = -1003886464823
LOG_CHANNEL_ID = -1003886464823
API_ID         = 30191201
API_HASH       = "5c87a8808e935cc3d97958d0bb24ff1f"
UPI_ID         = "raunitkumar01@fam"
DB_PATH        = "numberstore7.db"
IST            = timezone(timedelta(hours=5, minutes=30))

OXAPAY_MERCHANT_KEY = "R7GWJN-NPCMVX-H3QYHQ-FL2DJA"
OXAPAY_API_BASE     = "https://api.oxapay.com"
STORE_TAG           = "@iampritichat_bot"
STORE_LINK          = "https://t.me/iampritichat_bot"
SERVER_NUM          = 1

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ─── MARKDOWN ESCAPE (for MarkdownV2) ────────────────────────────────────────
def escape_mdv2(text):
    """Escape special characters for Telegram MarkdownV2."""
    if not text:
        return ""
    special_chars = r'_*[]()~`>#+-=|{}.!'
    for ch in special_chars:
        text = text.replace(ch, f'\\{ch}')
    return text


def mesc(t):
    return escape_mdv2(str(t))


# ─── DATABASE ─────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS force_channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id TEXT UNIQUE, channel_link TEXT, channel_name TEXT
    );
    CREATE TABLE IF NOT EXISTS stock_categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE, price_inr REAL DEFAULT 0, price_usd REAL DEFAULT 0,
        enabled INTEGER DEFAULT 1, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category_id INTEGER, category_name TEXT,
        phone_number TEXT, session_string TEXT, two_fa_password TEXT,
        is_sold INTEGER DEFAULT 0, sold_to INTEGER, sold_at TIMESTAMP,
        added_by INTEGER, added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, username TEXT, account_id INTEGER,
        category_id INTEGER, category_name TEXT,
        amount_inr REAL, amount_usd REAL,
        payment_method TEXT DEFAULT 'upi',
        payment_screenshot TEXT, crypto_track_id TEXT,
        status TEXT DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        reviewed_by INTEGER, reviewed_at TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
        is_banned INTEGER DEFAULT 0, total_purchases INTEGER DEFAULT 0,
        wallet_balance REAL DEFAULT 0,
        joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS deposits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, amount_inr REAL, amount_usd REAL,
        payment_method TEXT DEFAULT 'upi',
        screenshot TEXT, crypto_track_id TEXT,
        status TEXT DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        reviewed_by INTEGER, reviewed_at TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT
    );
    """)
    for k, v in [
        ("maintenance",     "0"),
        ("upi_enabled",     "1"),
        ("crypto_enabled",  "1"),
        ("usdt_rate",       "83"),
        ("welcome_message", "🏪 Welcome to NumberStore!\nBuy verified phone numbers instantly.\nFast • Secure • 24/7"),
    ]:
        c.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (k, v))
    conn.commit()
    conn.close()


# ─── HELPERS ─────────────────────────────────────────────────────────────────
def now_ist():
    return datetime.now(IST)

def fmt_time(ts_str):
    if not ts_str:
        return "N/A"
    try:
        dt = datetime.fromisoformat(str(ts_str))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(IST).strftime("%d %b %Y %H:%M IST")
    except Exception:
        return str(ts_str)

def get_setting(key, default=""):
    conn = get_db()
    row  = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default

def set_setting(key, value):
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, str(value)))
    conn.commit()
    conn.close()

def get_usdt_rate():
    try:
        return float(get_setting("usdt_rate", "83"))
    except Exception:
        return 83.0

def inr_to_usd(inr):
    rate = get_usdt_rate()
    return round(inr / rate, 2) if rate > 0 else 0.0

def register_user(user):
    conn = get_db()
    conn.execute(
        "INSERT OR IGNORE INTO users (id,username,first_name,joined_at) VALUES (?,?,?,?)",
        (user.id, user.username or "", user.first_name or "", now_ist().isoformat())
    )
    conn.execute(
        "UPDATE users SET username=?,first_name=? WHERE id=?",
        (user.username or "", user.first_name or "", user.id)
    )
    conn.commit()
    conn.close()

def is_banned(user_id):
    conn = get_db()
    row  = conn.execute("SELECT is_banned FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    return row and row["is_banned"] == 1

def is_maintenance():
    return get_setting("maintenance", "0") == "1"

def is_admin(user_id):
    return user_id in ADMIN_IDS

def status_emoji(s):
    return {"pending":"⏳","approved":"✅","rejected":"❌","paid":"💚","expired":"⌛"}.get(s,"❓")

def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Browse Numbers", callback_data="browse_0", style="primary"),
         InlineKeyboardButton("💰 My Wallet",       callback_data="wallet", style="primary")],
        [InlineKeyboardButton("📦 My Orders",       callback_data="my_orders_0", style="primary"),
         InlineKeyboardButton("❓ Help",             callback_data="help", style="primary")],
    ])

def generate_upi_qr(amount, note):
    upi_url = f"upi://pay?pa={UPI_ID}&pn=NumberStore&am={amount}&cu=INR&tn={note}"
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(upi_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf

def get_stock_count(cat_id):
    conn = get_db()
    row  = conn.execute("SELECT COUNT(*) as c FROM accounts WHERE category_id=? AND is_sold=0", (cat_id,)).fetchone()
    conn.close()
    return row["c"] if row else 0

def get_cat(cat_id):
    conn = get_db()
    row  = conn.execute("SELECT * FROM stock_categories WHERE id=?", (cat_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ─── FORCE-SUB ───────────────────────────────────────────────────────────────
def get_force_channels():
    conn = get_db()
    rows = conn.execute("SELECT * FROM force_channels").fetchall()
    conn.close()
    return [dict(r) for r in rows]

async def check_force_sub(bot, user_id):
    not_joined = []
    for ch in get_force_channels():
        try:
            member = await bot.get_chat_member(chat_id=ch["channel_id"], user_id=user_id)
            if member.status in (ChatMember.LEFT, ChatMember.BANNED):
                not_joined.append(ch)
        except Exception:
            not_joined.append(ch)
    return not_joined

async def send_force_sub_msg(update, not_joined):
    buttons = []
    for i, ch in enumerate(not_joined, 1):
        label = ch["channel_name"] or f"Channel {i}"
        buttons.append([InlineKeyboardButton(f"➕ Join {label}", url=ch["channel_link"], style="primary")])
    buttons.append([InlineKeyboardButton("✅ I've Joined — Verify", callback_data="verify_sub", style="success")])
    lines = ["⚠️ *Access Restricted*\n━━━━━━━━━━━━━━━━━━━━\nJoin these channels to use the bot:\n"]
    for ch in not_joined:
        lines.append(f"• {ch['channel_name'] or ch['channel_id']}")
    lines.append("\n━━━━━━━━━━━━━━━━━━━━\n_Tap Verify after joining._")
    msg = update.message or (update.callback_query.message if update.callback_query else None)
    if msg:
        try:
            await msg.reply_text("\n".join(lines), parse_mode="Markdown",
                                 reply_markup=InlineKeyboardMarkup(buttons))
        except Exception:
            pass


# ─── GUARD ───────────────────────────────────────────────────────────────────
async def guard(update, context):
    user = update.effective_user
    if not user:
        return True
    register_user(user)
    if is_banned(user.id):
        txt = "🚫 You are banned from using this bot."
        if update.callback_query:
            await update.callback_query.answer(txt, show_alert=True)
        else:
            await update.effective_message.reply_text(txt)
        return True
    if is_maintenance() and not is_admin(user.id):
        txt = "🔧 Bot is under maintenance. Please try again later."
        if update.callback_query:
            await update.callback_query.answer(txt, show_alert=True)
        else:
            await update.effective_message.reply_text(txt)
        return True
    if not is_admin(user.id):
        not_joined = await check_force_sub(context.bot, user.id)
        if not_joined:
            await send_force_sub_msg(update, not_joined)
            return True
    return False


# ─── VERIFY SUB ──────────────────────────────────────────────────────────────
async def verify_sub(update, context):
    query = update.callback_query
    await query.answer()
    not_joined = await check_force_sub(context.bot, query.from_user.id)
    if not_joined:
        buttons = []
        for i, ch in enumerate(not_joined, 1):
            buttons.append([InlineKeyboardButton(f"➕ Join {ch['channel_name'] or f'Channel {i}'}", url=ch["channel_link"], style="primary")])
        buttons.append([InlineKeyboardButton("✅ Verify Again", callback_data="verify_sub", style="success")])
        lines = ["❌ Still not joined all channels!\n"]
        for ch in not_joined:
            lines.append(f"• {ch['channel_name'] or ch['channel_id']}")
        await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))
    else:
        msg = get_setting("welcome_message", "🏪 Welcome to NumberStore!")
        await query.edit_message_text(msg, reply_markup=main_menu_kb())


# ─── /start ──────────────────────────────────────────────────────────────────
async def start(update, context):
    if await guard(update, context):
        return
    msg = get_setting("welcome_message", "🏪 Welcome to NumberStore!")
    await update.message.reply_text(msg, reply_markup=main_menu_kb())


# ─── /addchannel /removechannel ──────────────────────────────────────────────
async def addchannel_cmd(update, context):
    if not is_admin(update.effective_user.id):
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /addchannel <channel_id> <invite_link> [Name]")
        return
    ch_id, ch_link = args[0], args[1]
    ch_name = " ".join(args[2:]) if len(args) > 2 else ch_id
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO force_channels (channel_id,channel_link,channel_name) VALUES (?,?,?)",
                 (ch_id, ch_link, ch_name))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Channel added: {ch_name}")

async def removechannel_cmd(update, context):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /removechannel <channel_id>")
        return
    conn = get_db()
    conn.execute("DELETE FROM force_channels WHERE channel_id=?", (context.args[0],))
    conn.commit()
    conn.close()
    await update.message.reply_text("✅ Channel removed.")


# ─── LOG CHANNEL ─────────────────────────────────────────────────────────────
async def send_purchase_log(bot, category_name, price_inr, phone_number, username, user_id):
    ph = str(phone_number)
    masked = f"+{ph[:4]}{'•' * max(0, len(ph)-4)}"
    user_tag = f"@{username}" if username else f"ID:{user_id}"
    text = (
        f"✅ New Number Purchase Successful\n"
        f"➖ Category: {category_name} | ₹{price_inr:.0f}\n"
        f"➕ Number: {masked} 📞\n"
        f"➕ Server: ({SERVER_NUM}) 🥂\n"
        f"• {user_tag} || {STORE_TAG}"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Buy Now", url=STORE_LINK, style="primary")]])
    try:
        await bot.send_message(chat_id=LOG_CHANNEL_ID, text=text, reply_markup=kb)
    except Exception as e:
        logger.error(f"Log channel error: {e}")


# ─── OXAPAY ──────────────────────────────────────────────────────────────────
async def oxapay_create_invoice(amount_usd, desc, order_ref):
    payload = {
        "merchant": OXAPAY_MERCHANT_KEY, "amount": round(float(amount_usd), 2),
        "currency": "USDT", "lifeTime": 30, "feePaidByPayer": 1,
        "description": desc, "orderId": order_ref,
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{OXAPAY_API_BASE}/merchants/request", json=payload) as r:
                data = await r.json()
                if data.get("result") == 100:
                    return {"payLink": data["payLink"], "trackId": data["trackId"]}
                logger.error(f"OxaPay: {data}")
    except Exception as e:
        logger.error(f"OxaPay failed: {e}")
    return None

async def oxapay_check(track_id):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{OXAPAY_API_BASE}/merchants/inquiry",
                              json={"merchant": OXAPAY_MERCHANT_KEY, "trackId": track_id}) as r:
                data = await r.json()
                if data.get("result") == 100:
                    return data.get("status")
    except Exception as e:
        logger.error(f"OxaPay check: {e}")
    return None

async def poll_crypto_order(context, track_id, user_id, order_id):
    for _ in range(60):
        await asyncio.sleep(30)
        status = await oxapay_check(track_id)
        if status == "Paid":
            conn  = get_db()
            order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if order and order["status"] == "pending":
                acc = conn.execute("SELECT * FROM accounts WHERE category_id=? AND is_sold=0 LIMIT 1",
                                   (order["category_id"],)).fetchone()
                now = now_ist().isoformat()
                if acc:
                    conn.execute("UPDATE accounts SET is_sold=1,sold_to=?,sold_at=? WHERE id=?",
                                 (user_id, now, acc["id"]))
                    conn.execute("UPDATE orders SET status='approved',account_id=?,reviewed_at=? WHERE id=?",
                                 (acc["id"], now, order_id))
                    conn.execute("UPDATE users SET total_purchases=total_purchases+1 WHERE id=?", (user_id,))
                    conn.commit()
                    conn.close()
                    await send_purchase_log(context.bot, order["category_name"], order["amount_inr"],
                                            acc["phone_number"], order["username"], user_id)
                    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📱 Reveal Number",
                                               callback_data=f"reveal_{order_id}", style="success")]])
                    try:
                        await context.bot.send_message(chat_id=user_id,
                            text=f"✅ Crypto payment confirmed! Order #{order_id} approved.",
                            reply_markup=kb)
                    except Exception:
                        pass
                else:
                    conn.execute("UPDATE orders SET status='rejected',reviewed_at=? WHERE id=?",
                                 (now, order_id))
                    conn.commit()
                    conn.close()
                    try:
                        await context.bot.send_message(chat_id=user_id,
                            text="❌ Payment received but no stock. Refund will be processed.")
                    except Exception:
                        pass
            else:
                conn.close()
            return
        elif status in ("Expired", "Failed"):
            break
    try:
        await context.bot.send_message(chat_id=user_id, text="⌛ Crypto payment expired.")
    except Exception:
        pass

async def poll_crypto_deposit(context, track_id, user_id, dep_id):
    for _ in range(60):
        await asyncio.sleep(30)
        status = await oxapay_check(track_id)
        if status == "Paid":
            conn = get_db()
            dep  = conn.execute("SELECT * FROM deposits WHERE id=?", (dep_id,)).fetchone()
            if dep and dep["status"] == "pending":
                now = now_ist().isoformat()
                conn.execute("UPDATE deposits SET status='approved',reviewed_at=? WHERE id=?", (now, dep_id))
                conn.execute("UPDATE users SET wallet_balance=wallet_balance+? WHERE id=?",
                             (dep["amount_inr"], user_id))
                conn.commit()
                try:
                    await context.bot.send_message(chat_id=user_id,
                        text=f"✅ Crypto deposit of ₹{dep['amount_inr']:.0f} credited to your wallet!",
                        reply_markup=main_menu_kb())
                except Exception:
                    pass
            conn.close()
            return
        elif status in ("Expired", "Failed"):
            break
    try:
        await context.bot.send_message(chat_id=user_id, text="⌛ Crypto deposit expired.")
    except Exception:
        pass


# ─── BROWSE ──────────────────────────────────────────────────────────────────
async def browse_numbers(update, context):
    query = update.callback_query
    await query.answer()
    if await guard(update, context):
        return
    page = int(query.data.split("_")[1])
    conn = get_db()
    cats = conn.execute("""
        SELECT s.*, (SELECT COUNT(*) FROM accounts a WHERE a.category_id=s.id AND a.is_sold=0) as stock_count
        FROM stock_categories s WHERE s.enabled=1 ORDER BY s.name
    """).fetchall()
    cats = [c for c in cats if c["stock_count"] > 0]
    conn.close()
    per_page = 5
    total    = len(cats)
    if total == 0:
        await query.edit_message_text("📦 *No stock available at the moment!*", parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu", style="primary")]]))
        return
    pages = max(1, (total + per_page - 1) // per_page)
    page  = max(0, min(page, pages - 1))
    chunk = cats[page * per_page:(page + 1) * per_page]
    upi_on    = get_setting("upi_enabled",   "1") == "1"
    crypto_on = get_setting("crypto_enabled","1") == "1"
    pay_icons = ("💳UPI " if upi_on else "") + ("🪙Crypto" if crypto_on else "")
    lines   = ["🛒 *Available Numbers*\n━━━━━━━━━━━━━━━━━━━━"]
    buttons = []
    for c in chunk:
        lines.append(f"📂 *{mesc(c['name'])}*\n   📦 Stock: {c['stock_count']}  |  ₹{c['price_inr']:.0f}  |  ${c['price_usd']:.2f}")
        buttons.append([InlineKeyboardButton(
            f"📂 {c['name']}  •  📦{c['stock_count']}  •  ₹{c['price_inr']:.0f}",
            callback_data=f"cat_{c['id']}", style="primary"
        )])
    lines += ["━━━━━━━━━━━━━━━━━━━━", f"_Page {page+1}/{pages}  •  Pay: {pay_icons}_"]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"browse_{page-1}", style="primary"))
        nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data="noop", style="primary"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"browse_{page+1}", style="primary"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu", style="primary")])
    await query.edit_message_text("\n".join(lines), parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup(buttons))

async def noop_callback(update, context):
    await update.callback_query.answer()


# ─── CATEGORY DETAIL ─────────────────────────────────────────────────────────
async def category_detail(update, context):
    query = update.callback_query
    await query.answer()
    if await guard(update, context):
        return
    cat_id = int(query.data.split("_")[1])
    c = get_cat(cat_id)
    if not c:
        await query.edit_message_text("Category not found.")
        return
    stock = get_stock_count(cat_id)
    if stock == 0:
        await query.edit_message_text(
            f"📂 *{mesc(c['name'])}*\n━━━━━━━━━━━━━━━━━━━━\n❌ *Out of stock!*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="browse_0", style="primary")]]))
        return
    conn = get_db()
    user_row = conn.execute("SELECT wallet_balance FROM users WHERE id=?", (query.from_user.id,)).fetchone()
    conn.close()
    wallet    = user_row["wallet_balance"] if user_row else 0
    upi_on    = get_setting("upi_enabled",   "1") == "1"
    crypto_on = get_setting("crypto_enabled","1") == "1"
    text = (
        f"📂 *{mesc(c['name'])}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Price: ₹{c['price_inr']:.0f} INR  |  ${c['price_usd']:.2f} USDT\n"
        f"📦 Stock: {stock} available\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    buttons = []
    if upi_on:
        buttons.append([InlineKeyboardButton("💳 Buy with UPI", callback_data=f"pay_upi_{cat_id}", style="success")])
    if crypto_on:
        buttons.append([InlineKeyboardButton("🪙 Buy with Crypto (OxaPay)", callback_data=f"pay_crypto_{cat_id}", style="success")])
    buttons.append([InlineKeyboardButton(f"💰 Buy from Wallet  (₹{wallet:.2f})", callback_data=f"wallet_buy_{cat_id}", style="success")])
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="browse_0", style="primary")])
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))


# ─── WALLET BUY ──────────────────────────────────────────────────────────────
async def wallet_buy(update, context):
    query = update.callback_query
    await query.answer()
    if await guard(update, context):
        return
    cat_id   = int(query.data.split("_")[2])
    c        = get_cat(cat_id)
    user_id  = query.from_user.id
    conn     = get_db()
    user_row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user_row or not c:
        conn.close()
        await query.edit_message_text("Error.")
        return
    wallet = user_row["wallet_balance"]
    price  = c["price_inr"]
    if wallet < price:
        conn.close()
        await query.edit_message_text(
            f"❌ Insufficient balance.\nNeed ₹{price:.0f}, you have ₹{wallet:.2f}.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Deposit Funds", callback_data="wallet", style="success")],
                [InlineKeyboardButton("🔙 Back", callback_data=f"cat_{cat_id}", style="primary")],
            ]))
        return
    acc = conn.execute("SELECT * FROM accounts WHERE category_id=? AND is_sold=0 LIMIT 1", (cat_id,)).fetchone()
    if not acc:
        conn.close()
        await query.edit_message_text("❌ No accounts available right now.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"cat_{cat_id}", style="primary")]]))
        return
    now = now_ist().isoformat()
    conn.execute("UPDATE accounts SET is_sold=1,sold_to=?,sold_at=? WHERE id=?", (user_id, now, acc["id"]))
    conn.execute("UPDATE users SET wallet_balance=wallet_balance-?,total_purchases=total_purchases+1 WHERE id=?",
                 (price, user_id))
    order_id = conn.execute(
        "INSERT INTO orders (user_id,username,account_id,category_id,category_name,amount_inr,payment_method,status,created_at,reviewed_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (user_id, query.from_user.username or "", acc["id"], cat_id, c["name"], price, "wallet", "approved", now, now)
    ).lastrowid
    conn.commit()
    conn.close()
    await send_purchase_log(context.bot, c["name"], price, acc["phone_number"],
                            query.from_user.username or "", user_id)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📱 Reveal My Number", callback_data=f"reveal_{order_id}", style="success")]])
    await query.edit_message_text("✅ Purchased successfully!", reply_markup=kb)


# ─── PAY UPI ─────────────────────────────────────────────────────────────────
async def pay_upi(update, context):
    query = update.callback_query
    await query.answer()
    if await guard(update, context):
        return
    cat_id = int(query.data.split("_")[2])
    c = get_cat(cat_id)
    if not c:
        await query.edit_message_text("Category not found.")
        return
    context.user_data["buy_cat_id"] = cat_id
    qr_buf  = generate_upi_qr(c["price_inr"], f"Order")
    caption = (
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💳 UPI Payment\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Amount: ₹{c['price_inr']:.0f}\n"
        f"🏦 UPI ID: {UPI_ID}\n"
        f"📱 PhonePe / GPay / Paytm\n"
        f"⚠️ Pay EXACT amount\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📸 I've Paid — Upload Screenshot", callback_data=f"buy_upload_{cat_id}", style="success")],
        [InlineKeyboardButton("🔙 Back", callback_data=f"cat_{cat_id}", style="primary")],
    ])
    await query.message.reply_photo(photo=qr_buf, caption=caption, reply_markup=kb)
    try:
        await query.message.delete()
    except Exception:
        pass

async def buy_upload_prompt(update, context):
    query = update.callback_query
    await query.answer()
    if await guard(update, context):
        return
    cat_id = int(query.data.split("_")[2])
    context.user_data["buy_cat_id"]              = cat_id
    context.user_data["awaiting_buy_screenshot"] = True
    await query.message.reply_text(
        "📸 Please send your payment screenshot as a *photo*.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cat_{cat_id}", style="danger")]]))
    try:
        await query.message.delete()
    except Exception:
        pass


# ─── PAY CRYPTO ──────────────────────────────────────────────────────────────
async def pay_crypto(update, context):
    query = update.callback_query
    await query.answer("⏳ Creating invoice...")
    if await guard(update, context):
        return
    cat_id = int(query.data.split("_")[2])
    c = get_cat(cat_id)
    if not c or not c["price_usd"]:
        await query.edit_message_text("❌ USD price not set. Contact admin.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"cat_{cat_id}", style="primary")]]))
        return
    user_id = query.from_user.id
    invoice = await oxapay_create_invoice(c["price_usd"], f"Buy {c['name']}", f"order_{user_id}_{int(time.time())}")
    if not invoice:
        await query.edit_message_text("❌ Failed to create invoice. Try later.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"cat_{cat_id}", style="primary")]]))
        return
    now = now_ist().isoformat()
    conn = get_db()
    order_id = conn.execute(
        "INSERT INTO orders (user_id,username,category_id,category_name,amount_inr,amount_usd,payment_method,crypto_track_id,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (user_id, query.from_user.username or "", cat_id, c["name"],
         c["price_inr"], c["price_usd"], "crypto", invoice["trackId"], "pending", now)
    ).lastrowid
    conn.commit()
    conn.close()
    text = (
        f"🪙 *Crypto Payment*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📂 Item: {mesc(c['name'])}\n"
        f"💰 Amount: ${c['price_usd']:.2f} USDT\n"
        f"⏱ Expires in: 30 minutes\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Tap Pay Now → auto-verified on payment ✅"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Pay Now (OxaPay)", url=invoice["payLink"], style="success")],
        [InlineKeyboardButton("🔄 Check Status", callback_data=f"chk_ord_{order_id}", style="primary")],
        [InlineKeyboardButton("🔙 Back", callback_data=f"cat_{cat_id}", style="primary")],
    ])
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
    asyncio.create_task(poll_crypto_order(context, invoice["trackId"], user_id, order_id))

async def check_crypto_order_cb(update, context):
    query    = update.callback_query
    order_id = int(query.data.split("_")[2])
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    conn.close()
    if not order:
        await query.answer("Order not found.", show_alert=True)
        return
    if order["status"] == "approved":
        await query.answer("✅ Payment confirmed!", show_alert=True)
        await query.edit_message_text("✅ Payment confirmed! Your number is ready.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📱 Reveal Number",
                                               callback_data=f"reveal_{order_id}", style="success")]]))
        return
    status = await oxapay_check(order["crypto_track_id"])
    label  = {"Waiting":"⏳ Waiting","Paid":"✅ Paid","Expired":"⌛ Expired","Failed":"❌ Failed"}.get(status,"❓")
    await query.answer(f"Status: {label}", show_alert=True)


# ─── SCREENSHOT HANDLER ───────────────────────────────────────────────────────
async def screenshot_handler(update, context):
    if await guard(update, context):
        return
    user = update.effective_user

    if context.user_data.get("awaiting_buy_screenshot"):
        context.user_data.pop("awaiting_buy_screenshot")
        cat_id  = context.user_data.get("buy_cat_id")
        file_id = update.message.photo[-1].file_id if update.message.photo else None
        if not file_id:
            await update.message.reply_text("❌ Please send a photo.")
            return
        c = get_cat(cat_id)
        if not c:
            await update.message.reply_text("❌ Session expired.", reply_markup=main_menu_kb())
            return
        conn = get_db()
        order_id = conn.execute(
            "INSERT INTO orders (user_id,username,category_id,category_name,amount_inr,payment_method,payment_screenshot,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (user.id, user.username or "", cat_id, c["name"], c["price_inr"], "upi", file_id, "pending", now_ist().isoformat())
        ).lastrowid
        conn.commit()
        conn.close()
        admin_text = (
            f"NEW ORDER #{order_id}\n"
            f"User: @{user.username or 'N/A'} (ID: {user.id})\n"
            f"Category: {c['name']}\n"
            f"Amount: Rs{c['price_inr']:.0f} INR | UPI\n"
            f"Time: {now_ist().strftime('%d %b %Y %H:%M IST')}"
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"✅ Approve #{order_id}", callback_data=f"approve_order_{order_id}", style="success"),
            InlineKeyboardButton(f"❌ Reject #{order_id}",  callback_data=f"reject_order_{order_id}", style="danger"),
        ]])
        try:
            await context.bot.send_photo(chat_id=ADMIN_GROUP_ID, photo=file_id,
                                         caption=admin_text, reply_markup=kb)
        except Exception as e:
            logger.error(f"Admin group: {e}")
        await update.message.reply_text("⏳ Payment submitted! Awaiting admin approval.",
                                        reply_markup=main_menu_kb())
        return

    if context.user_data.get("awaiting_deposit_screenshot"):
        context.user_data.pop("awaiting_deposit_screenshot")
        dep_inr = context.user_data.get("dep_inr", 0)
        file_id = update.message.photo[-1].file_id if update.message.photo else None
        if not file_id:
            await update.message.reply_text("❌ Please send a photo.")
            return
        conn   = get_db()
        dep_id = conn.execute(
            "INSERT INTO deposits (user_id,amount_inr,payment_method,screenshot,status,created_at) VALUES (?,?,?,?,?,?)",
            (user.id, dep_inr, "upi", file_id, "pending", now_ist().isoformat())
        ).lastrowid
        conn.commit()
        conn.close()
        admin_text = (
            f"DEPOSIT #{dep_id}\n"
            f"User: @{user.username or 'N/A'} (ID: {user.id})\n"
            f"Amount: Rs{dep_inr:.0f} INR | UPI\n"
            f"Time: {now_ist().strftime('%d %b %Y %H:%M IST')}"
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"✅ Approve #{dep_id}", callback_data=f"approve_deposit_{dep_id}", style="success"),
            InlineKeyboardButton(f"❌ Reject #{dep_id}",  callback_data=f"reject_deposit_{dep_id}", style="danger"),
        ]])
        try:
            await context.bot.send_photo(chat_id=ADMIN_GROUP_ID, photo=file_id,
                                         caption=admin_text, reply_markup=kb)
        except Exception as e:
            logger.error(f"Admin group: {e}")
        await update.message.reply_text("⏳ Deposit submitted! Awaiting approval.",
                                        reply_markup=main_menu_kb())
        return


# ─── REVEAL NUMBER ────────────────────────────────────────────────────────────
async def reveal_number(update, context):
    query = update.callback_query
    await query.answer()
    if await guard(update, context):
        return
    order_id = int(query.data.split("_")[1])
    user_id  = query.from_user.id
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id=? AND user_id=?", (order_id, user_id)).fetchone()
    if not order or order["status"] != "approved":
        conn.close()
        await query.edit_message_text("❌ Order not found or not approved.")
        return
    acc = conn.execute("SELECT * FROM accounts WHERE id=?", (order["account_id"],)).fetchone()
    conn.close()
    if not acc:
        await query.edit_message_text("❌ Account data not found.")
        return
    text = (
        f"📱 *Your Number Details*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📂 Category: {mesc(order['category_name'])}\n"
        f"📞 Number: `+{acc['phone_number']}`\n"
        f"🔐 2FA: `{acc['two_fa_password'] or 'Not set'}`\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📨 Get Latest OTP",     callback_data=f"getotp_{acc['id']}", style="success")],
        [InlineKeyboardButton("🔒 Logout Bot Session", callback_data=f"logout_prompt_{acc['id']}", style="danger")],
        [InlineKeyboardButton("📦 My Orders",          callback_data="my_orders_0", style="primary")],
    ])
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)


# ─── GET OTP ─────────────────────────────────────────────────────────────────
async def get_otp(update, context):
    query = update.callback_query
    await query.answer("⏳ Fetching OTP...")
    acc_id = int(query.data.split("_")[1])
    conn = get_db()
    acc  = conn.execute("SELECT * FROM accounts WHERE id=?", (acc_id,)).fetchone()
    conn.close()
    if not acc:
        await query.edit_message_text("❌ Account not found.")
        return
    if not acc["session_string"]:
        await query.edit_message_text("ℹ️ Bot session already logged out.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back",
                callback_data=f"reveal_{_get_order_for_acc(acc_id)}", style="primary")]]))
        return
    await query.edit_message_text("⏳ Connecting to fetch OTP...")
    otp_code  = None
    error_msg = None
    client    = TelegramClient(StringSession(acc["session_string"]), API_ID, API_HASH)
    try:
        await client.connect()
        otp_code = await _fetch_otp(client)
    except FloodWaitError as e:
        error_msg = f"⏳ Please wait {e.seconds} seconds."
    except Exception as e:
        error_msg = ("❌ Session expired." if "session" in str(e).lower() or "auth" in str(e).lower()
                     else "⚠️ Could not fetch OTP.")
        logger.error(f"OTP fetch: {e}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
    if error_msg:
        await query.edit_message_text(error_msg,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back",
                callback_data=f"reveal_{_get_order_for_acc(acc_id)}", style="primary")]]))
        return
    text = (
        f"🔑 *Latest OTP:* `{otp_code or 'Not found'}`\n"
        f"📞 `+{acc['phone_number']}`\n"
        f"🔐 2FA: `{acc['two_fa_password'] or 'Not set'}`\n"
        f"⏱ {now_ist().strftime('%H:%M:%S IST')}"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh OTP",        callback_data=f"getotp_{acc_id}", style="success")],
        [InlineKeyboardButton("🔒 Logout Bot Session", callback_data=f"logout_prompt_{acc_id}", style="danger")],
        [InlineKeyboardButton("🔙 Back",               callback_data=f"getotp_back_{acc_id}", style="primary")],
    ])
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

async def _fetch_otp(client):
    pat = re.compile(r'\b\d{4,6}\b')
    for sender in ["+42777", 777000]:
        try:
            for msg in await client.get_messages(sender, limit=5):
                if msg.text:
                    m = pat.search(msg.text)
                    if m:
                        return m.group()
        except Exception:
            continue
    return None

def _get_order_for_acc(acc_id):
    conn = get_db()
    row  = conn.execute("SELECT id FROM orders WHERE account_id=? AND status='approved' LIMIT 1", (acc_id,)).fetchone()
    conn.close()
    return row["id"] if row else 0

async def getotp_back(update, context):
    query    = update.callback_query
    await query.answer()
    acc_id   = int(query.data.split("_")[2])
    order_id = _get_order_for_acc(acc_id)
    conn  = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    acc   = conn.execute("SELECT * FROM accounts WHERE id=?", (acc_id,)).fetchone()
    conn.close()
    if not order or not acc:
        await query.edit_message_text("❌ Order not found.")
        return
    text = (
        f"📱 *Your Number Details*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📂 Category: {mesc(order['category_name'])}\n"
        f"📞 Number: `+{acc['phone_number']}`\n"
        f"🔐 2FA: `{acc['two_fa_password'] or 'Not set'}`\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📨 Get Latest OTP",     callback_data=f"getotp_{acc['id']}", style="success")],
        [InlineKeyboardButton("🔒 Logout Bot Session", callback_data=f"logout_prompt_{acc['id']}", style="danger")],
        [InlineKeyboardButton("📦 My Orders",          callback_data="my_orders_0", style="primary")],
    ])
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)


# ─── LOGOUT SESSION ───────────────────────────────────────────────────────────
async def logout_prompt(update, context):
    query  = update.callback_query
    await query.answer()
    acc_id = int(query.data.split("_")[2])
    text = (
        "🔒 *Logout Bot Session*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ This will remove the bot's authorized device from your Telegram account\\.\n\n"
        "✅ *Only proceed if you have already successfully logged into this account on your own device\\.*\n\n"
        "After logout, the bot will no longer be able to fetch OTPs for this number\\.\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔒 Yes, Logout Bot Now", callback_data=f"logout_confirm_{acc_id}", style="danger")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"reveal_{_get_order_for_acc(acc_id)}", style="primary")],
    ])
    await query.edit_message_text(text, parse_mode="MarkdownV2", reply_markup=kb)

async def logout_confirm(update, context):
    query  = update.callback_query
    await query.answer("⏳ Logging out...")
    acc_id = int(query.data.split("_")[2])
    conn = get_db()
    acc  = conn.execute("SELECT * FROM accounts WHERE id=?", (acc_id,)).fetchone()
    conn.close()
    if not acc:
        await query.edit_message_text("❌ Account not found.")
        return
    if not acc["session_string"]:
        await query.edit_message_text("ℹ️ Bot session already logged out.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📦 My Orders", callback_data="my_orders_0", style="primary")]]))
        return
    await query.edit_message_text("⏳ Connecting to logout...")
    client = TelegramClient(StringSession(acc["session_string"]), API_ID, API_HASH)
    try:
        await client.connect()
        await client.log_out()
        conn = get_db()
        conn.execute("UPDATE accounts SET session_string='' WHERE id=?", (acc_id,))
        conn.commit()
        conn.close()
        await query.edit_message_text(
            "✅ *Bot session logged out successfully\\!*\n\n"
            "The bot's authorized device has been removed from your account\\.\n"
            "Your account is now fully under your control only\\. 🔐",
            parse_mode="MarkdownV2",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📦 My Orders", callback_data="my_orders_0", style="primary")]]))
    except Exception as e:
        logger.error(f"Logout error: {e}")
        await query.edit_message_text(
            "⚠️ Could not logout. Session may have already expired.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back",
                callback_data=f"reveal_{_get_order_for_acc(acc_id)}", style="primary")]]))
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


# ─── WALLET ──────────────────────────────────────────────────────────────────
async def wallet(update, context):
    query = update.callback_query
    await query.answer()
    if await guard(update, context):
        return
    conn = get_db()
    row  = conn.execute("SELECT wallet_balance FROM users WHERE id=?", (query.from_user.id,)).fetchone()
    conn.close()
    bal       = row["wallet_balance"] if row else 0
    upi_on    = get_setting("upi_enabled",   "1") == "1"
    crypto_on = get_setting("crypto_enabled","1") == "1"
    buttons = []
    if upi_on:
        buttons.append([InlineKeyboardButton("➕ Deposit via UPI",    callback_data="deposit_upi", style="success")])
    if crypto_on:
        buttons.append([InlineKeyboardButton("🪙 Deposit via Crypto", callback_data="deposit_crypto", style="success")])
    buttons += [
        [InlineKeyboardButton("📋 Deposit History", callback_data="dep_hist_0", style="primary")],
        [InlineKeyboardButton("🔙 Main Menu",        callback_data="main_menu", style="primary")],
    ]
    await query.edit_message_text(
        f"💰 *My Wallet*\n━━━━━━━━━━━━━━━━━━━━\n💵 Balance: ₹{bal:.2f} INR\n━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

async def deposit_upi_cb(update, context):
    query = update.callback_query
    await query.answer()
    if await guard(update, context):
        return
    context.user_data["awaiting_dep_amount"] = True
    context.user_data["dep_method"]          = "upi"
    await query.edit_message_text(
        "💳 *UPI Deposit*\nEnter amount in INR:\n_\\(Minimum ₹20\\)_",
        parse_mode="MarkdownV2",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="wallet", style="danger")]]))

async def deposit_crypto_cb(update, context):
    query = update.callback_query
    await query.answer()
    if await guard(update, context):
        return
    rate = get_usdt_rate()
    context.user_data["awaiting_dep_amount"] = True
    context.user_data["dep_method"]          = "crypto"
    await query.edit_message_text(
        f"🪙 *Crypto Deposit*\nEnter amount in USD:\n_(Minimum $0\\.1  |  Rate: 1 USDT \\= ₹{rate:.0f})_",
        parse_mode="MarkdownV2",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="wallet", style="danger")]]))

async def check_dep_cb(update, context):
    query  = update.callback_query
    dep_id = int(query.data.split("_")[2])
    conn   = get_db()
    dep    = conn.execute("SELECT * FROM deposits WHERE id=?", (dep_id,)).fetchone()
    conn.close()
    if not dep:
        await query.answer("Deposit not found.", show_alert=True)
        return
    if dep["status"] == "approved":
        await query.answer("✅ Already credited!", show_alert=True)
        return
    status = await oxapay_check(dep["crypto_track_id"])
    label  = {"Waiting":"⏳ Waiting","Paid":"✅ Paid","Expired":"⌛ Expired","Failed":"❌ Failed"}.get(status,"❓")
    await query.answer(f"Status: {label}", show_alert=True)


# ─── TEXT HANDLER ─────────────────────────────────────────────────────────────
async def text_handler(update, context):
    if await guard(update, context):
        return
    user = update.effective_user

    # ── Deposit amount ──
    if context.user_data.get("awaiting_dep_amount"):
        try:
            amount = float(update.message.text.strip())
        except ValueError:
            await update.message.reply_text("❌ Invalid number. Try again:")
            return
        method = context.user_data.pop("dep_method", "upi")
        context.user_data.pop("awaiting_dep_amount")

        if method == "upi":
            if amount < 20:
                await update.message.reply_text("❌ Minimum ₹20. Enter again:")
                context.user_data["awaiting_dep_amount"] = True
                context.user_data["dep_method"] = "upi"
                return
            context.user_data["dep_inr"]                     = amount
            context.user_data["awaiting_deposit_screenshot"] = True
            qr_buf = generate_upi_qr(amount, f"Deposit")
            await update.message.reply_photo(photo=qr_buf,
                caption=f"💳 UPI Deposit\nAmount: Rs{amount:.0f}\nUPI ID: {UPI_ID}\nPay EXACT amount then send screenshot.")
            await update.message.reply_text("📸 Send your payment screenshot now:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="wallet", style="danger")]]))
        else:
            if amount < 0.1:
                await update.message.reply_text("❌ Minimum $0.1. Enter again:")
                context.user_data["awaiting_dep_amount"] = True
                context.user_data["dep_method"] = "crypto"
                return
            rate    = get_usdt_rate()
            inr_est = round(amount * rate, 2)
            invoice = await oxapay_create_invoice(amount, f"Deposit", f"dep_{user.id}_{int(time.time())}")
            if not invoice:
                await update.message.reply_text("❌ Failed to create invoice.", reply_markup=main_menu_kb())
                return
            conn   = get_db()
            dep_id = conn.execute(
                "INSERT INTO deposits (user_id,amount_inr,amount_usd,payment_method,crypto_track_id,status,created_at) VALUES (?,?,?,?,?,?,?)",
                (user.id, inr_est, amount, "crypto", invoice["trackId"], "pending", now_ist().isoformat())
            ).lastrowid
            conn.commit()
            conn.close()
            text = (
                f"🪙 *Crypto Deposit*\n"
                f"Amount: ${amount:.2f} USDT \\(~₹{inr_est:.0f}\\)\n"
                f"Rate: 1 USDT \\= ₹{rate:.0f}\n"
                f"Expires: 30 minutes \\| Auto\\-credited ✅"
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Pay Now (OxaPay)", url=invoice["payLink"], style="success")],
                [InlineKeyboardButton("🔄 Check Status", callback_data=f"chk_dep_{dep_id}", style="primary")],
            ])
            await update.message.reply_text(text, parse_mode="MarkdownV2", reply_markup=kb)
            asyncio.create_task(poll_crypto_deposit(context, invoice["trackId"], user.id, dep_id))
        return

    # ── Admin: USDT rate ──
    if context.user_data.get("awaiting_usdt_rate"):
        context.user_data.pop("awaiting_usdt_rate")
        try:
            rate = float(update.message.text.strip())
            if rate <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ Invalid. Enter a positive number e.g. 85")
            return
        set_setting("usdt_rate", str(rate))
        await update.message.reply_text(
            f"✅ USDT rate updated: 1 USDT = ₹{rate:.2f} INR",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Settings", callback_data="admin_settings", style="primary")]]))
        return

    # ── Admin: edit balance ──
    if context.user_data.get("admin_edit_balance_uid"):
        uid = context.user_data.pop("admin_edit_balance_uid")
        try:
            delta = float(update.message.text.strip())
        except ValueError:
            await update.message.reply_text("❌ Invalid amount.")
            return
        conn = get_db()
        conn.execute("UPDATE users SET wallet_balance=wallet_balance+? WHERE id=?", (delta, uid))
        conn.commit()
        row = conn.execute("SELECT wallet_balance FROM users WHERE id=?", (uid,)).fetchone()
        conn.close()
        sign = "+" if delta >= 0 else ""
        await update.message.reply_text(
            f"✅ Updated: {sign}{delta:.2f} INR\nNew balance: ₹{row['wallet_balance']:.2f}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin", callback_data="admin_menu", style="primary")]]))
        return

    # ── Admin: set price for existing category (INR only, auto USD) ──
    if context.user_data.get("admin_set_price_cat") and context.user_data.get("awaiting_price_input"):
        try:
            inr = float(update.message.text.strip())
        except ValueError:
            await update.message.reply_text("❌ Invalid. Enter INR price e.g. 500")
            return
        cat_id = context.user_data.pop("admin_set_price_cat")
        context.user_data.pop("awaiting_price_input")
        usd = inr_to_usd(inr)
        conn = get_db()
        conn.execute("UPDATE stock_categories SET price_inr=?,price_usd=? WHERE id=?", (inr, usd, cat_id))
        conn.commit()
        c = conn.execute("SELECT * FROM stock_categories WHERE id=?", (cat_id,)).fetchone()
        conn.close()
        rate = get_usdt_rate()
        await update.message.reply_text(
            f"✅ {c['name']}: ₹{inr:.0f} INR → ${usd:.2f} USDT\n(Rate used: 1 USDT = ₹{rate:.0f})",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin", callback_data="admin_menu", style="primary")]]))
        return

    # ── Admin: set price for NEW stock (after adding numbers) ──
    if context.user_data.get("new_cat_step") == "price":
        try:
            inr_price = float(update.message.text.strip())
            if inr_price <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ Invalid price. Enter a positive number (e.g. 500):")
            return

        cat_id = context.user_data.get("new_cat_id")
        if not cat_id:
            await update.message.reply_text("❌ Session expired. Please start over.")
            context.user_data.clear()
            return

        usd_price = inr_to_usd(inr_price)
        conn = get_db()
        conn.execute("UPDATE stock_categories SET price_inr=?, price_usd=? WHERE id=?", (inr_price, usd_price, cat_id))
        conn.commit()
        conn.close()

        # Clear all temporary data
        for key in ["new_cat_id", "new_cat_name", "new_cat_step", "new_cat_quantity", "new_cat_added",
                    "current_phone", "current_session", "current_2fa", "login_client"]:
            context.user_data.pop(key, None)

        rate = get_usdt_rate()
        await update.message.reply_text(
            f"🎉 *Stock Added Successfully!*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Price set: ₹{inr_price:.0f} INR → ${usd_price:.2f} USDT\n"
            f"📊 Rate used: 1 USDT = ₹{rate:.0f}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ You can now see this category in the bot's shop.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Add More Stock", callback_data="add_stock_start", style="success")],
                [InlineKeyboardButton("🔙 Admin Menu", callback_data="admin_menu", style="primary")],
            ])
        )
        return

    # ── Admin: new category name ──
    if context.user_data.get("awaiting_new_category"):
        context.user_data.pop("awaiting_new_category")
        cat_name = update.message.text.strip()
        conn     = get_db()
        existing = conn.execute("SELECT id FROM stock_categories WHERE name=?", (cat_name,)).fetchone()
        if not existing:
            cat_id = conn.execute("INSERT INTO stock_categories (name) VALUES (?)", (cat_name,)).lastrowid
            conn.commit()
        else:
            cat_id = existing["id"]
        conn.close()
        context.user_data["new_cat_id"]    = cat_id
        context.user_data["new_cat_name"]  = cat_name
        context.user_data["new_cat_step"]  = "quantity"
        context.user_data["new_cat_added"] = 0
        await update.message.reply_text(
            f"✅ Category: *{mesc(cat_name)}*\n\n📦 How many numbers to add? (e.g. 5)",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_stock", style="danger")]]))
        return

    # ── Admin: quantity ──
    if context.user_data.get("new_cat_step") == "quantity":
        try:
            qty = int(update.message.text.strip())
            if qty < 1:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ Enter a valid number (e.g. 5):")
            return
        context.user_data["new_cat_quantity"] = qty
        context.user_data["new_cat_step"]     = "phone"
        await update.message.reply_text(
            f"📞 *Account 1/{qty}*\n\nSend phone number:\nExample: `+91XXXXXXXXXX`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_stock", style="danger")]]))
        return

    # ── Admin: phone number ── (start live login)
    if context.user_data.get("new_cat_step") == "phone":
        phone = update.message.text.strip()
        if not phone.startswith("+"):
            await update.message.reply_text("❌ Phone must start with + e.g. +91XXXXXXXXXX")
            return
        context.user_data["current_phone"] = phone.lstrip("+")
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        try:
            await client.connect()
            result = await client.send_code_request(phone)
            context.user_data["login_client"] = client
            context.user_data["login_phone"] = phone
            context.user_data["login_phone_code_hash"] = result.phone_code_hash
            context.user_data["new_cat_step"] = "login_await_otp"
            await update.message.reply_text(
                "📲 A login code has been sent to the Telegram account.\n"
                "Enter the OTP (you may use spaces, e.g. `1 2 3 4 5`):",
                parse_mode="Markdown"
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Failed to send code: {e}")
            if client:
                await client.disconnect()
            context.user_data["new_cat_step"] = "phone"
        return

    # ── Admin: OTP verification ──
    if context.user_data.get("new_cat_step") == "login_await_otp":
        client = context.user_data.get("login_client")
        phone = context.user_data.get("login_phone")
        code_hash = context.user_data.get("login_phone_code_hash")
        if not client:
            await update.message.reply_text("❌ Session expired. Please start over.")
            context.user_data.clear()
            return
        code = update.message.text.strip().replace(" ", "")
        if not code.isdigit():
            await update.message.reply_text("❌ Invalid OTP. Enter only digits (spaces allowed).")
            return
        try:
            await client.sign_in(phone, code, phone_code_hash=code_hash)
            session_str = client.session.save()
            context.user_data["current_session"] = session_str
            await client.disconnect()
            context.user_data.pop("login_client", None)
            context.user_data.pop("login_phone", None)
            context.user_data.pop("login_phone_code_hash", None)
            context.user_data["new_cat_step"] = "twofa"
            await update.message.reply_text(
                "✅ Login successful!\n\n"
                "🔐 Send the **2FA password** for this account (or type `/skip`):",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏭ Skip", callback_data="skip_2fa_stock")]])
            )
        except SessionPasswordNeededError:
            context.user_data["new_cat_step"] = "login_await_password"
            await update.message.reply_text(
                "🔐 This account has 2‑step verification enabled.\n"
                "Please enter your password:"
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Login failed: {e}")
            await client.disconnect()
            context.user_data.pop("login_client", None)
            context.user_data["new_cat_step"] = "phone"
        return

    # ── Admin: 2‑step password verification ──
    if context.user_data.get("new_cat_step") == "login_await_password":
        client = context.user_data.get("login_client")
        if not client:
            await update.message.reply_text("❌ Session expired. Start over.")
            context.user_data.clear()
            return
        password = update.message.text.strip()
        try:
            await client.sign_in(password=password)
            session_str = client.session.save()
            context.user_data["current_session"] = session_str
            await client.disconnect()
            context.user_data.pop("login_client", None)
            context.user_data.pop("login_phone", None)
            context.user_data.pop("login_phone_code_hash", None)
            context.user_data["new_cat_step"] = "twofa"
            await update.message.reply_text(
                "✅ Login successful!\n\n"
                "🔐 Send the **2FA password** for this account (or type `/skip`):",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏭ Skip", callback_data="skip_2fa_stock")]])
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Wrong password or error: {e}")
        return

    # ── Admin: 2FA password for account (stored in DB) ──
    if context.user_data.get("new_cat_step") == "twofa":
        text = update.message.text.strip()
        if text.lower() == "/skip":
            context.user_data["current_2fa"] = None
        else:
            context.user_data["current_2fa"] = text
        await _save_account_and_continue(update, context)
        return

    # ── Admin: remove account ──
    if context.user_data.get("awaiting_remove_acc"):
        context.user_data.pop("awaiting_remove_acc")
        val  = update.message.text.strip()
        conn = get_db()
        acc  = (conn.execute("SELECT * FROM accounts WHERE phone_number=?", (val.lstrip("+"),)).fetchone()
                if val.startswith("+") else
                conn.execute("SELECT * FROM accounts WHERE id=?",
                             (int(val) if val.isdigit() else -1,)).fetchone())
        conn.close()
        if not acc:
            await update.message.reply_text("❌ Account not found.")
            return
        cat = get_cat(acc["category_id"])
        kb  = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑️ Confirm Delete", callback_data=f"confirm_del_{acc['id']}", style="danger"),
             InlineKeyboardButton("❌ Cancel",          callback_data="admin_stock", style="primary")],
        ])
        await update.message.reply_text(
            f"Account #{acc['id']}\n📂 {cat['name'] if cat else '?'}\n"
            f"📞 +{acc['phone_number']}\nSold: {'Yes' if acc['is_sold'] else 'No'}",
            reply_markup=kb)
        return

    # ── Admin: broadcast ──
    if context.user_data.get("awaiting_broadcast"):
        context.user_data.pop("awaiting_broadcast")
        conn  = get_db()
        total = conn.execute("SELECT COUNT(*) as c FROM users WHERE is_banned=0").fetchone()["c"]
        conn.close()
        context.user_data["broadcast_msg_id"]  = update.message.message_id
        context.user_data["broadcast_chat_id"] = update.message.chat_id
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"✅ Send to {total} users", callback_data="broadcast_confirm", style="success"),
            InlineKeyboardButton("❌ Cancel", callback_data="admin_menu", style="danger"),
        ]])
        await update.message.reply_text(f"📢 Send to {total} users?", reply_markup=kb)
        return

    # ── Admin: search user ──
    if context.user_data.get("awaiting_search_user"):
        context.user_data.pop("awaiting_search_user")
        val  = update.message.text.strip().lstrip("@")
        conn = get_db()
        row  = (conn.execute("SELECT * FROM users WHERE id=?", (int(val),)).fetchone()
                if val.isdigit() else
                conn.execute("SELECT * FROM users WHERE username=?", (val,)).fetchone())
        conn.close()
        if not row:
            await update.message.reply_text("❌ User not found.")
            return
        await _show_user_profile(update, context, dict(row), via_message=True)
        return

    # ── Admin: welcome message ──
    if context.user_data.get("awaiting_welcome_msg"):
        context.user_data.pop("awaiting_welcome_msg")
        set_setting("welcome_message", update.message.text)
        await update.message.reply_text("✅ Welcome message updated!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin", callback_data="admin_menu", style="primary")]]))
        return


async def _save_account_and_continue(update, context):
    """Save the current account and continue adding more or go to price."""
    cat_id  = context.user_data.get("new_cat_id")
    if not cat_id:
        await update.message.reply_text("❌ Error: Category not found. Start over.")
        return

    phone   = context.user_data.pop("current_phone", None)
    session = context.user_data.pop("current_session", None)
    twofa   = context.user_data.pop("current_2fa", None)

    if not phone or not session:
        await update.message.reply_text("❌ Missing phone or session. Please restart stock addition.")
        context.user_data.clear()
        return

    conn = get_db()
    cat  = conn.execute("SELECT * FROM stock_categories WHERE id=?", (cat_id,)).fetchone()
    if not cat:
        conn.close()
        await update.message.reply_text("❌ Category not found in database.")
        return

    conn.execute(
        "INSERT INTO accounts (category_id,category_name,phone_number,session_string,two_fa_password,added_by,added_at) VALUES (?,?,?,?,?,?,?)",
        (cat_id, cat["name"], phone, session, twofa, update.effective_user.id, now_ist().isoformat())
    )
    conn.commit()
    conn.close()

    added = context.user_data.get("new_cat_added", 0) + 1
    context.user_data["new_cat_added"] = added
    qty   = context.user_data.get("new_cat_quantity", 0)

    msg = update.message or (update.callback_query.message if update.callback_query else None)
    if not msg:
        return

    if added < qty:
        context.user_data["new_cat_step"] = "phone"
        await msg.reply_text(
            f"✅ Account {added}/{qty} saved!\n\n"
            f"📞 *Account {added+1}/{qty}*\nSend phone number:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_stock", style="danger")]]))
    else:
        context.user_data["new_cat_step"] = "price"
        rate = get_usdt_rate()
        cat_name_escaped = mesc(cat["name"])
        await msg.reply_text(
            f"✅ All {qty} accounts saved\\!\n\n"
            f"💰 Set price for *{cat_name_escaped}*\n"
            f"Enter *INR price only* — USDT auto\\-calculated\\.\n"
            f"Rate: 1 USDT \\= ₹{rate:.0f}\n"
            f"Example: `500`",
            parse_mode="MarkdownV2",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_stock", style="danger")]]))


async def skip_2fa_stock(update, context):
    query = update.callback_query
    await query.answer()
    if not context.user_data.get("current_phone") or not context.user_data.get("current_session"):
        await query.edit_message_text("❌ Session expired. Please start over.")
        context.user_data.clear()
        return
    context.user_data["current_2fa"] = None
    context.user_data["new_cat_step"] = "twofa"
    await _save_account_and_continue(update, context)


# ─── ADMIN GROUP APPROVALS ────────────────────────────────────────────────────
async def approve_order(update, context):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("❌ Not authorized.", show_alert=True)
        return
    await query.answer()
    order_id = int(query.data.split("_")[2])
    conn  = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not order or order["status"] != "pending":
        conn.close()
        return
    acc = conn.execute("SELECT * FROM accounts WHERE category_id=? AND is_sold=0 LIMIT 1",
                       (order["category_id"],)).fetchone()
    if not acc:
        conn.close()
        await query.answer("❌ No stock available!", show_alert=True)
        return
    now = now_ist().isoformat()
    conn.execute("UPDATE accounts SET is_sold=1,sold_to=?,sold_at=? WHERE id=?", (order["user_id"], now, acc["id"]))
    conn.execute("UPDATE orders SET status='approved',account_id=?,reviewed_by=?,reviewed_at=? WHERE id=?",
                 (acc["id"], query.from_user.id, now, order_id))
    conn.execute("UPDATE users SET total_purchases=total_purchases+1 WHERE id=?", (order["user_id"],))
    conn.commit()
    conn.close()
    await send_purchase_log(context.bot, order["category_name"], order["amount_inr"],
                            acc["phone_number"], order["username"], order["user_id"])
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📱 Reveal My Number", callback_data=f"reveal_{order_id}", style="success")]])
    try:
        await context.bot.send_message(chat_id=order["user_id"],
            text=f"✅ Order #{order_id} approved! Tap below to reveal your number.", reply_markup=kb)
    except Exception:
        pass
    try:
        new_text = (query.message.caption or query.message.text or "") + \
                   f"\n✅ Approved by @{query.from_user.username or query.from_user.id}"
        if query.message.photo:
            await query.message.edit_caption(caption=new_text)
        else:
            await query.message.edit_text(text=new_text)
    except Exception:
        pass

async def reject_order(update, context):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("❌ Not authorized.", show_alert=True)
        return
    await query.answer()
    order_id = int(query.data.split("_")[2])
    conn  = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not order or order["status"] != "pending":
        conn.close()
        return
    conn.execute("UPDATE orders SET status='rejected',reviewed_by=?,reviewed_at=? WHERE id=?",
                 (query.from_user.id, now_ist().isoformat(), order_id))
    conn.commit()
    conn.close()
    try:
        await context.bot.send_message(chat_id=order["user_id"],
            text=f"❌ Order #{order_id} rejected.", reply_markup=main_menu_kb())
    except Exception:
        pass
    try:
        new_text = (query.message.caption or query.message.text or "") + \
                   f"\n❌ Rejected by @{query.from_user.username or query.from_user.id}"
        if query.message.photo:
            await query.message.edit_caption(caption=new_text)
        else:
            await query.message.edit_text(text=new_text)
    except Exception:
        pass

async def approve_deposit(update, context):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("❌ Not authorized.", show_alert=True)
        return
    await query.answer()
    dep_id = int(query.data.split("_")[2])
    conn = get_db()
    dep  = conn.execute("SELECT * FROM deposits WHERE id=?", (dep_id,)).fetchone()
    if not dep or dep["status"] != "pending":
        conn.close()
        return
    conn.execute("UPDATE deposits SET status='approved',reviewed_by=?,reviewed_at=? WHERE id=?",
                 (query.from_user.id, now_ist().isoformat(), dep_id))
    conn.execute("UPDATE users SET wallet_balance=wallet_balance+? WHERE id=?",
                 (dep["amount_inr"], dep["user_id"]))
    conn.commit()
    conn.close()
    try:
        await context.bot.send_message(chat_id=dep["user_id"],
            text=f"✅ Deposit of ₹{dep['amount_inr']:.0f} credited!", reply_markup=main_menu_kb())
    except Exception:
        pass
    try:
        new_text = (query.message.caption or query.message.text or "") + \
                   f"\n✅ Approved by @{query.from_user.username or query.from_user.id}"
        if query.message.photo:
            await query.message.edit_caption(caption=new_text)
        else:
            await query.message.edit_text(text=new_text)
    except Exception:
        pass

async def reject_deposit(update, context):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("❌ Not authorized.", show_alert=True)
        return
    await query.answer()
    dep_id = int(query.data.split("_")[2])
    conn = get_db()
    dep  = conn.execute("SELECT * FROM deposits WHERE id=?", (dep_id,)).fetchone()
    if not dep or dep["status"] != "pending":
        conn.close()
        return
    conn.execute("UPDATE deposits SET status='rejected',reviewed_by=?,reviewed_at=? WHERE id=?",
                 (query.from_user.id, now_ist().isoformat(), dep_id))
    conn.commit()
    conn.close()
    try:
        await context.bot.send_message(chat_id=dep["user_id"], text=f"❌ Deposit #{dep_id} rejected.")
    except Exception:
        pass
    try:
        new_text = (query.message.caption or query.message.text or "") + \
                   f"\n❌ Rejected by @{query.from_user.username or query.from_user.id}"
        if query.message.photo:
            await query.message.edit_caption(caption=new_text)
        else:
            await query.message.edit_text(text=new_text)
    except Exception:
        pass


# ─── MY ORDERS ────────────────────────────────────────────────────────────────
async def my_orders(update, context):
    query = update.callback_query
    await query.answer()
    if await guard(update, context):
        return
    page    = int(query.data.split("_")[2])
    user_id = query.from_user.id
    conn    = get_db()
    orders  = conn.execute("SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC", (user_id,)).fetchall()
    conn.close()
    if not orders:
        await query.edit_message_text("📦 No orders yet.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu", style="primary")]]))
        return
    per_page = 5
    total    = len(orders)
    pages    = max(1, (total + per_page - 1) // per_page)
    page     = max(0, min(page, pages - 1))
    chunk    = orders[page * per_page:(page + 1) * per_page]
    buttons  = []
    for o in chunk:
        label = f"#{o['id']} | {o['category_name']} | ₹{o['amount_inr']:.0f} | {status_emoji(o['status'])}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"order_detail_{o['id']}", style="primary")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"my_orders_{page-1}", style="primary"))
        nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data="noop", style="primary"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"my_orders_{page+1}", style="primary"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu", style="primary")])
    await query.edit_message_text("📦 *My Orders*", parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup(buttons))

async def order_detail(update, context):
    query    = update.callback_query
    await query.answer()
    order_id = int(query.data.split("_")[2])
    user_id  = query.from_user.id
    conn = get_db()
    o    = conn.execute("SELECT * FROM orders WHERE id=? AND user_id=?", (order_id, user_id)).fetchone()
    conn.close()
    if not o:
        await query.edit_message_text("❌ Order not found.")
        return
    text = (
        f"📦 *Order #{o['id']}*\n━━━━━━━━━━━━━━━━━━━━\n"
        f"📂 {mesc(o['category_name'])}\n"
        f"💰 ₹{o['amount_inr']:.0f} INR\n"
        f"💳 {o['payment_method'].upper()}\n"
        f"📊 {status_emoji(o['status'])} {o['status'].title()}\n"
        f"📅 {fmt_time(o['created_at'])}\n━━━━━━━━━━━━━━━━━━━━"
    )
    buttons = []
    if o["status"] == "approved" and o["account_id"]:
        buttons.append([InlineKeyboardButton("📱 Reveal Number", callback_data=f"reveal_{o['id']}", style="success")])
    buttons.append([InlineKeyboardButton("🔙 My Orders", callback_data="my_orders_0", style="primary")])
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))


# ─── DEPOSIT HISTORY ──────────────────────────────────────────────────────────
async def dep_hist(update, context):
    query = update.callback_query
    await query.answer()
    if await guard(update, context):
        return
    page    = int(query.data.split("_")[2])
    user_id = query.from_user.id
    conn    = get_db()
    deps    = conn.execute("SELECT * FROM deposits WHERE user_id=? ORDER BY created_at DESC", (user_id,)).fetchall()
    conn.close()
    if not deps:
        await query.edit_message_text("No deposits yet.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Wallet", callback_data="wallet", style="primary")]]))
        return
    per_page = 5
    total    = len(deps)
    pages    = max(1, (total + per_page - 1) // per_page)
    page     = max(0, min(page, pages - 1))
    chunk    = deps[page * per_page:(page + 1) * per_page]
    lines    = ["📋 *Deposit History*"]
    for d in chunk:
        lines.append(f"#{d['id']} | {d['payment_method'].upper()} | ₹{d['amount_inr']:.0f} | {status_emoji(d['status'])} | {fmt_time(d['created_at'])[:11]}")
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"dep_hist_{page-1}", style="primary"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"dep_hist_{page+1}", style="primary"))
    buttons = [nav] if nav else []
    buttons.append([InlineKeyboardButton("🔙 Wallet", callback_data="wallet", style="primary")])
    await query.edit_message_text("\n".join(lines), parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup(buttons))


# ─── HELP ─────────────────────────────────────────────────────────────────────
async def help_cb(update, context):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "❓ *How to Buy Numbers*\n━━━━━━━━━━━━━━━━━━━━\n"
        "1️⃣ Browse categories\n2️⃣ Choose payment method\n"
        "3️⃣ Upload screenshot / pay crypto\n4️⃣ Wait for confirmation\n"
        "5️⃣ Reveal number & get OTP\n━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Support", url="https://t.me/support", style="primary")],
            [InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu", style="primary")],
        ]))

async def main_menu_cb(update, context):
    query = update.callback_query
    await query.answer()
    if await guard(update, context):
        return
    msg = get_setting("welcome_message", "🏪 Welcome to NumberStore!")
    await query.edit_message_text(msg, reply_markup=main_menu_kb())


# ─── ADMIN PANEL ──────────────────────────────────────────────────────────────
async def admin_cmd(update, context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Not authorized.")
        return
    await update.message.reply_text("🔧 *Admin Panel*", parse_mode="Markdown",
                                    reply_markup=admin_main_kb())

def admin_main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Stock",     callback_data="admin_stock", style="primary"),
         InlineKeyboardButton("💰 Orders",    callback_data="admin_orders_all_0", style="primary"),
         InlineKeyboardButton("💳 Deposits",  callback_data="admin_deps_all_0", style="primary")],
        [InlineKeyboardButton("👥 Users",     callback_data="admin_users", style="primary"),
         InlineKeyboardButton("📊 Stats",     callback_data="admin_stats", style="primary"),
         InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast", style="primary")],
        [InlineKeyboardButton("📡 Channels",  callback_data="admin_channels", style="primary"),
         InlineKeyboardButton("⚙️ Settings",  callback_data="admin_settings", style="primary"),
         InlineKeyboardButton("❌ Close",      callback_data="admin_close", style="primary")],
    ])

async def admin_menu_cb(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    await query.edit_message_text("🔧 *Admin Panel*", parse_mode="Markdown",
                                  reply_markup=admin_main_kb())

async def admin_close(update, context):
    query = update.callback_query
    await query.answer()
    await query.message.delete()


# ─── ADMIN: CHANNELS ─────────────────────────────────────────────────────────
async def admin_channels(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    channels = get_force_channels()
    lines   = ["📡 *Force Subscribe Channels*\n━━━━━━━━━━━━━━━━━━━━"]
    buttons = []
    if channels:
        for ch in channels:
            lines.append(f"• {ch['channel_name']} | {ch['channel_id']}")
            buttons.append([InlineKeyboardButton(f"🗑️ Remove {ch['channel_name']}",
                                                  callback_data=f"del_channel_{ch['id']}", style="danger")])
    else:
        lines.append("_No channels added yet._")
    lines.append("\nUse /addchannel to add")
    buttons.append([InlineKeyboardButton("🔙 Admin Menu", callback_data="admin_menu", style="primary")])
    await query.edit_message_text("\n".join(lines), parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup(buttons))

async def del_channel_cb(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    ch_row_id = int(query.data.split("_")[2])
    conn = get_db()
    conn.execute("DELETE FROM force_channels WHERE id=?", (ch_row_id,))
    conn.commit()
    conn.close()
    await query.answer("✅ Channel removed!", show_alert=True)
    await admin_channels(update, context)


# ─── ADMIN: STOCK ─────────────────────────────────────────────────────────────
async def admin_stock(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    client = context.user_data.get("login_client")
    if client:
        try:
            await client.disconnect()
        except:
            pass
        context.user_data.pop("login_client", None)
    conn  = get_db()
    cats  = conn.execute("SELECT * FROM stock_categories ORDER BY name").fetchall()
    conn.close()
    buttons = [[InlineKeyboardButton("➕ Add New Stock", callback_data="add_stock_start", style="success")]]
    for c in cats:
        stock = get_stock_count(c["id"])
        conn2 = get_db()
        total = conn2.execute("SELECT COUNT(*) as cnt FROM accounts WHERE category_id=?",
                              (c["id"],)).fetchone()["cnt"]
        conn2.close()
        icon = "✅" if c["enabled"] else "❌"
        buttons.append([InlineKeyboardButton(
            f"{icon} {c['name']}  📦{stock}/{total}  ₹{c['price_inr']:.0f}",
            callback_data=f"stock_cat_{c['id']}", style="primary")])
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="admin_menu", style="primary")])
    await query.edit_message_text("📦 *Stock Manager*", parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup(buttons))

async def stock_cat_detail(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    cat_id = int(query.data.split("_")[2])
    c      = get_cat(cat_id)
    if not c:
        return
    stock = get_stock_count(cat_id)
    conn  = get_db()
    total = conn.execute("SELECT COUNT(*) as cnt FROM accounts WHERE category_id=?",
                         (cat_id,)).fetchone()["cnt"]
    conn.close()
    text = (
        f"📂 *{mesc(c['name'])}*\n"
        f"💰 ₹{c['price_inr']:.0f} INR | ${c['price_usd']:.2f} USDT\n"
        f"📦 Available: {stock} | Total: {total}\n"
        f"Status: {'✅ Enabled' if c['enabled'] else '❌ Disabled'}"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Set Price",        callback_data=f"setprice_cat_{cat_id}", style="primary"),
         InlineKeyboardButton("🔛 Toggle",            callback_data=f"toggle_cat_{cat_id}", style="primary")],
        [InlineKeyboardButton("➕ Add More Numbers",  callback_data=f"addmore_cat_{cat_id}", style="success"),
         InlineKeyboardButton("🗑️ Delete Category",  callback_data=f"del_cat_{cat_id}", style="danger")],
        [InlineKeyboardButton("🔙 Back",              callback_data="admin_stock", style="primary")],
    ])
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

async def add_stock_start(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    client = context.user_data.get("login_client")
    if client:
        try:
            await client.disconnect()
        except:
            pass
        context.user_data.pop("login_client", None)
    for k in ["new_cat_id","new_cat_name","new_cat_step","new_cat_quantity","new_cat_added",
              "current_phone","current_session","current_2fa"]:
        context.user_data.pop(k, None)
    context.user_data["awaiting_new_category"] = True
    await query.edit_message_text(
        "📦 *Add New Stock*\n━━━━━━━━━━━━━━━━━━━━\n"
        "Enter the *stock name/category*:\n\n"
        "Examples:\n• `India 2022 Gmail`\n• `USA Facebook Old`\n• `UK Telegram Fresh`",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_stock", style="danger")]]))

async def addmore_cat(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    cat_id = int(query.data.split("_")[2])
    c = get_cat(cat_id)
    context.user_data["new_cat_id"]    = cat_id
    context.user_data["new_cat_name"]  = c["name"]
    context.user_data["new_cat_step"]  = "quantity"
    context.user_data["new_cat_added"] = 0
    await query.edit_message_text(
        f"➕ Adding more to *{mesc(c['name'])}*\n\nHow many numbers?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_stock", style="danger")]]))

async def setprice_cat(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    cat_id = int(query.data.split("_")[2])
    c = get_cat(cat_id)
    rate = get_usdt_rate()
    context.user_data["admin_set_price_cat"]  = cat_id
    context.user_data["awaiting_price_input"] = True
    await query.edit_message_text(
        f"✏️ Set price for *{mesc(c['name'])}*\n\n"
        f"Enter *INR price only* — USDT auto\\-calculated\n"
        f"Rate: 1 USDT \\= ₹{rate:.0f}\n"
        f"Example: `500`",
        parse_mode="MarkdownV2")

async def toggle_cat(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    cat_id = int(query.data.split("_")[2])
    conn   = get_db()
    row    = conn.execute("SELECT enabled FROM stock_categories WHERE id=?", (cat_id,)).fetchone()
    new_v  = 0 if row["enabled"] else 1
    conn.execute("UPDATE stock_categories SET enabled=? WHERE id=?", (new_v, cat_id))
    conn.commit()
    conn.close()
    await query.answer("✅ Enabled" if new_v else "❌ Disabled", show_alert=True)
    c = get_cat(cat_id)
    stock = get_stock_count(cat_id)
    conn2 = get_db()
    total = conn2.execute("SELECT COUNT(*) as cnt FROM accounts WHERE category_id=?",
                          (cat_id,)).fetchone()["cnt"]
    conn2.close()
    text = (
        f"📂 *{mesc(c['name'])}*\n"
        f"💰 ₹{c['price_inr']:.0f} INR | ${c['price_usd']:.2f} USDT\n"
        f"📦 Available: {stock} | Total: {total}\n"
        f"Status: {'✅ Enabled' if c['enabled'] else '❌ Disabled'}"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Set Price",        callback_data=f"setprice_cat_{cat_id}", style="primary"),
         InlineKeyboardButton("🔛 Toggle",            callback_data=f"toggle_cat_{cat_id}", style="primary")],
        [InlineKeyboardButton("➕ Add More Numbers",  callback_data=f"addmore_cat_{cat_id}", style="success"),
         InlineKeyboardButton("🗑️ Delete Category",  callback_data=f"del_cat_{cat_id}", style="danger")],
        [InlineKeyboardButton("🔙 Back",              callback_data="admin_stock", style="primary")],
    ])
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

async def del_cat(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    cat_id = int(query.data.split("_")[2])
    conn   = get_db()
    conn.execute("DELETE FROM stock_categories WHERE id=?", (cat_id,))
    conn.execute("DELETE FROM accounts WHERE category_id=? AND is_sold=0", (cat_id,))
    conn.commit()
    cats  = conn.execute("SELECT * FROM stock_categories ORDER BY name").fetchall()
    conn.close()
    await query.answer("🗑️ Category deleted!", show_alert=True)
    buttons = [[InlineKeyboardButton("➕ Add New Stock", callback_data="add_stock_start", style="success")]]
    for c in cats:
        stock = get_stock_count(c["id"])
        conn2 = get_db()
        total = conn2.execute("SELECT COUNT(*) as cnt FROM accounts WHERE category_id=?",
                              (c["id"],)).fetchone()["cnt"]
        conn2.close()
        icon = "✅" if c["enabled"] else "❌"
        buttons.append([InlineKeyboardButton(
            f"{icon} {c['name']}  📦{stock}/{total}  ₹{c['price_inr']:.0f}",
            callback_data=f"stock_cat_{c['id']}", style="primary")])
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="admin_menu", style="primary")])
    await query.edit_message_text("📦 *Stock Manager*", parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup(buttons))

async def remove_acc_cb(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    context.user_data["awaiting_remove_acc"] = True
    await query.edit_message_text("Send account ID or phone number (+XXXXXXXXXXX) to remove:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin_stock", style="danger")]]))

async def confirm_del(update, context):
    query  = update.callback_query
    await query.answer()
    acc_id = int(query.data.split("_")[2])
    conn   = get_db()
    conn.execute("DELETE FROM accounts WHERE id=?", (acc_id,))
    conn.commit()
    conn.close()
    await query.edit_message_text("✅ Account deleted.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Stock", callback_data="admin_stock", style="primary")]]))


# ─── ADMIN: ORDERS ────────────────────────────────────────────────────────────
async def admin_orders(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    parts  = query.data.split("_")
    sf     = parts[2]
    page   = int(parts[3])
    conn   = get_db()
    orders = (conn.execute("SELECT * FROM orders ORDER BY created_at DESC").fetchall() if sf == "all"
              else conn.execute("SELECT * FROM orders WHERE status=? ORDER BY created_at DESC", (sf,)).fetchall())
    conn.close()
    filter_btns = [
        InlineKeyboardButton("⏳ Pending",  callback_data="admin_orders_pending_0", style="primary"),
        InlineKeyboardButton("✅ Approved", callback_data="admin_orders_approved_0", style="primary"),
        InlineKeyboardButton("❌ Rejected", callback_data="admin_orders_rejected_0", style="primary"),
    ]
    per_page = 5
    total    = len(orders)
    pages    = max(1, (total + per_page - 1) // per_page)
    page     = max(0, min(page, pages - 1))
    chunk    = orders[page * per_page:(page + 1) * per_page]
    buttons  = [filter_btns]
    for o in chunk:
        buttons.append([InlineKeyboardButton(
            f"#{o['id']} {o['category_name']} ₹{o['amount_inr']:.0f} {status_emoji(o['status'])}",
            callback_data=f"admin_order_view_{o['id']}", style="primary")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"admin_orders_{sf}_{page-1}", style="primary"))
        nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data="noop", style="primary"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"admin_orders_{sf}_{page+1}", style="primary"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔙 Admin", callback_data="admin_menu", style="primary")])
    await query.edit_message_text(f"💰 *Orders ({sf.title()})*", parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup(buttons))

async def admin_order_view(update, context):
    query    = update.callback_query
    await query.answer()
    order_id = int(query.data.split("_")[3])
    conn = get_db()
    o    = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    conn.close()
    if not o:
        await query.edit_message_text("Order not found.")
        return
    text = (
        f"📦 *Order #{o['id']}*\n"
        f"👤 @{o['username'] or 'N/A'} (ID: {o['user_id']})\n"
        f"📂 {mesc(o['category_name'])}\n"
        f"💰 ₹{o['amount_inr']:.0f} INR | {o['payment_method'].upper()}\n"
        f"📊 {status_emoji(o['status'])} {o['status'].title()}\n"
        f"📅 {fmt_time(o['created_at'])}"
    )
    buttons = []
    if o["status"] == "pending":
        buttons.append([
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_order_{order_id}", style="success"),
            InlineKeyboardButton("❌ Reject",  callback_data=f"reject_order_{order_id}", style="danger"),
        ])
    buttons.append([InlineKeyboardButton("🔙 Orders", callback_data="admin_orders_all_0", style="primary")])
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))


# ─── ADMIN: USERS ─────────────────────────────────────────────────────────────
async def admin_users(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Search",       callback_data="admin_search_user", style="primary"),
         InlineKeyboardButton("🚫 Ban",           callback_data="admin_ban_user", style="danger"),
         InlineKeyboardButton("✅ Unban",          callback_data="admin_unban_user", style="success")],
        [InlineKeyboardButton("💰 Edit Wallet",   callback_data="admin_edit_wallet", style="primary")],
        [InlineKeyboardButton("🔙 Back",          callback_data="admin_menu", style="primary")],
    ])
    await query.edit_message_text("👥 *Users Manager*", parse_mode="Markdown", reply_markup=kb)

async def admin_search_user(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    context.user_data["awaiting_search_user"] = True
    await query.edit_message_text("Enter user ID or @username:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin_users", style="danger")]]))

async def admin_ban_user(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    context.user_data["awaiting_search_user"] = True
    context.user_data["ban_action"] = "ban"
    await query.edit_message_text("Enter user ID or @username to ban:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin_users", style="danger")]]))

async def admin_unban_user(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    context.user_data["awaiting_search_user"] = True
    context.user_data["ban_action"] = "unban"
    await query.edit_message_text("Enter user ID or @username to unban:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin_users", style="danger")]]))

async def admin_edit_wallet(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    context.user_data["awaiting_search_user"] = True
    context.user_data["wallet_action"] = True
    await query.edit_message_text("Enter user ID or @username to edit wallet:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin_users", style="danger")]]))

async def _show_user_profile(update, context, row, via_message=False):
    text = (
        f"👤 *{mesc(row['first_name'])}* (@{mesc(row['username'])})\n"
        f"ID: `{row['id']}`\n"
        f"💰 Wallet: ₹{row['wallet_balance']:.2f} INR\n"
        f"🛒 Purchases: {row['total_purchases']}\n"
        f"🚫 Banned: {'Yes' if row['is_banned'] else 'No'}\n"
        f"📅 Joined: {fmt_time(row['joined_at'])}"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚫 Ban",          callback_data=f"ban_uid_{row['id']}", style="danger"),
         InlineKeyboardButton("✅ Unban",         callback_data=f"unban_uid_{row['id']}", style="success")],
        [InlineKeyboardButton("💰 Edit Balance", callback_data=f"editbal_uid_{row['id']}", style="primary")],
        [InlineKeyboardButton("🔙 Back",         callback_data="admin_users", style="primary")],
    ])
    if via_message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

async def ban_uid(update, context):
    query = update.callback_query
    await query.answer()
    uid  = int(query.data.split("_")[2])
    conn = get_db()
    conn.execute("UPDATE users SET is_banned=1 WHERE id=?", (uid,))
    conn.commit()
    conn.close()
    await query.answer("🚫 User banned!", show_alert=True)

async def unban_uid(update, context):
    query = update.callback_query
    await query.answer()
    uid  = int(query.data.split("_")[2])
    conn = get_db()
    conn.execute("UPDATE users SET is_banned=0 WHERE id=?", (uid,))
    conn.commit()
    conn.close()
    await query.answer("✅ User unbanned!", show_alert=True)

async def editbal_uid(update, context):
    query = update.callback_query
    await query.answer()
    uid  = int(query.data.split("_")[2])
    context.user_data["admin_edit_balance_uid"] = uid
    await query.edit_message_text("Enter amount to add/deduct (e.g. 500 or -200):",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin_users", style="danger")]]))


# ─── ADMIN: DEPOSITS ──────────────────────────────────────────────────────────
async def admin_deps(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    parts = query.data.split("_")
    sf    = parts[2]
    page  = int(parts[3])
    conn  = get_db()
    deps  = (conn.execute("SELECT * FROM deposits ORDER BY created_at DESC").fetchall() if sf == "all"
             else conn.execute("SELECT * FROM deposits WHERE status=? ORDER BY created_at DESC", (sf,)).fetchall())
    conn.close()
    filter_btns = [
        InlineKeyboardButton("⏳ Pending",  callback_data="admin_deps_pending_0", style="primary"),
        InlineKeyboardButton("✅ Approved", callback_data="admin_deps_approved_0", style="primary"),
        InlineKeyboardButton("❌ Rejected", callback_data="admin_deps_rejected_0", style="primary"),
    ]
    per_page = 5
    total    = len(deps)
    pages    = max(1, (total + per_page - 1) // per_page)
    page     = max(0, min(page, pages - 1))
    chunk    = deps[page * per_page:(page + 1) * per_page]
    buttons  = [filter_btns]
    for d in chunk:
        buttons.append([InlineKeyboardButton(
            f"#{d['id']} uid:{d['user_id']} ₹{d['amount_inr']:.0f} {status_emoji(d['status'])}",
            callback_data=f"admin_dep_view_{d['id']}", style="primary")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"admin_deps_{sf}_{page-1}", style="primary"))
        nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data="noop", style="primary"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"admin_deps_{sf}_{page+1}", style="primary"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔙 Admin", callback_data="admin_menu", style="primary")])
    await query.edit_message_text(f"💳 *Deposits ({sf.title()})*", parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup(buttons))

async def admin_dep_view(update, context):
    query  = update.callback_query
    await query.answer()
    dep_id = int(query.data.split("_")[3])
    conn   = get_db()
    d      = conn.execute("SELECT * FROM deposits WHERE id=?", (dep_id,)).fetchone()
    conn.close()
    if not d:
        await query.edit_message_text("Deposit not found.")
        return
    text = (
        f"💳 *Deposit #{d['id']}*\n"
        f"👤 User ID: {d['user_id']}\n"
        f"💵 ₹{d['amount_inr']:.0f} INR | {d['payment_method'].upper()}\n"
        f"📊 {status_emoji(d['status'])} {d['status'].title()}\n"
        f"📅 {fmt_time(d['created_at'])}"
    )
    buttons = []
    if d["status"] == "pending":
        buttons.append([
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_deposit_{dep_id}", style="success"),
            InlineKeyboardButton("❌ Reject",  callback_data=f"reject_deposit_{dep_id}", style="danger"),
        ])
    buttons.append([InlineKeyboardButton("🔙 Deposits", callback_data="admin_deps_all_0", style="primary")])
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))


# ─── ADMIN: STATS ─────────────────────────────────────────────────────────────
async def admin_stats(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    conn  = get_db()
    stats = {
        "users":    conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"],
        "stock":    conn.execute("SELECT COUNT(*) as c FROM accounts").fetchone()["c"],
        "avail":    conn.execute("SELECT COUNT(*) as c FROM accounts WHERE is_sold=0").fetchone()["c"],
        "sold":     conn.execute("SELECT COUNT(*) as c FROM accounts WHERE is_sold=1").fetchone()["c"],
        "revenue":  conn.execute("SELECT COALESCE(SUM(amount_inr),0) as s FROM orders WHERE status='approved'").fetchone()["s"],
        "p_orders": conn.execute("SELECT COUNT(*) as c FROM orders WHERE status='pending'").fetchone()["c"],
        "p_deps":   conn.execute("SELECT COUNT(*) as c FROM deposits WHERE status='pending'").fetchone()["c"],
        "banned":   conn.execute("SELECT COUNT(*) as c FROM users WHERE is_banned=1").fetchone()["c"],
    }
    conn.close()
    rate = get_usdt_rate()
    text = (
        f"📊 *Bot Statistics*\n━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Users: {stats['users']}  🚫 Banned: {stats['banned']}\n"
        f"📦 Stock: {stats['avail']} available / {stats['stock']} total\n"
        f"✅ Sold: {stats['sold']}\n"
        f"💵 Revenue: ₹{stats['revenue']:.0f} INR\n"
        f"⏳ Pending Orders: {stats['p_orders']}\n"
        f"💳 Pending Deposits: {stats['p_deps']}\n"
        f"🪙 USDT Rate: 1 USDT = ₹{rate:.0f}\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    await query.edit_message_text(text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin", callback_data="admin_menu", style="primary")]]))


# ─── ADMIN: SETTINGS ──────────────────────────────────────────────────────────
async def admin_settings(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    maint  = get_setting("maintenance",   "0") == "1"
    upi    = get_setting("upi_enabled",   "1") == "1"
    crypto = get_setting("crypto_enabled","1") == "1"
    rate   = get_usdt_rate()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🔧 Maintenance: {'ON→OFF' if maint else 'OFF→ON'}", callback_data="toggle_maintenance", style="primary")],
        [InlineKeyboardButton(f"💳 UPI: {'✅ ON→Disable' if upi else '❌ OFF→Enable'}", callback_data="toggle_upi", style="primary")],
        [InlineKeyboardButton(f"🪙 Crypto: {'✅ ON→Disable' if crypto else '❌ OFF→Enable'}", callback_data="toggle_crypto", style="primary")],
        [InlineKeyboardButton(f"💱 USDT Rate: ₹{rate:.0f} → Change", callback_data="set_usdt_rate", style="primary")],
        [InlineKeyboardButton("📝 Welcome Message", callback_data="edit_welcome_msg", style="primary")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_menu", style="primary")],
    ])
    await query.edit_message_text("⚙️ *Settings*", parse_mode="Markdown", reply_markup=kb)

async def toggle_maintenance(update, context):
    query = update.callback_query
    await query.answer()
    new = "0" if get_setting("maintenance","0") == "1" else "1"
    set_setting("maintenance", new)
    await query.answer(f"Maintenance {'ON' if new=='1' else 'OFF'}!", show_alert=True)
    await admin_settings(update, context)

async def toggle_upi(update, context):
    query = update.callback_query
    await query.answer()
    new = "0" if get_setting("upi_enabled","1") == "1" else "1"
    set_setting("upi_enabled", new)
    await query.answer(f"UPI {'Enabled' if new=='1' else 'Disabled'}!", show_alert=True)
    await admin_settings(update, context)

async def toggle_crypto(update, context):
    query = update.callback_query
    await query.answer()
    new = "0" if get_setting("crypto_enabled","1") == "1" else "1"
    set_setting("crypto_enabled", new)
    await query.answer(f"Crypto {'Enabled' if new=='1' else 'Disabled'}!", show_alert=True)
    await admin_settings(update, context)

async def set_usdt_rate_cb(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    context.user_data["awaiting_usdt_rate"] = True
    rate = get_usdt_rate()
    await query.edit_message_text(
        f"💱 *Set USDT Rate*\n\nCurrent rate: 1 USDT = ₹{rate:.0f}\n\nEnter new INR rate for 1 USDT:\nExample: `85`",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_settings", style="danger")]]))

async def edit_welcome_msg(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    context.user_data["awaiting_welcome_msg"] = True
    await query.edit_message_text("Send new welcome message:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin_settings", style="danger")]]))


# ─── ADMIN: BROADCAST ─────────────────────────────────────────────────────────
async def admin_broadcast(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    context.user_data["awaiting_broadcast"] = True
    await query.edit_message_text("📢 Send the message to broadcast:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_menu", style="danger")]]))

async def broadcast_confirm(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    msg_id  = context.user_data.pop("broadcast_msg_id",  None)
    chat_id = context.user_data.pop("broadcast_chat_id", None)
    if not msg_id:
        await query.edit_message_text("❌ No message to broadcast.")
        return
    conn    = get_db()
    users   = conn.execute("SELECT id FROM users WHERE is_banned=0").fetchall()
    conn.close()
    success = 0
    for u in users:
        try:
            await context.bot.copy_message(chat_id=u["id"], from_chat_id=chat_id, message_id=msg_id)
            success += 1
        except Exception:
            pass
    await query.edit_message_text(f"✅ Broadcast sent to {success}/{len(users)} users.")


# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",         start))
    app.add_handler(CommandHandler("admin",         admin_cmd))
    app.add_handler(CommandHandler("addchannel",    addchannel_cmd))
    app.add_handler(CommandHandler("removechannel", removechannel_cmd))

    app.add_handler(CallbackQueryHandler(verify_sub,             pattern="^verify_sub$"))
    app.add_handler(CallbackQueryHandler(main_menu_cb,           pattern="^main_menu$"))
    app.add_handler(CallbackQueryHandler(browse_numbers,         pattern=r"^browse_\d+$"))
    app.add_handler(CallbackQueryHandler(noop_callback,          pattern="^noop$"))
    app.add_handler(CallbackQueryHandler(category_detail,        pattern=r"^cat_\d+$"))
    app.add_handler(CallbackQueryHandler(wallet_buy,             pattern=r"^wallet_buy_\d+$"))
    app.add_handler(CallbackQueryHandler(pay_upi,                pattern=r"^pay_upi_\d+$"))
    app.add_handler(CallbackQueryHandler(buy_upload_prompt,      pattern=r"^buy_upload_\d+$"))
    app.add_handler(CallbackQueryHandler(pay_crypto,             pattern=r"^pay_crypto_\d+$"))
    app.add_handler(CallbackQueryHandler(check_crypto_order_cb,  pattern=r"^chk_ord_\d+$"))
    app.add_handler(CallbackQueryHandler(reveal_number,          pattern=r"^reveal_\d+$"))
    app.add_handler(CallbackQueryHandler(get_otp,                pattern=r"^getotp_\d+$"))
    app.add_handler(CallbackQueryHandler(getotp_back,            pattern=r"^getotp_back_\d+$"))
    app.add_handler(CallbackQueryHandler(logout_prompt,          pattern=r"^logout_prompt_\d+$"))
    app.add_handler(CallbackQueryHandler(logout_confirm,         pattern=r"^logout_confirm_\d+$"))
    app.add_handler(CallbackQueryHandler(wallet,                 pattern="^wallet$"))
    app.add_handler(CallbackQueryHandler(deposit_upi_cb,         pattern="^deposit_upi$"))
    app.add_handler(CallbackQueryHandler(deposit_crypto_cb,      pattern="^deposit_crypto$"))
    app.add_handler(CallbackQueryHandler(check_dep_cb,           pattern=r"^chk_dep_\d+$"))
    app.add_handler(CallbackQueryHandler(my_orders,              pattern=r"^my_orders_\d+$"))
    app.add_handler(CallbackQueryHandler(order_detail,           pattern=r"^order_detail_\d+$"))
    app.add_handler(CallbackQueryHandler(dep_hist,               pattern=r"^dep_hist_\d+$"))
    app.add_handler(CallbackQueryHandler(help_cb,                pattern="^help$"))

    app.add_handler(CallbackQueryHandler(approve_order,          pattern=r"^approve_order_\d+$"))
    app.add_handler(CallbackQueryHandler(reject_order,           pattern=r"^reject_order_\d+$"))
    app.add_handler(CallbackQueryHandler(approve_deposit,        pattern=r"^approve_deposit_\d+$"))
    app.add_handler(CallbackQueryHandler(reject_deposit,         pattern=r"^reject_deposit_\d+$"))

    app.add_handler(CallbackQueryHandler(admin_menu_cb,          pattern="^admin_menu$"))
    app.add_handler(CallbackQueryHandler(admin_close,            pattern="^admin_close$"))
    app.add_handler(CallbackQueryHandler(admin_channels,         pattern="^admin_channels$"))
    app.add_handler(CallbackQueryHandler(del_channel_cb,         pattern=r"^del_channel_\d+$"))
    app.add_handler(CallbackQueryHandler(admin_stock,            pattern="^admin_stock$"))
    app.add_handler(CallbackQueryHandler(add_stock_start,        pattern="^add_stock_start$"))
    app.add_handler(CallbackQueryHandler(stock_cat_detail,       pattern=r"^stock_cat_\d+$"))
    app.add_handler(CallbackQueryHandler(addmore_cat,            pattern=r"^addmore_cat_\d+$"))
    app.add_handler(CallbackQueryHandler(setprice_cat,           pattern=r"^setprice_cat_\d+$"))
    app.add_handler(CallbackQueryHandler(toggle_cat,             pattern=r"^toggle_cat_\d+$"))
    app.add_handler(CallbackQueryHandler(del_cat,                pattern=r"^del_cat_\d+$"))
    app.add_handler(CallbackQueryHandler(skip_2fa_stock,         pattern="^skip_2fa_stock$"))
    app.add_handler(CallbackQueryHandler(remove_acc_cb,          pattern="^remove_acc$"))
    app.add_handler(CallbackQueryHandler(confirm_del,            pattern=r"^confirm_del_\d+$"))
    app.add_handler(CallbackQueryHandler(admin_orders,           pattern=r"^admin_orders_[a-z]+_\d+$"))
    app.add_handler(CallbackQueryHandler(admin_order_view,       pattern=r"^admin_order_view_\d+$"))
    app.add_handler(CallbackQueryHandler(admin_users,            pattern="^admin_users$"))
    app.add_handler(CallbackQueryHandler(admin_search_user,      pattern="^admin_search_user$"))
    app.add_handler(CallbackQueryHandler(admin_ban_user,         pattern="^admin_ban_user$"))
    app.add_handler(CallbackQueryHandler(admin_unban_user,       pattern="^admin_unban_user$"))
    app.add_handler(CallbackQueryHandler(admin_edit_wallet,      pattern="^admin_edit_wallet$"))
    app.add_handler(CallbackQueryHandler(ban_uid,                pattern=r"^ban_uid_\d+$"))
    app.add_handler(CallbackQueryHandler(unban_uid,              pattern=r"^unban_uid_\d+$"))
    app.add_handler(CallbackQueryHandler(editbal_uid,            pattern=r"^editbal_uid_\d+$"))
    app.add_handler(CallbackQueryHandler(admin_deps,             pattern=r"^admin_deps_[a-z]+_\d+$"))
    app.add_handler(CallbackQueryHandler(admin_dep_view,         pattern=r"^admin_dep_view_\d+$"))
    app.add_handler(CallbackQueryHandler(admin_stats,            pattern="^admin_stats$"))
    app.add_handler(CallbackQueryHandler(admin_settings,         pattern="^admin_settings$"))
    app.add_handler(CallbackQueryHandler(toggle_maintenance,     pattern="^toggle_maintenance$"))
    app.add_handler(CallbackQueryHandler(toggle_upi,             pattern="^toggle_upi$"))
    app.add_handler(CallbackQueryHandler(toggle_crypto,          pattern="^toggle_crypto$"))
    app.add_handler(CallbackQueryHandler(set_usdt_rate_cb,       pattern="^set_usdt_rate$"))
    app.add_handler(CallbackQueryHandler(edit_welcome_msg,       pattern="^edit_welcome_msg$"))
    app.add_handler(CallbackQueryHandler(admin_broadcast,        pattern="^admin_broadcast$"))
    app.add_handler(CallbackQueryHandler(broadcast_confirm,      pattern="^broadcast_confirm$"))

    app.add_handler(MessageHandler(filters.PHOTO,                   screenshot_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    logger.info("✅ Bot started!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

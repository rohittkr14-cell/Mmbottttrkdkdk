import os
import sqlite3
import re
import json
import asyncio
import logging
import sys
import threading
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions, ChatMember, Update
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ChatMemberHandler, filters, ContextTypes
from telegram.constants import ParseMode
from telegram.request import HTTPXRequest

# ─── LOGGING ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('bot.log')
    ]
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.INFO)
logger = logging.getLogger(__name__)

# ─── CONFIG ─────────────────────────────────────────────────────────────────
BOT_TOKEN = "8048885210:AAFl9fYFzQ9L_U6i-7hZJ_yp4eqMcy3BbaI"
BOT_USERNAME = "TrynoMm_bot"
ADMIN_IDS = [7691071175, 8685373658]
MM_USERNAME = "Trynomm"
VOUCH_FORWARD_CHANNEL_ID = -1004404217443

# ─── FAKE HTTP SERVER FOR RENDER ─────────────────────────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is running!")
    def log_message(self, format, *args):
        pass

def run_fake_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info(f" Fake HTTP server running on port {port}")
    server.serve_forever()

# ─── DATABASE SETUP ─────────────────────────────────────────────────────────
DB_PATH = "mm_23bot.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS deals (
        deal_id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        deal_number INTEGER,
        deal_terms TEXT,
        buyer_id INTEGER,
        seller_id INTEGER,
        agreed_price TEXT,
        currency_type TEXT,
        holding_amount TEXT,
        status TEXT DEFAULT 'pending',
        agreed_by TEXT DEFAULT '',
        confirm_decision TEXT DEFAULT '',
        invite_link TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS config (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    
    defaults = [
        ('upi_photo_id', ''),
        ('crypto_address', 'bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh'),
        ('crypto_network', 'Bep20'),
        ('crypto_fees', 'Fees'),
        ('vouch_link', 'https://t.me/Secureble/24?comment=1'),
        ('vouch_username', MM_USERNAME),
        ('vouch_forward_chat', str(VOUCH_FORWARD_CHANNEL_ID)),
    ]
    for k, v in defaults:
        c.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (k, v))
    
    c.execute('''CREATE TABLE IF NOT EXISTS deal_counter (
        chat_id INTEGER PRIMARY KEY,
        counter INTEGER DEFAULT 1
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS editing_sessions (
        user_id INTEGER PRIMARY KEY,
        field TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS forwarded_vouches (
        message_id INTEGER,
        chat_id INTEGER,
        forwarded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (message_id, chat_id)
    )''')
    
    conn.commit()
    conn.close()
    logger.info("Database initialized")

def get_config(key: str) -> str:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT value FROM config WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else ''

def set_config(key: str, value: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

def is_vouch_forwarded(chat_id: int, message_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT message_id FROM forwarded_vouches WHERE chat_id = ? AND message_id = ?", (chat_id, message_id))
    row = c.fetchone()
    conn.close()
    return row is not None

def mark_vouch_forwarded(chat_id: int, message_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO forwarded_vouches (chat_id, message_id) VALUES (?, ?)", (chat_id, message_id))
    conn.commit()
    conn.close()

def get_next_deal_number(chat_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT counter FROM deal_counter WHERE chat_id = ?", (chat_id,))
    row = c.fetchone()
    if row:
        counter = row[0] + 1
        c.execute("UPDATE deal_counter SET counter = ? WHERE chat_id = ?", (counter, chat_id))
    else:
        counter = 1
        c.execute("INSERT INTO deal_counter (chat_id, counter) VALUES (?, ?)", (chat_id, counter))
    conn.commit()
    conn.close()
    return counter

def create_deal(chat_id: int, deal_number: int, terms: str = '', buyer_id: int = 0, seller_id: int = 0, price: str = '', currency: str = '', holding: str = '') -> int:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT INTO deals (chat_id, deal_number, deal_terms, buyer_id, seller_id, agreed_price, currency_type, holding_amount)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
              (chat_id, deal_number, terms, buyer_id, seller_id, price, currency, holding))
    deal_id = c.lastrowid
    conn.commit()
    conn.close()
    return deal_id

def get_deal_by_chat(chat_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM deals WHERE chat_id = ? ORDER BY deal_id DESC LIMIT 1", (chat_id,))
    row = c.fetchone()
    conn.close()
    if row:
        columns = ['deal_id', 'chat_id', 'deal_number', 'deal_terms', 'buyer_id', 'seller_id', 
                   'agreed_price', 'currency_type', 'holding_amount', 'status', 'agreed_by', 
                   'confirm_decision', 'invite_link', 'created_at']
        return dict(zip(columns, row))
    return None

def update_deal_by_chat(chat_id: int, **kwargs):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT deal_id FROM deals WHERE chat_id = ? ORDER BY deal_id DESC LIMIT 1", (chat_id,))
    row = c.fetchone()
    if row:
        deal_id = row[0]
        sets = []
        values = []
        for k, v in kwargs.items():
            sets.append(f"{k} = ?")
            values.append(v)
        values.append(deal_id)
        c.execute(f"UPDATE deals SET {', '.join(sets)} WHERE deal_id = ?", values)
    conn.commit()
    conn.close()

# ─── HELPERS ─────────────────────────────────────────────────────────────────

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    if user_id in ADMIN_IDS:
        return True
    
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]
    except:
        return False

async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except:
        pass

def get_mention(user_id: int, name: str = "") -> str:
    return f"<a href='tg://user?id={user_id}'>{name or 'User'}</a>"

async def set_title(update: Update, context: ContextTypes.DEFAULT_TYPE, title: str):
    try:
        await context.bot.set_chat_title(chat_id=update.effective_chat.id, title=title)
    except Exception as e:
        logger.error(f"set_title failed: {e}")

async def pin_msg(update: Update, context: ContextTypes.DEFAULT_TYPE, message_id: int):
    try:
        await context.bot.pin_chat_message(chat_id=update.effective_chat.id, message_id=message_id)
    except Exception as e:
        logger.error(f"pin_msg failed: {e}")

async def create_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    try:
        link = await context.bot.create_chat_invite_link(
            chat_id=update.effective_chat.id,
            member_limit=2
        )
        return link.invite_link
    except Exception as e:
        logger.error(f"create_link failed: {e}")
        return ""

def parse_cmd(text: str) -> tuple:
    if not text:
        return None, None
    text = text.strip()
    prefix = text[0] if text else ''
    if prefix in ['/', '.']:
        parts = text[1:].split(None, 1)
        cmd = parts[0].lower() if parts else ''
        args = parts[1] if len(parts) > 1 else ''
        return cmd, args
    return None, None

def style_keyboard(rows):
    inline_keyboard = []
    for row in rows:
        button_row = []
        for btn in row:
            if btn.get("url"):
                button_row.append(InlineKeyboardButton(text=btn["text"], url=btn["url"]))
            else:
                button_row.append(InlineKeyboardButton(text=btn["text"], callback_data=btn["callback_data"]))
        inline_keyboard.append(button_row)
    return InlineKeyboardMarkup(inline_keyboard)

# ─── COMMAND HANDLERS ───────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type != 'private':
        return
    keyboard = [[{"text": "Contact MM", "url": f"https://t.me/{MM_USERNAME}", "style": "primary"}]]
    await context.bot.send_message(
        chat_id=chat.id,
        text=f"<b>Welcome to the MM Service of @{MM_USERNAME}.</b>\n\nContact below for making a Secure GC.\n\nThank you, Have a Nice Day!",
        reply_markup=style_keyboard(keyboard),
        parse_mode=ParseMode.HTML
    )

async def set_deal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await delete_cmd(update, context)
        return
    chat_id = update.effective_chat.id
    deal_number = get_next_deal_number(chat_id)
    
    # GROUP NAME SETTING AS REQUESTED
    group_title = f"Deal #{deal_number} • @{MM_USERNAME}"
    await set_title(update, context, group_title)

    link = await create_link(update, context)
    create_deal(chat_id, deal_number)
    await delete_cmd(update, context)
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"<b>Invite Link Generated</b>\n\nPlease join & share this with the other user involved in the deal.\n\n{link}",
        parse_mode=ParseMode.HTML
    )
    msg2 = await context.bot.send_message(
        chat_id=chat_id,
        text="<b>Deal Setup</b>\n\nHey. Please state the terms of the deal.\n\n• What is the deal?\n• Who is the buyer/seller?\n• What is the agreed price and which crypto/currency?\n• Include any other relevant information.",
        parse_mode=ParseMode.HTML
    )
    update_deal_by_chat(chat_id, invite_link=link)
    await asyncio.sleep(0.5)
    await pin_msg(update, context, msg2.message_id)

async def rec(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await delete_cmd(update, context)
        return
    args_text = " ".join(context.args) if context.args else ""
    if not args_text and hasattr(update.message, 'text'):
        cmd, extracted_args = parse_cmd(update.message.text)
        args_text = extracted_args
    
    amount = ""
    deal_number = None
    if args_text:
        numbers = re.findall(r'\d+', args_text)
        if len(numbers) >= 2:
            amount = numbers[0]
            deal_number = int(numbers[1])
        elif len(numbers) == 1:
            amount = numbers[0]
    
    if deal_number is None:
        deal = get_deal_by_chat(update.effective_chat.id)
        deal_number = deal['deal_number'] if deal else 0
    
    currency_symbol = "₹"
    if ' $' in args_text or args_text.startswith('$'):
        currency_symbol = "$"
    amount_clean = amount
    
    await delete_cmd(update, context)
    msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"<b>Payment Received</b>\n\nI have successfully received the amount and the MM fee. It is safe to deal forward.\n\n<i>I will process the payment after the deal concludes. Thank you for your cooperation and trust!</i>",
        parse_mode=ParseMode.HTML
    )
    
    if amount_clean and deal_number:
        update_deal_by_chat(update.effective_chat.id, holding_amount=f"{currency_symbol}{amount_clean}")
        await set_title(update, context, f"Deal #{deal_number} • @Holding {currency_symbol}{amount_clean}")
    
    await asyncio.sleep(0.5)
    await pin_msg(update, context, msg.message_id)

async def agree(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await delete_cmd(update, context)
        return
    if not update.message.reply_to_message:
        await delete_cmd(update, context)
        return
    target_user = update.message.reply_to_message.from_user
    target_id = target_user.id
    target_name = target_user.full_name or "User"
    mention = get_mention(target_id, target_name)
    
    keyboard = [[{"text": f"Agree - {target_name}", "callback_data": f"agree_{target_id}_{update.effective_chat.id}", "style": "success"}]]
    await delete_cmd(update, context)
    msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"<b>Deal Agreement</b>\n\nPlease confirm that you agree to the terms stated above.\n\n{mention} can confirm this agreement by clicking the button below.",
        reply_markup=style_keyboard(keyboard),
        parse_mode=ParseMode.HTML
    )
    await asyncio.sleep(0.5)
    await pin_msg(update, context, msg.message_id)

async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await delete_cmd(update, context)
        return
    if not update.message.reply_to_message:
        await delete_cmd(update, context)
        return
    target_user = update.message.reply_to_message.from_user
    target_id = target_user.id
    target_name = target_user.full_name or "User"
    mention = get_mention(target_id, target_name)
    
    keyboard = [
        [
            {"text": f"Release - {target_name}", "callback_data": f"release_{target_id}_{update.effective_chat.id}", "style": "primary"},
            {"text": f"Refund - {target_name}", "callback_data": f"refund_{target_id}_{update.effective_chat.id}", "style": "danger"}
        ]
    ]
    await delete_cmd(update, context)
    msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"<b>Final Confirmation</b>\n\nWhen the deal is done, please choose an action. Only {mention} can make this decision.\n\n<b>Release</b> - Funds will be released to the seller\n<b>Refund</b> - Funds will be refunded to the buyer",
        reply_markup=style_keyboard(keyboard),
        parse_mode=ParseMode.HTML
    )
    await asyncio.sleep(0.5)
    await pin_msg(update, context, msg.message_id)

async def inr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await delete_cmd(update, context)
        return
    args_text = " ".join(context.args) if context.args else ""
    if not args_text and hasattr(update.message, 'text'):
        cmd, extracted_args = parse_cmd(update.message.text)
        args_text = extracted_args
    amount = args_text or "0"
    photo_id = get_config('upi_photo_id')
    text = f"<b>Pay on this QR</b>\n\nMust send the payment screenshot.\n\n<b>Deal Amount + {amount} Fees</b>"
    await delete_cmd(update, context)
    if photo_id:
        try:
            await context.bot.send_photo(chat_id=update.effective_chat.id, photo=photo_id, caption=text, parse_mode=ParseMode.HTML)
        except:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"{text}\n\n<i>(UPI QR photo not available, admin please use setinrphoto in bot DM)</i>", parse_mode=ParseMode.HTML)
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"{text}\n\n<i>(UPI QR not set. Admin please use setinrphoto in bot DM)</i>", parse_mode=ParseMode.HTML)

async def crp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await delete_cmd(update, context)
        return
    args_text = " ".join(context.args) if context.args else ""
    if not args_text and hasattr(update.message, 'text'):
        cmd, extracted_args = parse_cmd(update.message.text)
        args_text = extracted_args
    amount = args_text or "0"
    address = get_config('crypto_address')
    network = get_config('crypto_network')
    fees = get_config('crypto_fees')
    text = f"<b>Crypto Payment</b>\n\n<b>Network:</b> {network}\n<b>Address:</b> <code>{address}</code>\n\n<b>Deal Amount + {fees} {amount}</b>\n\n<i>Please double-check the network before sending.</i>"
    await delete_cmd(update, context)
    await context.bot.send_message(chat_id=update.effective_chat.id, text=text, parse_mode=ParseMode.HTML)

async def link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await delete_cmd(update, context)
        return
    l = await create_link(update, context)
    await delete_cmd(update, context)
    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"<b>Invite Link</b>\n\nPlease share this with the other user:\n{l}", parse_mode=ParseMode.HTML)

async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await delete_cmd(update, context)
        return
    args_text = " ".join(context.args) if context.args else ""
    if not args_text and hasattr(update.message, 'text'):
        cmd, extracted_args = parse_cmd(update.message.text)
        args_text = extracted_args
    deal_number = None
    if args_text and args_text.strip().isdigit():
        deal_number = int(args_text.strip())
    if deal_number is None:
        deal = get_deal_by_chat(update.effective_chat.id)
        deal_number = deal['deal_number'] if deal else 0
    await delete_cmd(update, context)
    await set_title(update, context, f"Deal #{deal_number} • @Completed")
    msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"<b>Deal Completed!</b>\n\nThank you for using my Middleman service!\n\nPlease leave me a vouch here:\n\n<code>Vouch @{MM_USERNAME} MMD</code>",
        parse_mode=ParseMode.HTML
    )
    await asyncio.sleep(0.5)
    await pin_msg(update, context, msg.message_id)

async def lock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    chat_id = update.effective_chat.id
    await delete_cmd(update, context)
    try:
        await context.bot.set_chat_permissions(chat_id=chat_id, permissions=ChatPermissions.no_permissions())
        await context.bot.send_message(chat_id=chat_id, text="<b>Group has been locked.</b>", parse_mode=ParseMode.HTML)
    except: pass

async def unlock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    chat_id = update.effective_chat.id
    await delete_cmd(update, context)
    try:
        await context.bot.set_chat_permissions(chat_id=chat_id, permissions=ChatPermissions.all_permissions())
        await context.bot.send_message(chat_id=chat_id, text="<b>Group has been unlocked.</b>", parse_mode=ParseMode.HTML)
    except: pass

async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context) or not update.message.reply_to_message:
        await delete_cmd(update, context); return
    target = update.message.reply_to_message.from_user
    await delete_cmd(update, context)
    try:
        await context.bot.ban_chat_member(chat_id=update.effective_chat.id, user_id=target.id)
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"<b>{get_mention(target.id, target.full_name)} banned.</b>", parse_mode=ParseMode.HTML)
    except: pass

async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context) or not update.message.reply_to_message:
        await delete_cmd(update, context); return
    target = update.message.reply_to_message.from_user
    await delete_cmd(update, context)
    try:
        await context.bot.unban_chat_member(chat_id=update.effective_chat.id, user_id=target.id)
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"<b>{get_mention(target.id, target.full_name)} unbanned.</b>", parse_mode=ParseMode.HTML)
    except: pass

async def kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context) or not update.message.reply_to_message:
        await delete_cmd(update, context); return
    target = update.message.reply_to_message.from_user
    await delete_cmd(update, context)
    try:
        await context.bot.ban_chat_member(chat_id=update.effective_chat.id, user_id=target.id)
        await context.bot.unban_chat_member(chat_id=update.effective_chat.id, user_id=target.id)
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"<b>{get_mention(target.id, target.full_name)} kicked.</b>", parse_mode=ParseMode.HTML)
    except: pass

async def mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context) or not update.message.reply_to_message:
        await delete_cmd(update, context); return
    target = update.message.reply_to_message.from_user
    await delete_cmd(update, context)
    try:
        permissions = ChatPermissions(can_send_messages=False)
        await context.bot.restrict_chat_member(chat_id=update.effective_chat.id, user_id=target.id, permissions=permissions)
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"<b>{get_mention(target.id, target.full_name)} muted.</b>", parse_mode=ParseMode.HTML)
    except: pass

async def unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context) or not update.message.reply_to_message:
        await delete_cmd(update, context); return
    target = update.message.reply_to_message.from_user
    await delete_cmd(update, context)
    try:
        permissions = ChatPermissions.all_permissions()
        await context.bot.restrict_chat_member(chat_id=update.effective_chat.id, user_id=target.id, permissions=permissions)
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"<b>{get_mention(target.id, target.full_name)} unmuted.</b>", parse_mode=ParseMode.HTML)
    except: pass

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    await delete_cmd(update, context)
    text = f"<b>Your Info</b>\nUser ID: <code>{user.id}</code>\nUsername: @{user.username or 'N/A'}\n\n<b>Chat Info</b>\nChat ID: <code>{chat.id}</code>\nChat Type: {chat.type}"
    await context.bot.send_message(chat_id=chat.id, text=text, parse_mode=ParseMode.HTML)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await delete_cmd(update, context)
    keyboard = [
        [{"text": "Deal Commands", "callback_data": "help_deal", "style": "primary"}],
        [{"text": "Group Control", "callback_data": "help_group", "style": "primary"}],
        [{"text": "Admin Commands", "callback_data": "help_admin", "style": "primary"}]
    ]
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="<b>Bot Help Menu</b>\n\nSelect a category below to see available commands:",
        reply_markup=style_keyboard(keyboard),
        parse_mode=ParseMode.HTML
    )

# ─── ADMIN DM COMMANDS ───────────────────────────────────────────────────────

async def setinrphoto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != 'private' or update.effective_user.id not in ADMIN_IDS: return
    c = sqlite3.connect(DB_PATH).cursor()
    c.execute("INSERT OR REPLACE INTO editing_sessions (user_id, field) VALUES (?, ?)", (update.effective_user.id, 'upi_photo'))
    c.connection.commit()
    await update.message.reply_text("Send me the new UPI QR photo. Send cancel to cancel.")

async def editcrp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != 'private' or update.effective_user.id not in ADMIN_IDS: return
    await update.message.reply_text(f"<b>Current Config:</b>\nNetwork: {get_config('crypto_network')}\nAddress: <code>{get_config('crypto_address')}</code>\nFees: {get_config('crypto_fees')}\n\nSend new address as:\n<code>ADDRESS|NETWORK|FEES</code>", parse_mode=ParseMode.HTML)
    c = sqlite3.connect(DB_PATH).cursor()
    c.execute("INSERT OR REPLACE INTO editing_sessions (user_id, field) VALUES (?, ?)", (update.effective_user.id, 'crypto_config'))
    c.connection.commit()

async def setvouch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != 'private' or update.effective_user.id not in ADMIN_IDS: return
    await update.message.reply_text(f"<b>Current Vouch Config:</b>\nUsername: @{get_config('vouch_username')}\nLink: {get_config('vouch_link')}\n\nSend new values as:\n<code>USERNAME|LINK</code>", parse_mode=ParseMode.HTML)
    c = sqlite3.connect(DB_PATH).cursor()
    c.execute("INSERT OR REPLACE INTO editing_sessions (user_id, field) VALUES (?, ?)", (update.effective_user.id, 'vouch_config'))
    c.connection.commit()

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != 'private': return
    c = sqlite3.connect(DB_PATH).cursor()
    c.execute("DELETE FROM editing_sessions WHERE user_id = ?", (update.effective_user.id,))
    c.connection.commit()
    await update.message.reply_text("Editing session cancelled.")

# ─── CALLBACK QUERY HANDLERS ─────────────────────────────────────────────────

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user = query.from_user
    await query.answer()
    
    if data.startswith("help_"):
        if data == "help_deal":
            text = "<b>Deal Commands</b>\n\n<b>set</b> - Clear msgs, send invite link, set deal name & pin\n<b>rec [amount]</b> - Confirm payment received & pin\n<b>agree</b> - Reply to a user to set agreement confirmer\n<b>confirm</b> - Reply to a user to give release/refund decision\n<b>inr [amount]</b> - Send UPI details\n<b>crp [amount]</b> - Send crypto details\n<b>link</b> - Generate invite link\n<b>done</b> - Mark deal completed"
        elif data == "help_group":
            text = "<b>Group Control</b>\n\n<b>lock</b> - Lock group (read-only)\n<b>unlock</b> - Unlock group\n<b>kick</b> - Kick user\n<b>ban</b> - Ban user\n<b>unban</b> - Unban user\n<b>mute</b> - Mute user\n<b>unmute</b> - Unmute user"
        elif data == "help_admin":
            text = "<b>Admin DM Only</b>\n\n<b>setinrphoto</b> - Change UPI photo\n<b>editcrp</b> - Edit crypto address & fees\n<b>setvouch</b> - Edit vouch username & link\n<b>cancel</b> - Cancel active editing session"
        elif data == "help_main":
            text = "<b>Bot Help Menu</b>\n\nSelect a category below to see available commands:"
            keyboard = [
                [{"text": "Deal Commands", "callback_data": "help_deal", "style": "primary"}],
                [{"text": "Group Control", "callback_data": "help_group", "style": "primary"}],
                [{"text": "Admin Commands", "callback_data": "help_admin", "style": "primary"}]
            ]
            await query.edit_message_text(text=text, reply_markup=style_keyboard(keyboard), parse_mode=ParseMode.HTML)
            return

        keyboard = [[{"text": "Back to Menu", "callback_data": "help_main", "style": "primary"}]]
        await query.edit_message_text(text=text, reply_markup=style_keyboard(keyboard), parse_mode=ParseMode.HTML)
        return

    if data.startswith("agree_"):
        parts = data.split("_")
        if user.id != int(parts[1]):
            await query.answer("You are not authorized to confirm this agreement!", show_alert=True)
            return
        mention = get_mention(user.id, user.full_name)
        new_text = f"<b>Agreed by the dealer.</b>\n\nBoth users have agreed to the deal terms.\nConfirmed by: {mention}\n\n<i>Now, continue the deal.</i>"
        await query.edit_message_text(text=new_text, parse_mode=ParseMode.HTML)
        update_deal_by_chat(int(parts[2]), agreed_by=str(user.id))
    
    elif data.startswith("release_"):
        parts = data.split("_")
        if user.id != int(parts[1]):
            await query.answer("You are not authorized to make this decision!", show_alert=True)
            return
        mention = get_mention(user.id, user.full_name)
        new_text = f"<b>Funds Released Initiated</b>\n\nThe buyer has agreed to release the funds.\nAction taken by: {mention}"
        await query.edit_message_text(text=new_text, parse_mode=ParseMode.HTML)
        await context.bot.send_message(chat_id=int(parts[2]), text=f"<b>Release Confirmation</b>\n\n@{MM_USERNAME} please release the funds.\nSeller, Please Drop the QR or UPI!\n\n<i>Wait for @{MM_USERNAME}'s response.</i>", parse_mode=ParseMode.HTML)
        update_deal_by_chat(int(parts[2]), confirm_decision='release', status='completed')
    
    elif data.startswith("refund_"):
        parts = data.split("_")
        if user.id != int(parts[1]):
            await query.answer("You are not authorized to make this decision!", show_alert=True)
            return
        mention = get_mention(user.id, user.full_name)
        new_text = f"<b>Refund Initiated</b>\n\nThe buyer has agreed to refund the amount.\nAction taken by: {mention}"
        await query.edit_message_text(text=new_text, parse_mode=ParseMode.HTML)
        await context.bot.send_message(chat_id=int(parts[2]), text=f"<b>Refund Confirmation</b>\n\n@{MM_USERNAME} please process the refund.\nSeller Please Confirm this Refund and Buyer Drop the QR or UPI.\n\n<i>Wait for @{MM_USERNAME}'s response.</i>", parse_mode=ParseMode.HTML)
        update_deal_by_chat(int(parts[2]), confirm_decision='refund', status='refunded')

# ─── MESSAGE HANDLERS ────────────────────────────────────────────────────────

async def handle_dm_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if chat.type != 'private': return
    
    c = sqlite3.connect(DB_PATH).cursor()
    c.execute("SELECT field FROM editing_sessions WHERE user_id = ?", (user.id,))
    row = c.fetchone()
    
    if not row: return
    field = row[0]
    
    if field == 'upi_photo':
        if update.message.photo:
            set_config('upi_photo_id', update.message.photo[-1].file_id)
            c.execute("DELETE FROM editing_sessions WHERE user_id = ?", (user.id,))
            c.connection.commit()
            await update.message.reply_text("UPI photo updated successfully!")
        else:
            await update.message.reply_text("Please send a photo. Send cancel to cancel.")
            
    elif field == 'crypto_config':
        text = update.message.text
        if not text: return
        parts = text.split("|")
        if len(parts) >= 1: set_config('crypto_address', parts[0].strip())
        if len(parts) >= 2: set_config('crypto_network', parts[1].strip())
        if len(parts) >= 3: set_config('crypto_fees', parts[2].strip())
        c.execute("DELETE FROM editing_sessions WHERE user_id = ?", (user.id,))
        c.connection.commit()
        await update.message.reply_text("Crypto config updated!")
        
    elif field == 'vouch_config':
        text = update.message.text
        if not text: return
        parts = text.split("|")
        if len(parts) >= 1: set_config('vouch_username', parts[0].strip())
        if len(parts) >= 2: set_config('vouch_link', parts[1].strip())
        c.execute("DELETE FROM editing_sessions WHERE user_id = ?", (user.id,))
        c.connection.commit()
        await update.message.reply_text("Vouch config updated!")

async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ['group', 'supergroup']:
        return
    if not update.message or not update.message.text:
        return
    
    text = update.message.text.strip()
    
    # ─── VOUCH DETECTION (NO AUTO DELETE) ──────────────────────────────────
    vouch_pattern = re.compile(r'vouch\s+@(\w+)', re.IGNORECASE)
    match = vouch_pattern.search(text)
    if match:
        chat_id = update.effective_chat.id
        message_id = update.message.message_id
        if not is_vouch_forwarded(chat_id, message_id):
            try:
                await context.bot.forward_message(chat_id=VOUCH_FORWARD_CHANNEL_ID, from_chat_id=chat_id, message_id=message_id)
                mark_vouch_forwarded(chat_id, message_id)
                logger.info(f"Vouch forwarded successfully.")
            except Exception as e:
                logger.error(f"Vouch forward failed: {e}")
    
    # ─── COMMAND DETECTION WITHOUT PREFIX ────────────────────────────────
    if text.startswith('/') or text.startswith('.'):
        return
    
    before_cmd = text.split(None, 1)[0].lower()
    known_commands = [
        'set', 'rec', 'agree', 'confirm', 'inr', 'crp', 'link', 'done',
        'lock', 'unlock', 'kick', 'ban', 'unban', 'mute', 'unmute',
        'id', 'help'
    ]
    
    if before_cmd not in known_commands:
        return
    
    if ' ' in text:
        context.args = text.split(None, 1)[1].split()
    else:
        context.args = []
    
    handler_map = {
        'set': set_deal, 'rec': rec, 'agree': agree, 'confirm': confirm,
        'inr': inr, 'crp': crp, 'link': link, 'done': done,
        'lock': lock, 'unlock': unlock, 'kick': kick, 'ban': ban,
        'unban': unban, 'mute': mute, 'unmute': unmute,
        'id': cmd_id, 'help': help_cmd,
    }
    
    handler = handler_map.get(before_cmd)
    if handler:
        try:
            await handler(update, context)
        except Exception as e:
            logger.error(f"Handler {before_cmd} error: {e}")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")

# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    init_db()
    
    http_thread = threading.Thread(target=run_fake_server, daemon=True)
    http_thread.start()
    
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(30.0)
        .build()
    )
    
    # Commands with Prefix
    app.add_handler(CommandHandler("set", set_deal))
    app.add_handler(CommandHandler("rec", rec))
    app.add_handler(CommandHandler("agree", agree))
    app.add_handler(CommandHandler("confirm", confirm))
    app.add_handler(CommandHandler("inr", inr))
    app.add_handler(CommandHandler("crp", crp))
    app.add_handler(CommandHandler("link", link))
    app.add_handler(CommandHandler("done", done))
    app.add_handler(CommandHandler("lock", lock))
    app.add_handler(CommandHandler("unlock", unlock))
    app.add_handler(CommandHandler("kick", kick))
    app.add_handler(CommandHandler("ban", ban))
    app.add_handler(CommandHandler("unban", unban))
    app.add_handler(CommandHandler("mute", mute))
    app.add_handler(CommandHandler("unmute", unmute))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("setinrphoto", setinrphoto))
    app.add_handler(CommandHandler("editcrp", editcrp))
    app.add_handler(CommandHandler("setvouch", setvouch))
    app.add_handler(CommandHandler("cancel", cancel))
    
    # Buttons & Text handlers
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.PHOTO | filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_dm_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS, handle_group_message))
    app.add_error_handler(error_handler)
    
    logger.info(" Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
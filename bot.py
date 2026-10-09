#!/usr/bin/env python3
"""
🤖 TG Scraper Bot
Features:
  - Scrape Telegram group members
  - Add members to groups
  - Multi-account login / session switching
  - 🛡️ Admin control: approve users, ban, kill switch, activity logs
"""

import asyncio
import configparser
import csv
import io
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, InputFile
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                           ConversationHandler, MessageHandler,
                           ContextTypes, filters)
from telegram.constants import ParseMode
from telethon import TelegramClient, errors
from telethon.tl.functions.channels import GetParticipantsRequest, InviteToChannelRequest
from telethon.tl.functions.messages import AddChatUserRequest
from telethon.tl.types import (ChannelParticipantsSearch,
                                UserStatusOnline, UserStatusRecently,
                                UserStatusLastWeek, UserStatusLastMonth,
                                UserStatusOffline)

# ── States ─────────────────────────────────────────────────────────────────────
WAIT_SOURCE, WAIT_MAX, WAIT_TARGET, WAIT_ADD_LIMIT = range(4)
WAIT_PHONE, WAIT_CODE, WAIT_2FA, WAIT_SESSION_NAME = range(10, 14)

# ── Config ─────────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.ini"
OUTPUT_DIR  = BASE_DIR / "output"
USERS_FILE  = BASE_DIR / "users.json"
LOG_FILE    = BASE_DIR / "activity.log"
OUTPUT_DIR.mkdir(exist_ok=True)

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.WARNING)
logger = logging.getLogger(__name__)

cfg = configparser.ConfigParser()
cfg.read(CONFIG_FILE)
API_ID    = int(cfg["telegram"]["api_id"])
API_HASH  = cfg["telegram"]["api_hash"]
BOT_TOKEN = cfg["telegram"]["bot_token"]
DEFAULT_SESSION = cfg["telegram"].get("session", "tg_scraper_session")

# Parse admin IDs
_raw_admins = cfg.get("telegram", "admin_ids", fallback="").strip()
ADMIN_IDS: set[int] = {int(x.strip()) for x in _raw_admins.split(",") if x.strip().isdigit()}

# ── Global client state ────────────────────────────────────────────────────────
state = {
    "client":       TelegramClient(str(BASE_DIR / DEFAULT_SESSION), API_ID, API_HASH),
    "session_name": DEFAULT_SESSION,
    "kill_switch":  False,   # True = all user operations disabled
}


def client() -> TelegramClient:
    return state["client"]


async def switch_session(name: str):
    c = state["client"]
    if c.is_connected():
        await c.disconnect()
    state["client"]       = TelegramClient(str(BASE_DIR / name), API_ID, API_HASH)
    state["session_name"] = name
    await state["client"].connect()


def list_sessions() -> list[str]:
    return [f.stem for f in sorted(BASE_DIR.glob("*.session"))]


# ── Admin / User System ────────────────────────────────────────────────────────

def load_users() -> dict:
    if USERS_FILE.exists():
        return json.loads(USERS_FILE.read_text(encoding="utf-8"))
    return {}


def save_users(users: dict):
    USERS_FILE.write_text(json.dumps(users, indent=2, ensure_ascii=False), encoding="utf-8")


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def get_status(uid: int) -> str:
    """admin | approved | pending | banned"""
    if is_admin(uid):
        return "admin"
    u = load_users().get(str(uid), {})
    if u.get("banned"):
        return "banned"
    return u.get("status", "pending")


def is_approved(uid: int) -> bool:
    return get_status(uid) in ("admin", "approved")


def register_user(uid: int, username: str, name: str) -> bool:
    """Returns True if this is a brand-new user."""
    users = load_users()
    if str(uid) in users:
        return False
    users[str(uid)] = {
        "username": username or "",
        "name":     name or "",
        "status":   "pending",
        "banned":   False,
        "joined":   datetime.now(timezone.utc).isoformat(),
        "last_action": "",
    }
    save_users(users)
    return True


def approve_user(uid: int):
    users = load_users()
    if str(uid) in users:
        users[str(uid)]["status"] = "approved"
        users[str(uid)]["banned"] = False
    else:
        users[str(uid)] = {"status": "approved", "banned": False,
                           "username": "", "name": "", "joined": datetime.now(timezone.utc).isoformat()}
    save_users(users)


def ban_user(uid: int):
    users = load_users()
    if str(uid) not in users:
        users[str(uid)] = {"status": "pending", "username": "", "name": "", "joined": ""}
    users[str(uid)]["banned"] = True
    save_users(users)


def unban_user(uid: int):
    users = load_users()
    if str(uid) in users:
        users[str(uid)]["banned"] = False
        users[str(uid)]["status"] = "approved"
    save_users(users)


def log_activity(uid: int, username: str, action: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] ID:{uid} @{username or 'N/A'}: {action}\n"
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line)
    # Update last_action in users
    users = load_users()
    if str(uid) in users:
        users[str(uid)]["last_action"] = f"{action} at {ts}"
        save_users(users)


async def notify_admins(context: ContextTypes.DEFAULT_TYPE, uid: int, username: str, name: str):
    """Notify all admins about a new user requesting access."""
    if not ADMIN_IDS:
        return
    text = (
        f"🔔 *New access request!*\n\n"
        f"👤 Name: *{name}*\n"
        f"📱 Username: @{username or 'N/A'}\n"
        f"🆔 ID: `{uid}`\n\n"
        f"Approve or ban?"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"apv_{uid}"),
        InlineKeyboardButton("🚫 Ban",     callback_data=f"ban_{uid}"),
    ]])
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        except Exception:
            pass


async def notify_user_approved(context: ContextTypes.DEFAULT_TYPE, uid: int):
    try:
        await context.bot.send_message(
            uid,
            "🎉 *Your access has been approved!*\n\nYou can now use TG Scraper Bot.\nTap /start to begin!",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        pass


async def notify_user_banned(context: ContextTypes.DEFAULT_TYPE, uid: int):
    try:
        await context.bot.send_message(
            uid,
            "🚫 *Your access has been revoked.*\n\nContact the admin for more info.",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        pass


async def check_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Returns True if user may proceed. Handles all gate-keeping logic."""
    user     = update.effective_user
    uid      = user.id
    username = user.username or ""
    name     = user.full_name or ""

    # Register if new
    is_new = register_user(uid, username, name)

    status = get_status(uid)

    # Kill switch (admins bypass)
    if state["kill_switch"] and status != "admin":
        msg = "🔴 *System is temporarily offline.*\n\nPlease try again later."
        if update.message:
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        elif update.callback_query:
            await update.callback_query.answer("🔴 System offline", show_alert=True)
        return False

    if status == "banned":
        msg = "🚫 *Your access has been revoked.*\n\nContact the admin."
        if update.message:
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        elif update.callback_query:
            await update.callback_query.answer("🚫 Access revoked", show_alert=True)
        return False

    if status == "pending":
        if is_new:
            await notify_admins(context, uid, username, name)
        msg = (
            "⏳ *Awaiting admin approval.*\n\n"
            "Your request has been sent. You'll be notified once approved.\n\n"
        )
        if not ADMIN_IDS:
            msg += (
                "⚠️ _No admin ID is configured yet!_\n"
                "The bot owner must add their ID to config.ini first.\n"
                "Run /myid to find your Telegram ID."
            )
        if update.message:
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        elif update.callback_query:
            await update.callback_query.answer("⏳ Awaiting approval", show_alert=True)
        return False

    return True  # approved or admin


# ── Helpers ────────────────────────────────────────────────────────────────────

def status_label(s):
    if isinstance(s, UserStatusOnline):      return "🟢 Online"
    elif isinstance(s, UserStatusRecently):  return "🔵 Recently"
    elif isinstance(s, UserStatusLastWeek):  return "🟡 Last Week"
    elif isinstance(s, UserStatusLastMonth): return "🟠 Last Month"
    elif isinstance(s, UserStatusOffline):   return "⚫ Offline"
    else:                                     return "❓ Unknown"


def clean_md(text: str) -> str:
    """Remove markdown characters that break V1 parsing."""
    if not text: return ""
    return str(text).replace("*", "").replace("_", "").replace("`", "").replace("[", "").replace("]", "")


def build_member(user):
    return {
        "user_id":    user.id,
        "username":   user.username or "",
        "first_name": user.first_name or "",
        "last_name":  user.last_name or "",
        "phone":      user.phone or "",
        "status":     status_label(user.status),
        "is_bot":     int(user.bot),
        "is_premium": int(getattr(user, "premium", False)),
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }


def to_csv(members) -> bytes:
    buf = io.StringIO()
    fields = ["user_id","username","first_name","last_name","phone","status","is_bot","is_premium","scraped_at"]
    w = csv.DictWriter(buf, fieldnames=fields)
    w.writeheader()
    w.writerows(members)
    return buf.getvalue().encode("utf-8")


def to_txt(members) -> bytes:
    return "\n".join(f"@{m['username']}" for m in members if m["username"]).encode("utf-8")


def pbar(curr, total, width=18):
    if total <= 0: return "░" * width
    pct = min(curr / total, 1.0)
    return f"{'█'*int(width*pct)}{'░'*(width-int(width*pct))}  {int(pct*100)}%"


# ── Keyboards ──────────────────────────────────────────────────────────────────

def kb_main(uid: int):
    rows = [
        [InlineKeyboardButton("🔍  Scrape Members Only",      callback_data="scrape_only")],
        [InlineKeyboardButton("🔍➕ Scrape & Add to Group",   callback_data="scrape_add")],
        [InlineKeyboardButton("📁  Download Last Results",    callback_data="last_results")],
        [InlineKeyboardButton("👤  Accounts / Login",         callback_data="accounts")],
        [InlineKeyboardButton("ℹ️  How to Use",               callback_data="help")],
    ]
    if is_admin(uid):
        rows.insert(0, [InlineKeyboardButton("🛡️  Admin Panel",  callback_data="admin_panel")])
    return InlineKeyboardMarkup(rows)


def kb_max():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("50",    callback_data="max_50"),
         InlineKeyboardButton("100",   callback_data="max_100"),
         InlineKeyboardButton("500",   callback_data="max_500")],
        [InlineKeyboardButton("1 000", callback_data="max_1000"),
         InlineKeyboardButton("♾️ All",callback_data="max_0")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])


def kb_add_limit(total):
    q, h = max(10, total//4), max(20, total//2)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(str(min(q,total)), callback_data=f"al_{min(q,total)}"),
         InlineKeyboardButton(str(min(h,total)), callback_data=f"al_{min(h,total)}"),
         InlineKeyboardButton(f"All ({total})",  callback_data="al_0")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])


def kb_cancel():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]])

def kb_after_scrape():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Yes — Add them to a group!", callback_data="yes_add")],
        [InlineKeyboardButton("🏠 Back to Menu",               callback_data="main_menu")],
    ])

def kb_menu_only():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]])

def kb_accounts():
    sessions = list_sessions()
    current  = state["session_name"]
    rows = []
    for s in sessions:
        if s != current:
            rows.append([InlineKeyboardButton(f"🔄 Switch → {s}", callback_data=f"sw_{s}")])
    for s in sessions:
        if s != current:
            rows.append([InlineKeyboardButton(f"🗑 Delete: {s}", callback_data=f"del_{s}")])
    rows.append([InlineKeyboardButton("➕ Add / Login New Account", callback_data="add_account")])
    rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)


def kb_admin_panel():
    ks_label = "🔴 KILL SWITCH — Disable All Users" if not state["kill_switch"] else "🟢 RESTORE — Re-enable Users"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Manage Users",    callback_data="adm_users")],
        [InlineKeyboardButton("📋 Activity Logs",   callback_data="adm_logs")],
        [InlineKeyboardButton(ks_label,             callback_data="adm_kill")],
        [InlineKeyboardButton("🏠 Main Menu",        callback_data="main_menu")],
    ])


def kb_user_list(show_pending=True):
    """Build inline keyboard for user management."""
    users = load_users()
    rows  = []

    # Pending users first
    if show_pending:
        for uid_str, u in users.items():
            if u.get("status") == "pending" and not u.get("banned"):
                label = f"⏳ @{u['username'] or u['name'] or uid_str}"
                rows.append([
                    InlineKeyboardButton(label, callback_data="noop"),
                ])
                rows.append([
                    InlineKeyboardButton("✅ Approve", callback_data=f"apv_{uid_str}"),
                    InlineKeyboardButton("🚫 Ban",     callback_data=f"ban_{uid_str}"),
                ])

    # Approved users
    for uid_str, u in users.items():
        if u.get("status") == "approved" and not u.get("banned"):
            label = f"✅ @{u['username'] or u['name'] or uid_str}"
            rows.append([
                InlineKeyboardButton(label,      callback_data="noop"),
                InlineKeyboardButton("🚫 Ban",   callback_data=f"ban_{uid_str}"),
            ])

    # Banned users
    for uid_str, u in users.items():
        if u.get("banned"):
            label = f"🚫 @{u['username'] or u['name'] or uid_str}"
            rows.append([
                InlineKeyboardButton(label,        callback_data="noop"),
                InlineKeyboardButton("✅ Unban",   callback_data=f"unban_{uid_str}"),
            ])

    rows.append([InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_panel")])
    return InlineKeyboardMarkup(rows)


# ── Core Scraping ──────────────────────────────────────────────────────────────

async def do_scrape(entity, max_m, prog_msg, context, chat_id):
    members, offset, limit, total, last_edit = [], 0, 200, None, 0
    c = client()
    while True:
        try:
            p = await c(GetParticipantsRequest(
                entity, ChannelParticipantsSearch(""), offset=offset, limit=limit, hash=0,
            ))
        except errors.FloodWaitError as e:
            await asyncio.sleep(e.seconds + 2); continue
        except errors.ChatAdminRequiredError:
            return members, "❌ Admin rights needed to list members."
        except Exception as ex:
            return members, f"❌ Error: `{str(ex)[:120]}`"

        if not p.users: break
        if total is None:
            total = p.count or 1
            if max_m > 0: total = min(total, max_m)

        for user in p.users:
            if user.is_self: continue
            members.append(build_member(user))
            if max_m > 0 and len(members) >= max_m: break

        offset += len(p.users)
        now = time.time()
        if now - last_edit >= 3:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=prog_msg.message_id,
                    text=f"⏳ *Scraping...*\n\n`{pbar(len(members), total or 1)}`\n📥 *{len(members):,}* / *{total or '?'}* members\n\n_Please wait..._",
                    parse_mode=ParseMode.MARKDOWN,
                )
                last_edit = now
            except Exception: pass
        await asyncio.sleep(1)
        if max_m > 0 and len(members) >= max_m: break
        if total and offset >= total: break
    return members, None


# ── Core Adding ────────────────────────────────────────────────────────────────

async def do_add(target_entity, members, add_limit, add_delay, prog_msg, context, chat_id):
    to_add = members[:add_limit] if add_limit > 0 else members
    total, added, failed, last_edit = len(to_add), 0, 0, 0
    c = client()
    is_super = (hasattr(target_entity, "megagroup") or hasattr(target_entity, "gigagroup") or
                target_entity.__class__.__name__ in ("Channel", "ChannelFull"))

    for i, m in enumerate(to_add):
        uid = m["user_id"]
        try:
            ue = await c.get_entity(uid)
            if is_super:
                await c(InviteToChannelRequest(channel=target_entity, users=[ue]))
            else:
                await c(AddChatUserRequest(chat_id=target_entity.id, user_id=ue, fwd_limit=10))
            added += 1
        except errors.FloodWaitError as e:
            await asyncio.sleep(e.seconds + 2)
            try:
                ue = await c.get_entity(uid)
                if is_super: await c(InviteToChannelRequest(channel=target_entity, users=[ue]))
                else: await c(AddChatUserRequest(chat_id=target_entity.id, user_id=ue, fwd_limit=10))
                added += 1
            except Exception: failed += 1
        except errors.PeerFloodError:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=prog_msg.message_id,
                    text=f"⛔ *Telegram Rate Limit!*\n\nAdded *{added}* before stopped.\nWait 24 hours then retry.",
                    parse_mode=ParseMode.MARKDOWN, reply_markup=kb_menu_only(),
                )
            except Exception: pass
            return added, failed, "peer_flood"
        except errors.ChatAdminRequiredError:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=prog_msg.message_id,
                    text="❌ *Need admin rights* in the target group.",
                    parse_mode=ParseMode.MARKDOWN, reply_markup=kb_menu_only(),
                )
            except Exception: pass
            return added, failed, "no_admin"
        except (errors.UserPrivacyRestrictedError, errors.UserNotMutualContactError,
                errors.UserAlreadyParticipantError, errors.InputUserDeactivatedError,
                errors.UserBannedInChannelError, errors.UserKickedError):
            failed += 1
        except Exception:
            failed += 1

        now = time.time()
        if now - last_edit >= 3:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=prog_msg.message_id,
                    text=f"⏳ *Adding members...*\n\n`{pbar(i+1,total)}`\n✅ *{added}*  ❌ *{failed}*  📊 *{total}*\n\n_Almost there!_",
                    parse_mode=ParseMode.MARKDOWN,
                )
                last_edit = now
            except Exception: pass
        await asyncio.sleep(add_delay)
    return added, failed, "done"


# ── Commands ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    if not await check_access(update, context):
        return ConversationHandler.END

    c = client()
    if not c.is_connected(): await c.connect()
    authorized = await c.is_user_authorized()

    uid  = update.effective_user.id
    name = ""
    if authorized:
        me   = await c.get_me()
        safe_name = str(me.first_name or "").replace("*", "").replace("_", "").replace("`", "")
        safe_user = str(me.username or me.id).replace("*", "").replace("_", "").replace("`", "")
        name = f" • Scraping as: *{safe_name}* (@{safe_user})"

    sys_warn = "\n\n🔴 *System is in maintenance mode.*" if state["kill_switch"] and is_admin(uid) else ""

    text = f"👋 *TG Scraper Bot*{name}\n\nWhat would you like to do?{sys_warn}"

    if not authorized:
        text = "⚠️ *No account logged in!*\n\nTap *Accounts / Login* to connect a Telegram account first."

    kb = kb_main(uid)
    if update.message:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    elif update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        except Exception:
            await context.bot.send_message(update.effective_chat.id, text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    return ConversationHandler.END


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help user find their Telegram ID to set as admin."""
    uid = update.effective_user.id
    await update.message.reply_text(
        f"🆔 *Your Telegram User ID:*\n\n`{uid}`\n\n"
        f"Copy this and paste it into `config.ini` under `admin_ids = {uid}`\n"
        f"Then restart the bot to get admin access.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Cancelled.", reply_markup=kb_main(update.effective_user.id))
    return ConversationHandler.END


# ── Admin Panel ────────────────────────────────────────────────────────────────

async def show_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        if update.callback_query:
            await update.callback_query.answer("🚫 Admin only", show_alert=True)
        return

    users    = load_users()
    pending  = sum(1 for u in users.values() if u.get("status") == "pending" and not u.get("banned"))
    approved = sum(1 for u in users.values() if u.get("status") == "approved" and not u.get("banned"))
    banned   = sum(1 for u in users.values() if u.get("banned"))
    sys_icon = "🔴 KILLED" if state["kill_switch"] else "✅ ACTIVE"
    safe_session = clean_md(state['session_name'])

    text = (
        f"🛡️ *Admin Control Panel*\n\n"
        f"🔌 System: *{sys_icon}*\n\n"
        f"👥 *Users:*\n"
        f"  ⏳ Pending:  *{pending}*\n"
        f"  ✅ Approved: *{approved}*\n"
        f"  🚫 Banned:   *{banned}*\n"
        f"  Total:       *{len(users)}*\n\n"
        f"🛠️ Scraping account: *{safe_session}*"
    )

    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_admin_panel())
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_admin_panel())


# ── Main Callback Router ───────────────────────────────────────────────────────

async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    uid  = update.effective_user.id
    await q.answer()

    # ── Admin-only callbacks ────────────────────────────────────────────────────
    if data == "admin_panel":
        await show_admin_panel(update, context)
        return ConversationHandler.END

    elif data == "adm_users":
        if not is_admin(uid):
            return ConversationHandler.END
        users = load_users()
        if not users:
            await q.edit_message_text("👥 *No users yet.*", parse_mode=ParseMode.MARKDOWN, reply_markup=kb_admin_panel())
            return ConversationHandler.END
        pending  = sum(1 for u in users.values() if u.get("status") == "pending" and not u.get("banned"))
        approved = sum(1 for u in users.values() if u.get("status") == "approved" and not u.get("banned"))
        banned   = sum(1 for u in users.values() if u.get("banned"))
        await q.edit_message_text(
            f"👥 *User Management*\n\n"
            f"⏳ Pending: *{pending}*  ✅ Approved: *{approved}*  🚫 Banned: *{banned}*\n\n"
            f"_Tap a user to approve/ban:_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_user_list(),
        )
        return ConversationHandler.END

    elif data == "adm_logs":
        if not is_admin(uid):
            return ConversationHandler.END
        if not LOG_FILE.exists() or LOG_FILE.stat().st_size == 0:
            await q.edit_message_text("📋 *No activity logs yet.*", parse_mode=ParseMode.MARKDOWN, reply_markup=kb_admin_panel())
            return ConversationHandler.END
        # Send last 50 lines
        lines = LOG_FILE.read_text(encoding="utf-8").strip().split("\n")
        recent = "\n".join(lines[-50:])
        log_bytes = recent.encode("utf-8")
        await q.edit_message_text("📋 Sending logs...", parse_mode=ParseMode.MARKDOWN)
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=InputFile(io.BytesIO(log_bytes), filename="activity.log"),
            caption=f"📋 *Last {min(50, len(lines))} activity entries*",
            parse_mode=ParseMode.MARKDOWN,
        )
        await context.bot.send_message(update.effective_chat.id, "⬆️ Log file", reply_markup=kb_admin_panel())
        return ConversationHandler.END

    elif data == "adm_kill":
        if not is_admin(uid):
            return ConversationHandler.END
        state["kill_switch"] = not state["kill_switch"]
        status = "🔴 *KILL SWITCH ACTIVATED*\n\nAll user operations are now disabled." if state["kill_switch"] \
            else "🟢 *System RESTORED*\n\nAll users can operate normally again."
        await q.edit_message_text(status, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_admin_panel())
        log_activity(uid, update.effective_user.username or "", f"Kill switch → {state['kill_switch']}")
        return ConversationHandler.END

    elif data.startswith("apv_"):
        if not is_admin(uid):
            return ConversationHandler.END
        target_uid = int(data[4:])
        approve_user(target_uid)
        await notify_user_approved(context, target_uid)
        log_activity(uid, update.effective_user.username or "", f"Approved user {target_uid}")
        await q.edit_message_text(f"✅ *User {target_uid} approved!*", parse_mode=ParseMode.MARKDOWN, reply_markup=kb_admin_panel())
        return ConversationHandler.END

    elif data.startswith("ban_"):
        if not is_admin(uid):
            return ConversationHandler.END
        target_uid = int(data[4:])
        ban_user(target_uid)
        await notify_user_banned(context, target_uid)
        log_activity(uid, update.effective_user.username or "", f"Banned user {target_uid}")
        await q.edit_message_text(f"🚫 *User {target_uid} banned!*", parse_mode=ParseMode.MARKDOWN, reply_markup=kb_admin_panel())
        return ConversationHandler.END

    elif data.startswith("unban_"):
        if not is_admin(uid):
            return ConversationHandler.END
        target_uid = int(data[6:])
        unban_user(target_uid)
        await notify_user_approved(context, target_uid)
        log_activity(uid, update.effective_user.username or "", f"Unbanned user {target_uid}")
        await q.edit_message_text(f"✅ *User {target_uid} unbanned!*", parse_mode=ParseMode.MARKDOWN, reply_markup=kb_admin_panel())
        return ConversationHandler.END

    elif data == "noop":
        return ConversationHandler.END

    # ── Access check for user operations ───────────────────────────────────────
    if data not in ("main_menu", "help", "accounts", "add_account", "cancel") and not data.startswith(("sw_", "del_")):
        if not await check_access(update, context):
            return ConversationHandler.END

    # ── Navigation ──────────────────────────────────────────────────────────────
    if data == "main_menu":
        return await cmd_start(update, context)

    elif data == "cancel":
        context.user_data.clear()
        await q.edit_message_text("❌ Cancelled.", reply_markup=kb_main(uid))
        return ConversationHandler.END

    elif data == "help":
        await q.edit_message_text(
            "ℹ️ *How to Use TG Scraper Bot*\n\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "🔍 *Scrape Only* — grabs member list → sends CSV + TXT\n\n"
            "🔍➕ *Scrape & Add* — scrapes Group A, adds to Group B\n\n"
            "📁 *Last Results* — re-download your latest scrape\n\n"
            "👤 *Accounts* — login or switch Telegram accounts\n\n"
            "🛡️ *Admin Panel* — _(admin only)_ manage users & system\n\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "📌 *Group formats:*\n"
            "• `@username`\n"
            "• `https://t.me/groupname`\n"
            "• `https://t.me/+invitelink`\n\n"
            "⚠️ Add limit: ~50/day per account\n"
            "If adds fail → message `@SpamBot`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_menu_only(),
        )
        return ConversationHandler.END

    elif data == "last_results":
        csv_files = sorted(OUTPUT_DIR.glob("*.csv"))
        if not csv_files:
            await q.edit_message_text("📁 *No results yet!* Run a scrape first.", parse_mode=ParseMode.MARKDOWN, reply_markup=kb_menu_only())
            return ConversationHandler.END
        await q.edit_message_text("📤 Sending latest files...", parse_mode=ParseMode.MARKDOWN)
        latest = csv_files[-1]
        with open(latest, "rb") as f:
            await context.bot.send_document(update.effective_chat.id, InputFile(f, filename=latest.name),
                                             caption=f"📊 *{latest.stem}*", parse_mode=ParseMode.MARKDOWN)
        txt = latest.with_suffix(".txt")
        if txt.exists():
            with open(txt, "rb") as f:
                await context.bot.send_document(update.effective_chat.id, InputFile(f, filename=txt.name),
                                                 caption="📝 *Usernames TXT*", parse_mode=ParseMode.MARKDOWN)
        await context.bot.send_message(update.effective_chat.id, "✅ Done!", reply_markup=kb_main(uid))
        return ConversationHandler.END

    # ── Accounts ────────────────────────────────────────────────────────────────
    elif data == "accounts":
        sessions = list_sessions()
        current  = state["session_name"]
        me_name  = "Not logged in"
        c = client()
        if c.is_connected() and await c.is_user_authorized():
            me = await c.get_me()
            safe_name = clean_md(me.first_name)
            safe_user = clean_md(me.username or me.id)
            me_name = f"{safe_name} (@{safe_user})"
        text = (
            f"👤 *Account Manager*\n\n"
            f"✅ *Active:* {me_name}\n💾 *Session:* `{current}`\n\n"
        )
        if sessions:
            text += "*Saved sessions:*\n" + "\n".join(f"  {'✅' if s==current else '○'} `{s}`" for s in sessions)
        await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_accounts())
        return ConversationHandler.END

    elif data == "add_account":
        await q.edit_message_text(
            "➕ *Login New Account*\n\n*Step 1:* Send your phone number with country code:\n\nExample: `+1 234 567 8900`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_cancel(),
        )
        return WAIT_PHONE

    elif data.startswith("sw_"):
        new_name = data[3:]
        await q.edit_message_text(f"🔄 Switching to `{new_name}`...", parse_mode=ParseMode.MARKDOWN)
        try:
            await switch_session(new_name)
            c = client()
            me_name = "Not authorized"
            if await c.is_user_authorized():
                me = await c.get_me()
                me_name = f"{me.first_name} (@{me.username or me.id})"
            await q.edit_message_text(f"✅ *Switched!*\n\nNow using: *{me_name}*", parse_mode=ParseMode.MARKDOWN, reply_markup=kb_menu_only())
        except Exception as ex:
            await q.edit_message_text(f"❌ Failed: `{ex}`", parse_mode=ParseMode.MARKDOWN, reply_markup=kb_menu_only())
        return ConversationHandler.END

    elif data.startswith("del_"):
        name = data[4:]
        for ext in (".session", ".session-journal"):
            p = BASE_DIR / f"{name}{ext}"
            if p.exists(): p.unlink()
        await q.edit_message_text(f"🗑 *Deleted:* `{name}`", parse_mode=ParseMode.MARKDOWN, reply_markup=kb_menu_only())
        return ConversationHandler.END

    # ── Scrape flow ─────────────────────────────────────────────────────────────
    elif data in ("scrape_only", "scrape_add"):
        c = client()
        if not c.is_connected() or not await c.is_user_authorized():
            await q.edit_message_text("⚠️ *Not logged in!*\n\nGo to 👤 Accounts to log in first.", parse_mode=ParseMode.MARKDOWN, reply_markup=kb_menu_only())
            return ConversationHandler.END
        context.user_data["mode"] = data
        await q.edit_message_text(
            "🔍 *Step 1 of 3 — Source Group*\n\nSend the *username or link* of the group to scrape:\n\n"
            "• `@groupname`\n• `https://t.me/groupname`\n• `https://t.me/+invitelink`\n\n"
            "_(You must be a member)_",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_cancel(),
        )
        return WAIT_SOURCE

    elif data.startswith("max_"):
        return await _run_max(update, context, int(data[4:]))

    elif data.startswith("al_"):
        return await _run_add_limit(update, context, int(data[3:]))

    elif data == "yes_add":
        await q.edit_message_text(
            "➕ *Step 3 of 3 — Target Group*\n\nSend the *username or link* of the group to ADD members INTO:\n\n_(You must be an *admin* there!)_",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_cancel(),
        )
        return WAIT_TARGET

    elif data == "no_add":
        context.user_data.clear()
        await q.edit_message_text("✅ All done! Files saved.", reply_markup=kb_main(uid))
        return ConversationHandler.END

    return ConversationHandler.END


# ── Login Flow ─────────────────────────────────────────────────────────────────

async def recv_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    msg   = await update.message.reply_text("📲 Sending code...", parse_mode=ParseMode.MARKDOWN)
    tmp_s = re.sub(r"[^\w]", "_", phone)
    tmp_c = TelegramClient(str(BASE_DIR / f"tmp_{tmp_s}"), API_ID, API_HASH)
    try:
        await tmp_c.connect()
        r = await tmp_c.send_code_request(phone)
        context.user_data.update({"login_phone": phone, "login_hash": r.phone_code_hash,
                                   "login_session": tmp_s, "login_client": tmp_c})
        await msg.edit_text(f"✅ *Code sent to {phone}!*\n\n*Step 2:* Enter the code from your Telegram app:\n_(digits only, e.g. `12345`)_",
                            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_cancel())
        return WAIT_CODE
    except errors.PhoneNumberInvalidError:
        await tmp_c.disconnect()
        await msg.edit_text("❌ *Invalid number!*\nInclude country code, e.g. `+1 234 567 8900`", parse_mode=ParseMode.MARKDOWN, reply_markup=kb_cancel())
        return WAIT_PHONE
    except Exception as ex:
        await tmp_c.disconnect()
        await msg.edit_text(f"❌ Error: `{str(ex)[:120]}`\n\nTry again:", parse_mode=ParseMode.MARKDOWN, reply_markup=kb_cancel())
        return WAIT_PHONE


async def recv_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip().replace(" ", "")
    msg  = await update.message.reply_text("🔐 Verifying...", parse_mode=ParseMode.MARKDOWN)
    try:
        await context.user_data["login_client"].sign_in(
            context.user_data["login_phone"], code,
            phone_code_hash=context.user_data["login_hash"]
        )
        me = await context.user_data["login_client"].get_me()
        context.user_data["login_me"] = me
        safe_name = clean_md(me.first_name)
        await msg.edit_text(f"✅ *Logged in as {safe_name}!*\n\n*Step 3:* Give this session a name:\n_(e.g. `account1`, `client`) or send `skip`_",
                            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_cancel())
        return WAIT_SESSION_NAME
    except errors.SessionPasswordNeededError:
        await msg.edit_text("🔑 *2FA required.*\n\nSend your Telegram password:", parse_mode=ParseMode.MARKDOWN, reply_markup=kb_cancel())
        return WAIT_2FA
    except errors.PhoneCodeInvalidError:
        await msg.edit_text("❌ *Wrong code!* Try again:", parse_mode=ParseMode.MARKDOWN, reply_markup=kb_cancel())
        return WAIT_CODE
    except errors.PhoneCodeExpiredError:
        await context.user_data["login_client"].disconnect()
        context.user_data.clear()
        await msg.edit_text("❌ *Code expired.* Please start over.", parse_mode=ParseMode.MARKDOWN, reply_markup=kb_menu_only())
        return ConversationHandler.END
    except Exception as ex:
        await msg.edit_text(f"❌ Error: `{str(ex)[:120]}`", parse_mode=ParseMode.MARKDOWN, reply_markup=kb_menu_only())
        return ConversationHandler.END


async def recv_2fa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    try: await update.message.delete()
    except Exception: pass
    msg = await context.bot.send_message(update.effective_chat.id, "🔐 Checking password...")
    try:
        await context.user_data["login_client"].sign_in(password=password)
        me = await context.user_data["login_client"].get_me()
        context.user_data["login_me"] = me
        safe_name = clean_md(me.first_name)
        await msg.edit_text(f"✅ *Password accepted! Logged in as {safe_name}.*\n\nGive this session a name _(or `skip`)_:",
                            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_cancel())
        return WAIT_SESSION_NAME
    except errors.PasswordHashInvalidError:
        await msg.edit_text("❌ *Wrong password!* Try again:", parse_mode=ParseMode.MARKDOWN, reply_markup=kb_cancel())
        return WAIT_2FA
    except Exception as ex:
        await msg.edit_text(f"❌ Error: `{str(ex)[:120]}`", parse_mode=ParseMode.MARKDOWN, reply_markup=kb_menu_only())
        return ConversationHandler.END


async def recv_session_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw  = update.message.text.strip()
    me   = context.user_data["login_me"]
    tmp_c = context.user_data["login_client"]
    tmp_s = context.user_data["login_session"]
    final = f"account_{me.id}" if raw.lower() == "skip" else re.sub(r"[^\w\-]", "_", raw)[:30]
    msg  = await update.message.reply_text("💾 Saving...", parse_mode=ParseMode.MARKDOWN)
    await tmp_c.disconnect()
    old = BASE_DIR / f"tmp_{tmp_s}.session"
    new = BASE_DIR / f"{final}.session"
    try:
        if old.exists(): old.rename(new)
        await switch_session(final)
        context.user_data.clear()
        uid = update.effective_user.id
        safe_name = clean_md(me.first_name)
        safe_user = clean_md(me.username or "N/A")
        await msg.edit_text(
            f"🎉 *Account saved & active!*\n\n👤 *{safe_name}*  @{safe_user}\n💾 Session: `{final}`\n\nReady to scrape!",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main(uid),
        )
    except Exception as ex:
        await msg.edit_text(f"❌ Error: `{ex}`", parse_mode=ParseMode.MARKDOWN, reply_markup=kb_menu_only())
    return ConversationHandler.END


# ── Scrape / Add Flow ──────────────────────────────────────────────────────────

async def recv_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update, context): return ConversationHandler.END
    inp = update.message.text.strip()
    msg = await update.message.reply_text("🔍 Looking up group...", parse_mode=ParseMode.MARKDOWN)
    try:
        entity = await client().get_entity(inp)
        title  = getattr(entity, "title", inp)
        count  = getattr(entity, "participants_count", "?")
        context.user_data.update({"entity": entity, "source_title": title})
    except Exception as ex:
        await msg.edit_text(f"❌ *Group not found!*\n\n`{str(ex)[:120]}`\n\nMake sure you're a member. Try again 👇",
                            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_cancel())
        return WAIT_SOURCE
    await msg.edit_text(
        f"✅ *Found:* *{title}*\n👥 Members: *{f'{count:,}' if isinstance(count, int) else count}*\n\n*Step 2 of 3 — How many to scrape?*",
        parse_mode=ParseMode.MARKDOWN, reply_markup=kb_max(),
    )
    return WAIT_MAX


async def recv_max(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update, context): return ConversationHandler.END
    try:
        val = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Enter a valid number.", reply_markup=kb_max())
        return WAIT_MAX
    return await _run_max(update, context, val)


async def _run_max(update: Update, context: ContextTypes.DEFAULT_TYPE, val: int):
    context.user_data["max_members"] = val
    entity  = context.user_data["entity"]
    title   = context.user_data["source_title"]
    chat_id = update.effective_chat.id
    uid     = update.effective_user.id
    init    = f"⏳ *Starting scrape...*\n\n📌 *{title}*\n🎯 *{'all' if val==0 else f'{val:,}'}* members\n\n`{'░'*18}  0%`"
    prog    = (await update.callback_query.edit_message_text(init, parse_mode=ParseMode.MARKDOWN)
               if update.callback_query
               else await update.message.reply_text(init, parse_mode=ParseMode.MARKDOWN))

    members, err = await do_scrape(entity, val, prog, context, chat_id)
    context.user_data["members"] = members
    log_activity(uid, update.effective_user.username or "", f"Scraped {len(members)} from '{title}'")

    if err or not members:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=prog.message_id,
                                             text=err or "⚠️ No members found.", parse_mode=ParseMode.MARKDOWN, reply_markup=kb_menu_only())
        return ConversationHandler.END

    safe = re.sub(r"[^\w\-]", "_", title)[:30]
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_data, txt_data = to_csv(members), to_txt(members)
    (OUTPUT_DIR / f"{safe}_{ts}.csv").write_bytes(csv_data)
    (OUTPUT_DIR / f"{safe}_{ts}.txt").write_bytes(txt_data)
    unames = sum(1 for m in members if m["username"])

    await context.bot.edit_message_text(chat_id=chat_id, message_id=prog.message_id,
                                         text=f"✅ *Done!* Sending files...", parse_mode=ParseMode.MARKDOWN)
    await context.bot.send_document(chat_id, InputFile(io.BytesIO(csv_data), filename=f"{safe}_{ts}.csv"),
                                     caption=f"📊 *Full CSV* — {len(members):,} members", parse_mode=ParseMode.MARKDOWN)
    await context.bot.send_document(chat_id, InputFile(io.BytesIO(txt_data), filename=f"{safe}_{ts}.txt"),
                                     caption=f"📝 *Usernames* — {unames:,} @handles", parse_mode=ParseMode.MARKDOWN)

    if context.user_data.get("mode") == "scrape_add":
        await context.bot.send_message(chat_id, f"🎯 *{len(members):,} members ready!*\n\nAdd them to a group?",
                                        parse_mode=ParseMode.MARKDOWN, reply_markup=kb_after_scrape())
    else:
        await context.bot.send_message(chat_id, "🎉 *All done!*", reply_markup=kb_main(uid))
    return ConversationHandler.END


async def recv_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update, context): return ConversationHandler.END
    inp = update.message.text.strip()
    msg = await update.message.reply_text("🔍 Looking up target...", parse_mode=ParseMode.MARKDOWN)
    try:
        entity = await client().get_entity(inp)
        title  = getattr(entity, "title", inp)
        context.user_data.update({"target_entity": entity, "target_title": title})
    except Exception as ex:
        await msg.edit_text(f"❌ *Not found!*\n\n`{str(ex)[:120]}`\n\nTry again:", parse_mode=ParseMode.MARKDOWN, reply_markup=kb_cancel())
        return WAIT_TARGET
    members = context.user_data.get("members", [])
    await msg.edit_text(f"✅ *Target:* *{title}*\n\n👥 Ready: *{len(members):,}*\n\n⚠️ *~50 adds/day max!* How many now?",
                        parse_mode=ParseMode.MARKDOWN, reply_markup=kb_add_limit(len(members)))
    return WAIT_ADD_LIMIT


async def recv_add_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update, context): return ConversationHandler.END
    try:
        val = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Enter a number.", reply_markup=kb_cancel())
        return WAIT_ADD_LIMIT
    return await _run_add_limit(update, context, val)


async def _run_add_limit(update: Update, context: ContextTypes.DEFAULT_TYPE, val: int):
    chat_id = update.effective_chat.id
    uid     = update.effective_user.id
    members = context.user_data.get("members", [])
    te      = context.user_data["target_entity"]
    tt      = context.user_data["target_title"]
    init    = f"⏳ *Adding members...*\n\n🎯 *{tt}*\n👥 *{'all' if val==0 else val}*\n\n`{'░'*18}  0%`"
    prog    = (await update.callback_query.edit_message_text(init, parse_mode=ParseMode.MARKDOWN)
               if update.callback_query
               else await update.message.reply_text(init, parse_mode=ParseMode.MARKDOWN))

    added, failed, status = await do_add(te, members, val, 2, prog, context, chat_id)
    log_activity(uid, update.effective_user.username or "", f"Added {added} to '{tt}' ({failed} failed)")

    if status in ("peer_flood", "no_admin"):
        return ConversationHandler.END

    await context.bot.edit_message_text(
        chat_id=chat_id, message_id=prog.message_id,
        text=f"🎉 *Done!*\n\n🎯 *{tt}*\n✅ Added: *{added}*\n❌ Skipped: *{failed}*\n\n_Skipped = privacy, already member, or deleted. Normal!_",
        parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main(uid),
    )
    context.user_data.clear()
    return ConversationHandler.END


# ── Startup / Shutdown ─────────────────────────────────────────────────────────

async def post_init(app):
    c = client()
    await c.connect()
    if await c.is_user_authorized():
        me = await c.get_me()
        print(f"✅ Scraping account: {me.first_name} (@{me.username})")
    else:
        print("⚠️  No session — users can log in via 👤 Accounts in the bot.")
    if ADMIN_IDS:
        print(f"🛡️  Admins: {ADMIN_IDS}")
    else:
        print("⚠️  No admin_ids set in config.ini — message the bot and run /myid to find your ID!")


async def post_shutdown(app):
    c = client()
    if c.is_connected():
        await c.disconnect()


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start), CallbackQueryHandler(cb_handler)],
        states={
            WAIT_SOURCE:       [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_source),
                                 CallbackQueryHandler(cb_handler, pattern="^cancel$")],
            WAIT_MAX:          [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_max),
                                 CallbackQueryHandler(cb_handler, pattern="^(max_|cancel)")],
            WAIT_TARGET:       [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_target),
                                 CallbackQueryHandler(cb_handler, pattern="^cancel$")],
            WAIT_ADD_LIMIT:    [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_add_limit),
                                 CallbackQueryHandler(cb_handler, pattern="^(al_|cancel)")],
            WAIT_PHONE:        [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_phone),
                                 CallbackQueryHandler(cb_handler, pattern="^cancel$")],
            WAIT_CODE:         [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_code),
                                 CallbackQueryHandler(cb_handler, pattern="^cancel$")],
            WAIT_2FA:          [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_2fa),
                                 CallbackQueryHandler(cb_handler, pattern="^cancel$")],
            WAIT_SESSION_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_session_name),
                                 CallbackQueryHandler(cb_handler, pattern="^cancel$")],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel), CommandHandler("start", cmd_start),
                   CallbackQueryHandler(cb_handler, pattern="^cancel$")],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("myid",  cmd_myid))
    app.add_handler(CommandHandler("admin", show_admin_panel))

    print("\n🤖 TG Scraper Bot is running!")
    print("📱 Telegram → @tg_scrape_tool_bot → /start")
    print("🆔 Run /myid in the bot to get your admin ID")
    print("⌨️  Ctrl+C to stop.\n")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

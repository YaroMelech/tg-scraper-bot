#!/usr/bin/env python3
"""
🤖 TG Scraper Bot — Professional Edition
  ✅ Scrape members with activity filters
  ✅ Add members with smart retry + contact-import fallback
  ✅ Multi-account session management
  ✅ Admin panel with user approval, ban, kill switch, logs
"""

import asyncio
import configparser
import csv
import html
import io
import json
import logging
import re
import random
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
from telethon.tl.functions.contacts import ImportContactsRequest, DeleteContactsRequest, AddContactRequest
from telethon.tl.functions.messages import AddChatUserRequest
from telethon.tl.types import (
    Channel, Chat,
    ChannelParticipantsSearch,
    InputPhoneContact,
    UserStatusOnline, UserStatusRecently,
    UserStatusLastWeek, UserStatusLastMonth,
    UserStatusOffline,
)

# ── Conversation States ────────────────────────────────────────────────────────
WAIT_SOURCE, WAIT_FILTER, WAIT_MAX, WAIT_TARGET, WAIT_ADD_LIMIT = range(5)
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

_raw_admins = cfg.get("telegram", "admin_ids", fallback="").strip()
ADMIN_IDS: set[int] = {int(x.strip()) for x in _raw_admins.split(",") if x.strip().isdigit()}

# ── Global State ───────────────────────────────────────────────────────────────
state = {
    "client":       TelegramClient(str(BASE_DIR / DEFAULT_SESSION), API_ID, API_HASH),
    "session_name": DEFAULT_SESSION,
    "kill_switch":  False,
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
    if is_admin(uid): return "admin"
    u = load_users().get(str(uid), {})
    if u.get("banned"): return "banned"
    return u.get("status", "pending")

def is_approved(uid: int) -> bool:
    return get_status(uid) in ("admin", "approved")

def register_user(uid: int, username: str, name: str) -> bool:
    users = load_users()
    if str(uid) in users:
        return False
    users[str(uid)] = {
        "username": username or "", "name": name or "",
        "status": "pending", "banned": False,
        "joined": datetime.now(timezone.utc).isoformat(),
        "last_action": "",
    }
    save_users(users)
    return True

def approve_user(uid: int):
    users = load_users()
    if str(uid) not in users:
        users[str(uid)] = {"username":"","name":"","joined":"","banned":False}
    users[str(uid)].update({"status":"approved","banned":False})
    save_users(users)

def ban_user(uid: int):
    users = load_users()
    if str(uid) not in users:
        users[str(uid)] = {"status":"pending","username":"","name":"","joined":""}
    users[str(uid)]["banned"] = True
    save_users(users)

def unban_user(uid: int):
    users = load_users()
    if str(uid) in users:
        users[str(uid)].update({"banned":False,"status":"approved"})
    save_users(users)

def log_activity(uid: int, username: str, action: str):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] ID:{uid} @{username or 'N/A'}: {action}\n"
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line)
    users = load_users()
    if str(uid) in users:
        users[str(uid)]["last_action"] = f"{action} at {ts}"
        save_users(users)

async def notify_admins(context: ContextTypes.DEFAULT_TYPE, uid: int, username: str, name: str):
    if not ADMIN_IDS: return
    text = (
        f"🔔 <b>New access request!</b>\n\n"
        f"👤 Name: <b>{h(name)}</b>\n"
        f"📱 Username: @{h(username or 'N/A')}\n"
        f"🆔 ID: <code>{uid}</code>\n\n"
        f"Approve or ban?"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"apv_{uid}"),
        InlineKeyboardButton("🚫 Ban",     callback_data=f"ban_{uid}"),
    ]])
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception:
            pass

async def notify_user_approved(context: ContextTypes.DEFAULT_TYPE, uid: int):
    try:
        await context.bot.send_message(uid,
            "🎉 <b>Your access has been approved!</b>\n\nSend /start to begin.",
            parse_mode=ParseMode.HTML)
    except Exception: pass

async def notify_user_banned(context: ContextTypes.DEFAULT_TYPE, uid: int):
    try:
        await context.bot.send_message(uid,
            "🚫 <b>Your access has been revoked.</b>\n\nContact the admin.",
            parse_mode=ParseMode.HTML)
    except Exception: pass

async def check_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user     = update.effective_user
    uid      = user.id
    username = user.username or ""
    name     = user.full_name or ""
    is_new   = register_user(uid, username, name)
    status   = get_status(uid)

    if state["kill_switch"] and status != "admin":
        msg = "🔴 <b>System is temporarily offline.</b>\n\nPlease try again later."
        if update.message:
            await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        elif update.callback_query:
            await update.callback_query.answer("🔴 System offline", show_alert=True)
        return False

    if status == "banned":
        msg = "🚫 <b>Your access has been revoked.</b>\n\nContact the admin."
        if update.message:
            await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        elif update.callback_query:
            await update.callback_query.answer("🚫 Access revoked", show_alert=True)
        return False

    if status == "pending":
        if is_new: await notify_admins(context, uid, username, name)
        msg = "⏳ <b>Awaiting admin approval.</b>\n\nYour request was sent. You'll get a message when approved."
        if not ADMIN_IDS:
            msg += "\n\n⚠️ <i>No admin configured yet. The bot owner must set admin_ids in config.ini first.</i>"
        if update.message:
            await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        elif update.callback_query:
            await update.callback_query.answer("⏳ Awaiting approval", show_alert=True)
        return False

    return True

# ── Helpers ────────────────────────────────────────────────────────────────────

def h(text) -> str:
    """HTML-escape a value safely."""
    return html.escape(str(text or ""))

def pbar(curr: int, total: int, width: int = 20) -> str:
    if total <= 0: return "░" * width + "  0%"
    pct    = min(curr / total, 1.0)
    filled = int(width * pct)
    return f"{'█'*filled}{'░'*(width-filled)}  {int(pct*100)}%"

def status_label(s) -> str:
    if isinstance(s, UserStatusOnline):      return "🟢 Online"
    elif isinstance(s, UserStatusRecently):  return "🔵 Recently"
    elif isinstance(s, UserStatusLastWeek):  return "🟡 Last Week"
    elif isinstance(s, UserStatusLastMonth): return "🟠 Last Month"
    elif isinstance(s, UserStatusOffline):   return "⚫ Offline"
    else:                                     return "❓ Unknown"

# Activity filter mapping
FILTER_TYPES = {
    "recent": (UserStatusOnline, UserStatusRecently),
    "week":   (UserStatusOnline, UserStatusRecently, UserStatusLastWeek),
    "month":  (UserStatusOnline, UserStatusRecently, UserStatusLastWeek, UserStatusLastMonth),
    "all":    None,
}
FILTER_LABELS = {
    "recent": "🟢 Recently Active",
    "week":   "🟡 Active Last Week",
    "month":  "🟠 Active Last Month",
    "all":    "♾️ Everyone",
}

def passes_filter(user, filter_key: str) -> bool:
    """Return True if user matches the activity filter. Always skips bots."""
    if user.bot: return False
    if user.is_self: return False
    allowed = FILTER_TYPES.get(filter_key)
    if allowed is None: return True  # "all"
    return isinstance(user.status, allowed)

def build_member(user) -> dict:
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

def to_csv(members: list) -> bytes:
    buf = io.StringIO()
    fields = ["user_id","username","first_name","last_name","phone","status","is_bot","is_premium","scraped_at"]
    w = csv.DictWriter(buf, fieldnames=fields)
    w.writeheader(); w.writerows(members)
    return buf.getvalue().encode("utf-8")

def to_txt(members: list) -> bytes:
    return "\n".join(f"@{m['username']}" for m in members if m["username"]).encode("utf-8")

# ── Keyboards ──────────────────────────────────────────────────────────────────

def kb_main(uid: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🔍  Scrape Members",           callback_data="scrape_only")],
        [InlineKeyboardButton("🚀  Scrape + Add to Group",    callback_data="scrape_add")],
        [InlineKeyboardButton("📁  Download Last Results",    callback_data="last_results")],
        [InlineKeyboardButton("👤  Accounts",                  callback_data="accounts")],
        [InlineKeyboardButton("ℹ️   Help",                    callback_data="help")],
    ]
    if is_admin(uid):
        rows.insert(0, [InlineKeyboardButton("🛡️  Admin Panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(rows)

def kb_filter() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Recently Active  ← Recommended", callback_data="flt_recent")],
        [InlineKeyboardButton("🟡 Active Last Week",                callback_data="flt_week")],
        [InlineKeyboardButton("🟠 Active Last Month",               callback_data="flt_month")],
        [InlineKeyboardButton("♾️  Everyone (no filter)",           callback_data="flt_all")],
        [InlineKeyboardButton("❌ Cancel",                           callback_data="cancel")],
    ])

def kb_max() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("100",   callback_data="max_100"),
         InlineKeyboardButton("500",   callback_data="max_500"),
         InlineKeyboardButton("1 000", callback_data="max_1000")],
        [InlineKeyboardButton("5 000", callback_data="max_5000"),
         InlineKeyboardButton("♾️ All", callback_data="max_0")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])

def kb_add_limit(total: int) -> InlineKeyboardMarkup:
    opts = [10, 20, 50]
    rows = [[InlineKeyboardButton(str(n), callback_data=f"al_{n}") for n in opts if n <= total]]
    rows.append([InlineKeyboardButton(f"All {total}", callback_data="al_0")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)

def kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]])

def kb_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Back to Menu", callback_data="main_menu")]])

def kb_after_scrape() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Yes — Add them to a group now!", callback_data="yes_add")],
        [InlineKeyboardButton("🏠 No, back to menu",               callback_data="main_menu")],
    ])

def kb_accounts() -> InlineKeyboardMarkup:
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
    rows.append([InlineKeyboardButton("🏠 Back to Menu",            callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)

def kb_admin() -> InlineKeyboardMarkup:
    ks_label = ("🔴 Kill Switch — Disable All Users"
                if not state["kill_switch"] else
                "🟢 Restore — Re-enable All Users")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Manage Users",  callback_data="adm_users")],
        [InlineKeyboardButton("📋 Activity Logs", callback_data="adm_logs")],
        [InlineKeyboardButton(ks_label,           callback_data="adm_kill")],
        [InlineKeyboardButton("🏠 Back to Menu",  callback_data="main_menu")],
    ])

def kb_user_list() -> InlineKeyboardMarkup:
    users = load_users()
    rows  = []
    for uid_str, u in users.items():
        if u.get("status") == "pending" and not u.get("banned"):
            name = h(u.get("username") or u.get("name") or uid_str)
            rows.append([InlineKeyboardButton(f"⏳ @{name}", callback_data="noop")])
            rows.append([
                InlineKeyboardButton("✅ Approve", callback_data=f"apv_{uid_str}"),
                InlineKeyboardButton("🚫 Ban",     callback_data=f"ban_{uid_str}"),
            ])
    for uid_str, u in users.items():
        if u.get("status") == "approved" and not u.get("banned"):
            name = h(u.get("username") or u.get("name") or uid_str)
            rows.append([
                InlineKeyboardButton(f"✅ @{name}", callback_data="noop"),
                InlineKeyboardButton("🚫 Ban",      callback_data=f"ban_{uid_str}"),
            ])
    for uid_str, u in users.items():
        if u.get("banned"):
            name = h(u.get("username") or u.get("name") or uid_str)
            rows.append([
                InlineKeyboardButton(f"🚫 @{name}", callback_data="noop"),
                InlineKeyboardButton("✅ Unban",    callback_data=f"unban_{uid_str}"),
            ])
    rows.append([InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_panel")])
    return InlineKeyboardMarkup(rows)

# ── Core Scraping ──────────────────────────────────────────────────────────────

async def do_scrape(entity, max_m: int, filter_key: str, prog_msg, context, chat_id) -> tuple[list, str | None]:
    members, offset, limit, total, last_edit = [], 0, 200, None, 0
    c = client()

    while True:
        try:
            p = await c(GetParticipantsRequest(
                entity, ChannelParticipantsSearch(""), offset=offset, limit=limit, hash=0,
            ))
        except errors.FloodWaitError as e:
            await asyncio.sleep(e.seconds + 2)
            continue
        except errors.ChatAdminRequiredError:
            return members, "❌ <b>Admin rights required</b> to view members of this group."
        except Exception as ex:
            return members, f"❌ <b>Error:</b> <code>{h(str(ex)[:200])}</code>"

        if not p.users: break

        if total is None:
            total = p.count or 1
            if max_m > 0: total = min(total, max_m)

        for user in p.users:
            if not passes_filter(user, filter_key):
                continue
            members.append(build_member(user))
            if max_m > 0 and len(members) >= max_m:
                break

        offset += len(p.users)

        now = time.time()
        if now - last_edit >= 3:
            try:
                filter_label = FILTER_LABELS.get(filter_key, "")
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=prog_msg.message_id,
                    parse_mode=ParseMode.HTML,
                    text=(
                        f"⏳ <b>Scraping in progress…</b>\n\n"
                        f"<code>{pbar(len(members), total or 1)}</code>\n"
                        f"📥 <b>{len(members):,}</b> collected  |  Filter: {filter_label}\n\n"
                        f"<i>Please wait, this may take a minute…</i>"
                    ),
                )
                last_edit = now
            except Exception: pass

        await asyncio.sleep(0.5)
        if max_m > 0 and len(members) >= max_m: break
        if total and offset >= total: break

    return members, None


# ── Core Adding ────────────────────────────────────────────────────────────────

async def try_import_contact(c: TelegramClient, m: dict) -> bool:
    """Try to import user as contact using phone number (fallback for mutual-contact errors)."""
    phone = m.get("phone", "")
    if not phone: return False
    try:
        await c(ImportContactsRequest([InputPhoneContact(
            client_id=m["user_id"],
            phone=phone,
            first_name=m.get("first_name") or "User",
            last_name=m.get("last_name") or "",
        )]))
        return True
    except Exception:
        return False

async def _cleanup_contacts(c: TelegramClient, user_ids: list):
    """Delete a list of user IDs from contacts (cleanup after contact trick)."""
    if not user_ids:
        return
    try:
        input_users = []
        for uid in user_ids:
            try:
                input_users.append(await c.get_input_entity(uid))
            except Exception:
                pass
        if input_users:
            await c(DeleteContactsRequest(id=input_users))
    except Exception:
        pass


async def do_add(target_entity, members: list, add_limit: int, prog_msg, context, chat_id) -> dict:
    """
    Add members using the CONTACT TRICK:
      Phase 1 — Add member as a contact (works via ID/username, NO PHONE REQUIRED)
      Phase 2 — Add each member to the group (now treated as contacts, much less restricted)
      Phase 3 — Bulk delete imported contacts (cleanup)
    """
    to_add          = members[:add_limit] if add_limit > 0 else members
    total           = len(to_add)
    c               = client()
    is_super        = isinstance(target_entity, Channel)
    imported_ids    = []   # track who we imported so we can clean up
    stats           = {"added": 0, "privacy": 0, "already_in": 0,
                       "deactivated": 0, "contact_needed": 0, "other": 0}
    last_edit       = 0

    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=prog_msg.message_id,
            parse_mode=ParseMode.HTML,
            text=f"📇 <b>Preparing…</b>\n\n<i>Adding users as temporary contacts to bypass Telegram restrictions...</i>",
        )
    except Exception:
        pass

    # ── Add each member ──────────────────────────────────────────────
    for i, m in enumerate(to_add):
        uid = m["user_id"]

        try:
            input_user = await c.get_input_entity(uid)
        except Exception:
            stats["other"] += 1
            continue

        # TRICK: Add them as a contact first using their ID (no phone needed!)
        try:
            await c(AddContactRequest(
                id=input_user,
                first_name=m.get("first_name") or "User",
                last_name=m.get("last_name") or "",
                phone=m.get("phone") or "",
                add_phone_privacy_exception=False
            ))
            imported_ids.append(uid)
        except Exception:
            pass
        
        # Human-like delay after making contact, before adding to group
        await asyncio.sleep(random.uniform(1, 3))

        try:
            if is_super:
                await c(InviteToChannelRequest(channel=target_entity, users=[input_user]))
            else:
                await c(AddChatUserRequest(chat_id=target_entity.id, user_id=input_user, fwd_limit=10))
            stats["added"] += 1

        except errors.FloodWaitError as e:
            # Temporary wait — sleep and retry once
            await asyncio.sleep(e.seconds + 3)
            try:
                if is_super:
                    await c(InviteToChannelRequest(channel=target_entity, users=[input_user]))
                else:
                    await c(AddChatUserRequest(chat_id=target_entity.id, user_id=input_user, fwd_limit=10))
                stats["added"] += 1
            except Exception:
                stats["other"] += 1

        except errors.PeerFloodError:
            # Account is spam-flagged — stop immediately and clean up
            await _cleanup_contacts(c, imported_ids)
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=prog_msg.message_id,
                    parse_mode=ParseMode.HTML,
                    text=(
                        f"⛔ <b>Telegram Group Limit Hit!</b>\n\n"
                        f"✅ Added <b>{stats['added']}</b> members before stopping.\n\n"
                        f"<b>Why this happens:</b> Telegram limits how many people can be\n"
                        f"added to a single group per day (usually ~50 max per group,\n"
                        f"even if you have multiple accounts). Sometimes it silent-drops them.\n\n"
                        f"<b>What to do:</b>\n"
                        f"1. Check the group — some members might have been added\n"
                        f"2. Wait <b>24 hours</b> before adding to THIS group again"
                    ),
                    reply_markup=kb_menu(),
                )
            except Exception:
                pass
            stats["status"] = "peer_flood"
            return stats

        except errors.ChatAdminRequiredError:
            await _cleanup_contacts(c, imported_ids)
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=prog_msg.message_id,
                    parse_mode=ParseMode.HTML,
                    text=(
                        f"❌ <b>Admin rights required!</b>\n\n"
                        f"You need to be an <b>admin</b> in the target group.\n"
                        f"Ask the group owner to promote you first, then try again."
                    ),
                    reply_markup=kb_menu(),
                )
            except Exception:
                pass
            stats["status"] = "no_admin"
            return stats

        except errors.UserPrivacyRestrictedError:  stats["privacy"]      += 1
        except errors.UserNotMutualContactError:   stats["contact_needed"] += 1
        except errors.UserAlreadyParticipantError: stats["already_in"]   += 1
        except errors.InputUserDeactivatedError:   stats["deactivated"]  += 1
        except (errors.UserBannedInChannelError, errors.UserKickedError): stats["other"] += 1
        except Exception:                          stats["other"]        += 1

        # Human-like random delay — same speed a person taps through the UI
        await asyncio.sleep(random.uniform(3, 7))

        # Progress update every ~5 seconds
        now = time.time()
        if now - last_edit >= 5:
            done = i + 1
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=prog_msg.message_id,
                    parse_mode=ParseMode.HTML,
                    text=(
                        f"🚀 <b>Adding members…</b>\n\n"
                        f"<code>{pbar(done, total)}</code>\n"
                        f"✅ Added: <b>{stats['added']}</b>  "
                        f"⏭ Skipped: <b>{done - stats['added']}</b>  "
                        f"📊 Total: <b>{total}</b>\n\n"
                        f"<i>Running contact trick to bypass restrictions…</i>"
                    ),
                )
                last_edit = now
            except Exception:
                pass

    # ── Phase 3: Clean up — delete all imported contacts ─────────────────────
    if imported_ids:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=prog_msg.message_id,
                parse_mode=ParseMode.HTML,
                text="🧹 <b>Cleaning up…</b>\n\n<i>Removing temporarily imported contacts…</i>",
            )
        except Exception:
            pass
        await _cleanup_contacts(c, imported_ids)

    stats["status"] = "done"
    return stats


# ── Account Management Helpers ─────────────────────────────────────────────────

async def show_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sessions = list_sessions()
    current  = state["session_name"]
    me_str   = "Not logged in"
    c = client()
    if c.is_connected() and await c.is_user_authorized():
        me     = await c.get_me()
        me_str = f"{h(me.first_name)} (@{h(me.username or str(me.id))})"

    lines = []
    for s in sessions:
        icon = "✅" if s == current else "○"
        label = " (Legacy/Empty)" if s == "tg_scraper_session" else ""
        lines.append(f"  {icon} <code>{h(s)}</code>{label}")

    text = (
        f"👤 <b>Account Manager</b>\n\n"
        f"✅ Active: <b>{me_str}</b>\n"
        f"💾 Session: <code>{h(current)}</code>\n\n"
        f"<b>Saved sessions:</b>\n" + ("\n".join(lines) if lines else "  <i>None yet</i>")
        + "\n\n<i>Each session = one Telegram account. You can login multiple accounts and switch between them.</i>"
    )
    kb = kb_accounts()
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

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

    status_str = "🔴 <b>KILLED</b> — all users blocked" if state["kill_switch"] else "✅ <b>ACTIVE</b>"

    text = (
        f"🛡️ <b>Admin Control Panel</b>\n\n"
        f"🔌 System: {status_str}\n\n"
        f"👥 <b>Users:</b>\n"
        f"  ⏳ Pending:  <b>{pending}</b>\n"
        f"  ✅ Approved: <b>{approved}</b>\n"
        f"  🚫 Banned:   <b>{banned}</b>\n"
        f"  Total:       <b>{len(users)}</b>\n\n"
        f"🛠️ Scraping as: <code>{h(state['session_name'])}</code>"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_admin())
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_admin())


# ── Start / Main Menu ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    if not await check_access(update, context):
        return ConversationHandler.END

    c = client()
    if not c.is_connected(): await c.connect()

    uid    = update.effective_user.id
    me_str = ""
    if await c.is_user_authorized():
        me     = await c.get_me()
        me_str = f"\n🔗 Scraping as: <b>{h(me.first_name)}</b> (@{h(me.username or str(me.id))})"

    maintenance = "\n\n🔴 <i>System is in maintenance mode (Kill Switch active)</i>" if state["kill_switch"] and is_admin(uid) else ""

    text = f"👋 <b>TG Scraper Bot</b>{me_str}\n\nWhat would you like to do?{maintenance}"

    if not me_str:
        text = (
            "⚠️ <b>No account logged in!</b>\n\n"
            "Tap <b>Accounts</b> below to connect your Telegram account first.\n"
            "<i>(You only need to do this once)</i>"
        )

    kb = kb_main(uid)
    if update.message:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    elif update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception:
            await context.bot.send_message(update.effective_chat.id, text, parse_mode=ParseMode.HTML, reply_markup=kb)
    return ConversationHandler.END

async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(
        f"🆔 <b>Your Telegram User ID:</b>\n\n<code>{uid}</code>\n\n"
        f"Copy this number and paste it into <code>config.ini</code> under:\n"
        f"<code>admin_ids = {uid}</code>\n\nThen restart the bot to become admin.",
        parse_mode=ParseMode.HTML,
    )

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    uid = update.effective_user.id
    await update.message.reply_text("❌ Cancelled.", reply_markup=kb_main(uid))
    return ConversationHandler.END

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Global error handler — logs errors and sends friendly message."""
    logger.error("Exception while handling an update:", exc_info=context.error)
    import traceback
    with open("error.txt", "a") as f:
        f.write(traceback.format_exc())
        if context.error:
            f.write(str(context.error) + "\n")
    if isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(
                update.effective_chat.id,
                f"⚠️ <b>Something went wrong.</b>\n\nError: <code>{html.escape(str(context.error))}</code>\n\nPlease tap /start to try again.",
                parse_mode=ParseMode.HTML,
                reply_markup=kb_menu(),
            )
        except Exception: pass


# ── Callback Router ────────────────────────────────────────────────────────────

async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    uid  = update.effective_user.id
    await q.answer()

    # ── Admin actions (no access check needed) ──────────────────────────────────
    if data == "admin_panel":
        await show_admin_panel(update, context)
        return ConversationHandler.END

    elif data == "adm_users":
        if not is_admin(uid): return ConversationHandler.END
        users = load_users()
        if not users:
            await q.edit_message_text("👥 <b>No users yet.</b>", parse_mode=ParseMode.HTML, reply_markup=kb_admin())
            return ConversationHandler.END
        pending  = sum(1 for u in users.values() if u.get("status")=="pending" and not u.get("banned"))
        approved = sum(1 for u in users.values() if u.get("status")=="approved" and not u.get("banned"))
        banned   = sum(1 for u in users.values() if u.get("banned"))
        await q.edit_message_text(
            f"👥 <b>User Management</b>\n\n"
            f"⏳ Pending: <b>{pending}</b>  ✅ Approved: <b>{approved}</b>  🚫 Banned: <b>{banned}</b>",
            parse_mode=ParseMode.HTML, reply_markup=kb_user_list(),
        )
        return ConversationHandler.END

    elif data == "adm_logs":
        if not is_admin(uid): return ConversationHandler.END
        if not LOG_FILE.exists() or LOG_FILE.stat().st_size == 0:
            await q.edit_message_text("📋 <b>No activity logs yet.</b>", parse_mode=ParseMode.HTML, reply_markup=kb_admin())
            return ConversationHandler.END
        lines   = LOG_FILE.read_text(encoding="utf-8").strip().split("\n")
        recent  = "\n".join(lines[-50:])
        await q.edit_message_text("📋 Sending logs…", parse_mode=ParseMode.HTML)
        await context.bot.send_document(
            update.effective_chat.id,
            InputFile(io.BytesIO(recent.encode()), filename="activity.log"),
            caption=f"📋 <b>Last {min(50,len(lines))} entries</b>",
            parse_mode=ParseMode.HTML,
        )
        await context.bot.send_message(update.effective_chat.id, "⬆️ Done.", reply_markup=kb_admin())
        return ConversationHandler.END

    elif data == "adm_kill":
        if not is_admin(uid): return ConversationHandler.END
        state["kill_switch"] = not state["kill_switch"]
        log_activity(uid, update.effective_user.username or "", f"Kill switch → {state['kill_switch']}")
        status = ("🔴 <b>Kill Switch ACTIVATED.</b>\n\nAll non-admin operations are disabled."
                  if state["kill_switch"] else
                  "🟢 <b>System RESTORED.</b>\n\nAll approved users can operate normally.")
        await q.edit_message_text(status, parse_mode=ParseMode.HTML, reply_markup=kb_admin())
        return ConversationHandler.END

    elif data.startswith("apv_"):
        if not is_admin(uid): return ConversationHandler.END
        target = int(data[4:])
        approve_user(target)
        await notify_user_approved(context, target)
        log_activity(uid, update.effective_user.username or "", f"Approved {target}")
        await q.edit_message_text(f"✅ <b>User {target} approved!</b>", parse_mode=ParseMode.HTML, reply_markup=kb_admin())
        return ConversationHandler.END

    elif data.startswith("ban_"):
        if not is_admin(uid): return ConversationHandler.END
        target = int(data[4:])
        ban_user(target)
        await notify_user_banned(context, target)
        log_activity(uid, update.effective_user.username or "", f"Banned {target}")
        await q.edit_message_text(f"🚫 <b>User {target} banned!</b>", parse_mode=ParseMode.HTML, reply_markup=kb_admin())
        return ConversationHandler.END

    elif data.startswith("unban_"):
        if not is_admin(uid): return ConversationHandler.END
        target = int(data[6:])
        unban_user(target)
        await notify_user_approved(context, target)
        log_activity(uid, update.effective_user.username or "", f"Unbanned {target}")
        await q.edit_message_text(f"✅ <b>User {target} unbanned!</b>", parse_mode=ParseMode.HTML, reply_markup=kb_admin())
        return ConversationHandler.END

    elif data == "noop":
        return ConversationHandler.END

    # ── Access check for regular users ─────────────────────────────────────────
    open_actions = ("main_menu","help","accounts","add_account","cancel")
    if data not in open_actions and not data.startswith(("sw_","del_","flt_","max_","al_")):
        if not await check_access(update, context):
            return ConversationHandler.END

    # ── Navigation ──────────────────────────────────────────────────────────────
    if data == "main_menu":
        return await cmd_start(update, context)

    elif data == "cancel":
        context.user_data.clear()
        await q.edit_message_text("❌ <b>Cancelled.</b>\n\nWhat would you like to do?",
                                   parse_mode=ParseMode.HTML, reply_markup=kb_main(uid))
        return ConversationHandler.END

    elif data == "help":
        await q.edit_message_text(
            "ℹ️ <b>How to Use TG Scraper Bot</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "🔍 <b>Scrape Members</b>\n"
            "Pulls a member list from any group and sends you a <b>CSV</b> + <b>TXT</b> file.\n\n"
            "🚀 <b>Scrape + Add</b>\n"
            "Scrapes members from Group A and adds them to your Group B automatically.\n\n"
            "📁 <b>Last Results</b>\n"
            "Re-downloads your most recent scrape files.\n\n"
            "👤 <b>Accounts</b>\n"
            "Login, switch, or remove Telegram accounts.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "📌 <b>Accepted group formats:</b>\n"
            "• <code>@groupusername</code>\n"
            "• <code>https://t.me/groupname</code>\n"
            "• <code>https://t.me/+invitelink</code>\n\n"
            "⚠️ <b>Note:</b> Telegram limits ~50 adds/day per account.\n"
            "If adds fail → message <code>@SpamBot</code> on Telegram.",
            parse_mode=ParseMode.HTML, reply_markup=kb_menu(),
        )
        return ConversationHandler.END

    elif data == "last_results":
        csv_files = sorted(OUTPUT_DIR.glob("*.csv"))
        if not csv_files:
            await q.edit_message_text("📁 <b>No results yet!</b>\n\nRun a scrape first.",
                                       parse_mode=ParseMode.HTML, reply_markup=kb_menu())
            return ConversationHandler.END
        await q.edit_message_text("📤 Sending your latest files…", parse_mode=ParseMode.HTML)
        latest = csv_files[-1]
        with open(latest, "rb") as f:
            await context.bot.send_document(update.effective_chat.id,
                InputFile(f, filename=latest.name),
                caption=f"📊 <b>{h(latest.stem)}</b>  (Full CSV)", parse_mode=ParseMode.HTML)
        txt = latest.with_suffix(".txt")
        if txt.exists():
            with open(txt, "rb") as f:
                await context.bot.send_document(update.effective_chat.id,
                    InputFile(f, filename=txt.name),
                    caption="📝 <b>Usernames only (TXT)</b>", parse_mode=ParseMode.HTML)
        await context.bot.send_message(update.effective_chat.id, "✅ Done!", reply_markup=kb_main(uid))
        return ConversationHandler.END

    # ── Accounts ────────────────────────────────────────────────────────────────
    elif data == "accounts":
        await show_accounts(update, context)
        return ConversationHandler.END

    elif data == "add_account":
        await q.edit_message_text(
            "➕ <b>Login New Account</b>\n\n"
            "<b>Step 1:</b> Send your phone number with country code:\n\n"
            "Example: <code>+1 234 567 8900</code>",
            parse_mode=ParseMode.HTML, reply_markup=kb_cancel(),
        )
        return WAIT_PHONE

    elif data.startswith("sw_"):
        new_name = data[3:]
        await q.edit_message_text(f"🔄 Switching to <code>{h(new_name)}</code>…", parse_mode=ParseMode.HTML)
        try:
            await switch_session(new_name)
            c = client()
            me_str = "Not authorized"
            if await c.is_user_authorized():
                me     = await c.get_me()
                me_str = f"{h(me.first_name)} (@{h(me.username or str(me.id))})"
            await q.edit_message_text(f"✅ <b>Switched!</b>\n\nNow using: <b>{me_str}</b>",
                                       parse_mode=ParseMode.HTML, reply_markup=kb_menu())
        except Exception as ex:
            await q.edit_message_text(f"❌ Failed: <code>{h(str(ex)[:150])}</code>",
                                       parse_mode=ParseMode.HTML, reply_markup=kb_menu())
        return ConversationHandler.END

    elif data.startswith("del_"):
        name = data[4:]
        for ext in (".session", ".session-journal"):
            p = BASE_DIR / f"{name}{ext}"
            if p.exists(): p.unlink()
        await q.edit_message_text(f"🗑 <b>Deleted:</b> <code>{h(name)}</code>",
                                   parse_mode=ParseMode.HTML, reply_markup=kb_menu())
        return ConversationHandler.END

    # ── Scrape flow start ────────────────────────────────────────────────────────
    elif data in ("scrape_only", "scrape_add"):
        c = client()
        if not c.is_connected() or not await c.is_user_authorized():
            await q.edit_message_text(
                "⚠️ <b>Not logged in!</b>\n\nTap <b>Accounts</b> to connect a Telegram account first.",
                parse_mode=ParseMode.HTML, reply_markup=kb_main(uid))
            return ConversationHandler.END
        context.user_data["mode"] = data
        await q.edit_message_text(
            "🔍 <b>Step 1 of 4 — Source Group</b>\n\n"
            "Send the <b>username or link</b> of the group you want to scrape:\n\n"
            "• <code>@groupname</code>\n"
            "• <code>https://t.me/groupname</code>\n"
            "• <code>https://t.me/+invitelink</code>\n\n"
            "<i>You must be a member of this group.</i>",
            parse_mode=ParseMode.HTML, reply_markup=kb_cancel(),
        )
        return WAIT_SOURCE

    # ── Filter quick buttons ─────────────────────────────────────────────────────
    elif data.startswith("flt_"):
        flt = data[4:]
        return await _run_filter(update, context, flt)

    # ── Max members buttons ──────────────────────────────────────────────────────
    elif data.startswith("max_"):
        return await _run_max(update, context, int(data[4:]))

    # ── Add limit buttons ────────────────────────────────────────────────────────
    elif data.startswith("al_"):
        return await _run_add_limit(update, context, int(data[3:]))

    # ── After-scrape decision ────────────────────────────────────────────────────
    elif data == "yes_add":
        await q.edit_message_text(
            "🚀 <b>Step — Target Group</b>\n\n"
            "Send the <b>username or link</b> of the group to <b>add members into</b>:\n\n"
            "<i>You need to be an admin in this group.</i>",
            parse_mode=ParseMode.HTML, reply_markup=kb_cancel(),
        )
        return WAIT_TARGET

    elif data == "no_add":
        context.user_data.clear()
        await q.edit_message_text("✅ All done!", reply_markup=kb_main(uid))
        return ConversationHandler.END

    return ConversationHandler.END


# ── Login Flow ─────────────────────────────────────────────────────────────────

async def recv_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    msg   = await update.message.reply_text("📲 Sending code to Telegram…", parse_mode=ParseMode.HTML)
    tmp_s = re.sub(r"[^\w]", "_", phone)
    tmp_c = TelegramClient(str(BASE_DIR / f"tmp_{tmp_s}"), API_ID, API_HASH)
    try:
        await tmp_c.connect()
        r = await tmp_c.send_code_request(phone)
        context.user_data.update({"login_phone": phone, "login_hash": r.phone_code_hash,
                                   "login_session": tmp_s, "login_client": tmp_c})
        await msg.edit_text(
            f"✅ <b>Code sent to {h(phone)}!</b>\n\n"
            f"<b>Step 2:</b> Check your Telegram app and type the code here:\n"
            f"<i>(digits only, e.g. 12345)</i>",
            parse_mode=ParseMode.HTML, reply_markup=kb_cancel())
        return WAIT_CODE
    except errors.PhoneNumberInvalidError:
        await tmp_c.disconnect()
        await msg.edit_text(
            "❌ <b>Invalid phone number!</b>\n\nMake sure to include country code.\n"
            "Example: <code>+1 234 567 8900</code>",
            parse_mode=ParseMode.HTML, reply_markup=kb_cancel())
        return WAIT_PHONE
    except Exception as ex:
        await tmp_c.disconnect()
        await msg.edit_text(f"❌ Error: <code>{h(str(ex)[:150])}</code>\n\nTry again:",
                            parse_mode=ParseMode.HTML, reply_markup=kb_cancel())
        return WAIT_PHONE

async def recv_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip().replace(" ", "")
    msg  = await update.message.reply_text("🔐 Verifying code…", parse_mode=ParseMode.HTML)
    try:
        await context.user_data["login_client"].sign_in(
            context.user_data["login_phone"], code,
            phone_code_hash=context.user_data["login_hash"])
        me = await context.user_data["login_client"].get_me()
        context.user_data["login_me"] = me
        await msg.edit_text(
            f"✅ <b>Logged in as {h(me.first_name)}!</b>\n\n"
            f"<b>Step 3:</b> Give this account a name to save it:\n"
            f"<i>(e.g. <code>account1</code>, <code>research</code>) or send <code>skip</code></i>",
            parse_mode=ParseMode.HTML, reply_markup=kb_cancel())
        return WAIT_SESSION_NAME
    except errors.SessionPasswordNeededError:
        await msg.edit_text("🔑 <b>2FA required.</b>\n\nSend your Telegram password:",
                            parse_mode=ParseMode.HTML, reply_markup=kb_cancel())
        return WAIT_2FA
    except errors.PhoneCodeInvalidError:
        await msg.edit_text("❌ <b>Wrong code!</b> Try again:", parse_mode=ParseMode.HTML, reply_markup=kb_cancel())
        return WAIT_CODE
    except errors.PhoneCodeExpiredError:
        await context.user_data["login_client"].disconnect(); context.user_data.clear()
        await msg.edit_text("❌ <b>Code expired.</b> Please start over.",
                            parse_mode=ParseMode.HTML, reply_markup=kb_menu())
        return ConversationHandler.END
    except Exception as ex:
        await msg.edit_text(f"❌ Error: <code>{h(str(ex)[:150])}</code>",
                            parse_mode=ParseMode.HTML, reply_markup=kb_menu())
        return ConversationHandler.END

async def recv_2fa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    try: await update.message.delete()
    except Exception: pass
    msg = await context.bot.send_message(update.effective_chat.id, "🔐 Checking password…")
    try:
        await context.user_data["login_client"].sign_in(password=password)
        me = await context.user_data["login_client"].get_me()
        context.user_data["login_me"] = me
        await msg.edit_text(
            f"✅ <b>Password accepted! Logged in as {h(me.first_name)}.</b>\n\n"
            f"Give this session a name <i>(or <code>skip</code>)</i>:",
            parse_mode=ParseMode.HTML, reply_markup=kb_cancel())
        return WAIT_SESSION_NAME
    except errors.PasswordHashInvalidError:
        await msg.edit_text("❌ <b>Wrong password!</b> Try again:", parse_mode=ParseMode.HTML, reply_markup=kb_cancel())
        return WAIT_2FA
    except Exception as ex:
        await msg.edit_text(f"❌ Error: <code>{h(str(ex)[:150])}</code>", parse_mode=ParseMode.HTML, reply_markup=kb_menu())
        return ConversationHandler.END

async def recv_session_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw   = update.message.text.strip()
    me    = context.user_data["login_me"]
    tmp_c = context.user_data["login_client"]
    tmp_s = context.user_data["login_session"]
    final = f"account_{me.id}" if raw.lower() == "skip" else re.sub(r"[^\w\-]","_",raw)[:30]
    msg   = await update.message.reply_text("💾 Saving…", parse_mode=ParseMode.HTML)
    await tmp_c.disconnect()
    old = BASE_DIR / f"tmp_{tmp_s}.session"
    new = BASE_DIR / f"{final}.session"
    try:
        if old.exists(): old.rename(new)
        await switch_session(final)
        context.user_data.clear()
        uid = update.effective_user.id
        await msg.edit_text(
            f"🎉 <b>Account saved and active!</b>\n\n"
            f"👤 <b>{h(me.first_name)}</b> (@{h(me.username or 'N/A')})\n"
            f"💾 Session: <code>{h(final)}</code>\n\n"
            f"✅ Ready to scrape!",
            parse_mode=ParseMode.HTML, reply_markup=kb_main(uid))
    except Exception as ex:
        await msg.edit_text(f"❌ Error saving: <code>{h(str(ex))}</code>",
                            parse_mode=ParseMode.HTML, reply_markup=kb_menu())
    return ConversationHandler.END


# ── Scrape Flow ────────────────────────────────────────────────────────────────

async def recv_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update, context): return ConversationHandler.END
    inp = update.message.text.strip()
    msg = await update.message.reply_text("🔍 Looking up group…", parse_mode=ParseMode.HTML)
    try:
        entity = await client().get_entity(inp)
        title  = getattr(entity, "title", inp)
        count  = getattr(entity, "participants_count", "?")
        context.user_data.update({"entity": entity, "source_title": title})
    except Exception as ex:
        await msg.edit_text(
            f"❌ <b>Group not found!</b>\n\n<code>{h(str(ex)[:200])}</code>\n\n"
            f"Make sure:\n• The link/username is correct\n• You are a member\n\nTry again 👇",
            parse_mode=ParseMode.HTML, reply_markup=kb_cancel())
        return WAIT_SOURCE

    count_str = f"{count:,}" if isinstance(count, int) else str(count)
    await msg.edit_text(
        f"✅ <b>Found!</b>\n\n"
        f"📌 <b>{h(title)}</b>\n"
        f"👥 Members: <b>{count_str}</b>\n\n"
        f"🔍 <b>Step 2 of 4 — Activity Filter</b>\n\n"
        f"Which members do you want? Active members are more likely to engage!\n"
        f"<i>Bots are always excluded automatically.</i>",
        parse_mode=ParseMode.HTML, reply_markup=kb_filter(),
    )
    return WAIT_FILTER

async def recv_filter_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # If they type instead of tap, default to "recent"
    if not await check_access(update, context): return ConversationHandler.END
    return await _run_filter(update, context, "recent")

async def _run_filter(update: Update, context: ContextTypes.DEFAULT_TYPE, flt: str):
    context.user_data["filter"] = flt
    label = FILTER_LABELS.get(flt, flt)
    title = context.user_data["source_title"]
    text  = (
        f"✅ Filter: <b>{label}</b>\n\n"
        f"📊 <b>Step 3 of 4 — How many members?</b>\n\n"
        f"Tap a number or type your own:\n<i>(group: <b>{h(title)}</b>)</i>"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_max())
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_max())
    return WAIT_MAX

async def recv_max(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update, context): return ConversationHandler.END
    try:
        val = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Please enter a valid number.", reply_markup=kb_max())
        return WAIT_MAX
    return await _run_max(update, context, val)

async def _run_max(update: Update, context: ContextTypes.DEFAULT_TYPE, val: int):
    context.user_data["max_members"] = val
    entity     = context.user_data["entity"]
    title      = context.user_data["source_title"]
    filter_key = context.user_data.get("filter", "all")
    filter_lbl = FILTER_LABELS.get(filter_key, "")
    label      = f"{val:,}" if val > 0 else "all"
    chat_id    = update.effective_chat.id
    uid        = update.effective_user.id

    init = (
        f"⏳ <b>Starting scrape…</b>\n\n"
        f"📌 <b>{h(title)}</b>\n"
        f"🎯 Target: <b>{label}</b>  |  Filter: {filter_lbl}\n\n"
        f"<code>{'░'*20}  0%</code>\n<i>Please wait…</i>"
    )
    prog = (await update.callback_query.edit_message_text(init, parse_mode=ParseMode.HTML)
            if update.callback_query
            else await update.message.reply_text(init, parse_mode=ParseMode.HTML))

    members, err = await do_scrape(entity, val, filter_key, prog, context, chat_id)
    context.user_data["members"] = members
    log_activity(uid, update.effective_user.username or "", f"Scraped {len(members)} from '{title}' [{filter_key}]")

    if err or not members:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=prog.message_id,
            text=err or "⚠️ <b>No members found</b> with this filter. Try a wider filter or a different group.",
            parse_mode=ParseMode.HTML, reply_markup=kb_menu())
        return ConversationHandler.END

    safe     = re.sub(r"[^\w\-]", "_", title)[:30]
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_data = to_csv(members)
    txt_data = to_txt(members)
    (OUTPUT_DIR / f"{safe}_{ts}.csv").write_bytes(csv_data)
    (OUTPUT_DIR / f"{safe}_{ts}.txt").write_bytes(txt_data)
    unames = sum(1 for m in members if m["username"])

    await context.bot.edit_message_text(
        chat_id=chat_id, message_id=prog.message_id,
        text=f"✅ <b>Scrape complete!</b> Sending your files…",
        parse_mode=ParseMode.HTML)

    await context.bot.send_document(chat_id,
        InputFile(io.BytesIO(csv_data), filename=f"{safe}_{ts}.csv"),
        caption=(
            f"📊 <b>Full Data — {len(members):,} members</b>\n"
            f"Filter: {filter_lbl}\n"
            f"<i>Open in Excel or Google Sheets</i>"
        ), parse_mode=ParseMode.HTML)
    await context.bot.send_document(chat_id,
        InputFile(io.BytesIO(txt_data), filename=f"{safe}_{ts}.txt"),
        caption=f"📝 <b>Usernames only — {unames:,} @handles</b>", parse_mode=ParseMode.HTML)
    await context.bot.send_message(chat_id,
        f"✅ <b>Done!</b>  {len(members):,} members scraped.\n\n"
        + (f"Want to add them to a group now?" if context.user_data.get("mode") == "scrape_add" else "What's next?"),
        parse_mode=ParseMode.HTML,
        reply_markup=(kb_after_scrape() if context.user_data.get("mode") == "scrape_add" else kb_main(uid)))

    return ConversationHandler.END


# ── Add Flow ───────────────────────────────────────────────────────────────────

async def recv_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update, context): return ConversationHandler.END
    inp = update.message.text.strip()
    msg = await update.message.reply_text("🔍 Looking up target group…", parse_mode=ParseMode.HTML)
    try:
        entity = await client().get_entity(inp)
        title  = getattr(entity, "title", inp)
        context.user_data.update({"target_entity": entity, "target_title": title})
    except Exception as ex:
        await msg.edit_text(
            f"❌ <b>Group not found!</b>\n\n<code>{h(str(ex)[:200])}</code>\n\nTry again:",
            parse_mode=ParseMode.HTML, reply_markup=kb_cancel())
        return WAIT_TARGET

    members = context.user_data.get("members", [])
    gtype   = "Supergroup/Channel" if isinstance(entity, Channel) else "Basic Group"
    await msg.edit_text(
        f"✅ <b>Target found!</b>\n\n"
        f"📌 <b>{h(title)}</b>  <i>({gtype})</i>\n"
        f"👥 Members ready: <b>{len(members):,}</b>\n\n"
        f"⚠️ <b>Telegram limits ~50 adds per day per account.</b>\n\n"
        f"<b>How many do you want to add right now?</b>",
        parse_mode=ParseMode.HTML, reply_markup=kb_add_limit(min(len(members), 50)))
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
    label   = str(val) if val > 0 else f"all {len(members):,}"

    init = (
        f"🚀 <b>Adding members…</b>\n\n"
        f"📌 <b>{h(tt)}</b>\n"
        f"👥 Adding: <b>{label}</b>\n\n"
        f"<code>{'░'*20}  0%</code>\n<i>This may take a few minutes…</i>"
    )
    prog = (await update.callback_query.edit_message_text(init, parse_mode=ParseMode.HTML)
            if update.callback_query
            else await update.message.reply_text(init, parse_mode=ParseMode.HTML))

    stats = await do_add(te, members, val, prog, context, chat_id)
    log_activity(uid, update.effective_user.username or "",
                 f"Added {stats.get('added',0)} to '{tt}'")

    if stats.get("status") in ("peer_flood", "no_admin"):
        return ConversationHandler.END

    skipped = sum(v for k,v in stats.items() if k not in ("added","status"))
    await context.bot.edit_message_text(
        chat_id=chat_id, message_id=prog.message_id,
        parse_mode=ParseMode.HTML,
        text=(
            f"🎉 <b>Done adding members!</b>\n\n"
            f"📌 <b>{h(tt)}</b>\n\n"
            f"✅ Successfully added: <b>{stats['added']}</b>\n"
            f"⏭ Total skipped: <b>{skipped}</b>\n"
            f"  • Privacy restricted: {stats.get('privacy',0)}\n"
            f"  • Already in group: {stats.get('already_in',0)}\n"
            f"  • Deactivated accounts: {stats.get('deactivated',0)}\n"
            f"  • Need mutual contact: {stats.get('contact_needed',0)}\n"
            f"  • Other: {stats.get('other',0)}\n\n"
            f"<i>Skipped members have privacy settings or are inactive — this is completely normal!</i>"
        ),
        reply_markup=kb_main(uid),
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
        print("⚠️  No session — login via 👤 Accounts in the bot.")
    print(f"🛡️  Admins: {ADMIN_IDS}" if ADMIN_IDS else "⚠️  No admin_ids set! Send /myid to the bot.")

async def post_shutdown(app):
    c = client()
    if c.is_connected(): await c.disconnect()


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
            WAIT_FILTER:       [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_filter_text),
                                 CallbackQueryHandler(cb_handler, pattern="^(flt_|cancel)")],
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
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            CommandHandler("start",  cmd_start),
            CallbackQueryHandler(cb_handler, pattern="^cancel$"),
        ],
        allow_reentry=True,
        per_message=False,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("myid",  cmd_myid))
    app.add_handler(CommandHandler("admin", show_admin_panel))
    app.add_error_handler(error_handler)

    print("\n🤖 TG Scraper Bot is running!")
    print("📱 Telegram → @tg_scrape_tool_bot → /start")
    print("🆔 Run /myid in the bot to get your admin ID")
    print("⌨️  Ctrl+C to stop.\n")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

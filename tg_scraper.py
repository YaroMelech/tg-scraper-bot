#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════╗
║        TG Scraper — All-in-One for Linux         ║
║  Telegram Group Member Scraper + Adder Tool      ║
╚══════════════════════════════════════════════════╝
"""

import asyncio
import configparser
import csv
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.progress import (BarColumn, MofNCompleteColumn, Progress,
                           SpinnerColumn, TaskProgressColumn, TextColumn,
                           TimeRemainingColumn)
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich import box
from rich.text import Text
from rich.align import Align
from rich.rule import Rule
from telethon import TelegramClient, errors, functions, types
from telethon.tl.functions.channels import (GetParticipantsRequest,
                                             InviteToChannelRequest,
                                             CreateChannelRequest)
from telethon.tl.functions.messages import AddChatUserRequest
from telethon.tl.types import (ChannelParticipantsSearch, InputPeerChannel,
                                UserStatusEmpty, UserStatusLastMonth,
                                UserStatusLastWeek, UserStatusOffline,
                                UserStatusOnline, UserStatusRecently)

# ── Constants ─────────────────────────────────────────────────────────────────
console    = Console()
CONFIG_FILE = Path(__file__).parent / "config.ini"
OUTPUT_DIR  = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_config():
    cfg = configparser.ConfigParser()
    if not CONFIG_FILE.exists():
        console.print(f"[red]✗ config.ini not found at {CONFIG_FILE}[/]")
        sys.exit(1)
    cfg.read(CONFIG_FILE)
    return cfg


def banner():
    console.print(Panel.fit(
        Align.center(
            "[bold cyan]🔭  TG Scraper[/bold cyan]  [dim]v2.0[/dim]\n"
            "[dim]All-in-One Telegram Group Member Scraper + Adder\n"
            "For data science research purposes only[/dim]"
        ),
        border_style="cyan",
        padding=(1, 4),
    ))


def status_label(status):
    if isinstance(status, UserStatusOnline):      return "🟢 Online"
    elif isinstance(status, UserStatusRecently):  return "🔵 Recently"
    elif isinstance(status, UserStatusLastWeek):  return "🟡 Last Week"
    elif isinstance(status, UserStatusLastMonth): return "🟠 Last Month"
    elif isinstance(status, UserStatusOffline):   return "⚫ Offline"
    else:                                          return "❓ Unknown"


def build_member_dict(user):
    """Extract all available fields from a Telegram User object."""
    return {
        "user_id":    user.id,
        "username":   user.username or "",
        "first_name": user.first_name or "",
        "last_name":  user.last_name or "",
        "phone":      user.phone or "",
        "status":     status_label(user.status),
        "is_bot":     user.bot,
        "is_premium": getattr(user, "premium", False),
        "is_verified":getattr(user, "verified", False),
        "is_scam":    getattr(user, "scam", False),
        "scraped_at": datetime.now(timezone.utc).isoformat(),  # timezone-aware UTC
    }


# ── Group listing ─────────────────────────────────────────────────────────────

async def list_groups(client):
    """Return a list of (entity, title, member_count) the user can access."""
    dialogs = await client.get_dialogs()
    groups  = []
    for d in dialogs:
        e = d.entity
        if hasattr(e, "megagroup") or hasattr(e, "gigagroup"):
            count = getattr(e, "participants_count", "?")
            groups.append((e, d.title, count))
        elif hasattr(e, "participants_count"):
            groups.append((e, d.title, e.participants_count))
    return groups


async def pick_group(client, prompt_text="Enter group number"):
    """Show an interactive group-picker menu and return (entity, title)."""
    console.print("[bold]Fetching your groups…[/]")
    groups = await list_groups(client)
    if not groups:
        console.print("[red]✗ No groups found.[/]")
        return None, None

    table = Table(title="Your Groups", box=box.ROUNDED, border_style="cyan")
    table.add_column("#",       style="bold yellow", width=4)
    table.add_column("Title",   style="white")
    table.add_column("Members", justify="right", style="green")
    for i, (_, title, count) in enumerate(groups, 1):
        table.add_row(str(i), title, str(count))
    console.print(table)

    choice = Prompt.ask(f"\n[bold cyan]{prompt_text}[/]", default="1")
    idx = int(choice) - 1
    if idx < 0 or idx >= len(groups):
        console.print("[red]✗ Invalid choice.[/]")
        return None, None

    entity, title, _ = groups[idx]
    return entity, title


# ── Scraper ───────────────────────────────────────────────────────────────────

async def scrape_members(client, entity, max_members=0, delay=1, filters=None):
    """Fetch all members from a group with rate-limit handling."""
    members = []
    offset  = 0
    limit   = 200

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("Scraping members...", total=None)

        while True:
            try:
                participants = await client(GetParticipantsRequest(
                    entity,
                    ChannelParticipantsSearch(""),
                    offset=offset,
                    limit=limit,
                    hash=0,
                ))
            except errors.FloodWaitError as e:
                console.print(f"[yellow]⚠ Rate limit — waiting {e.seconds}s…[/]")
                await asyncio.sleep(e.seconds + 2)
                continue
            except errors.ChatAdminRequiredError:
                console.print("[red]✗ Admin rights required to list members.[/]")
                break
            except Exception as ex:
                console.print(f"[red]✗ Error: {ex}[/]")
                break

            if not participants.users:
                break

            total = getattr(participants, "count", None) or len(participants.users)
            if max_members and max_members > 0:
                total = min(total, max_members)
            progress.update(task, total=total)

            for user in participants.users:
                if user.is_self:
                    continue
                member = build_member_dict(user)

                if filters:
                    if filters.get("has_username") and not member["username"]:
                        continue
                    if filters.get("no_bots") and member["is_bot"]:
                        continue
                    if filters.get("premium_only") and not member["is_premium"]:
                        continue
                    if filters.get("regex"):
                        combined = f"{member['username']} {member['first_name']} {member['last_name']}"
                        if not re.search(filters["regex"], combined, re.IGNORECASE):
                            continue

                members.append(member)
                progress.advance(task)

                if max_members and len(members) >= max_members:
                    break

            offset += len(participants.users)
            await asyncio.sleep(delay)

            if max_members and len(members) >= max_members:
                break
            if total > 0 and offset >= total:
                break

    return members


# ── Adder ─────────────────────────────────────────────────────────────────────

# Error reasons mapped to readable messages
ADD_ERRORS = {
    "UserPrivacyRestrictedError":  "Privacy restricted",
    "UserNotMutualContactError":   "Not a mutual contact",
    "UserAlreadyParticipantError": "Already in group",
    "UserBannedInChannelError":    "Banned in channel",
    "InputUserDeactivatedError":   "Account deleted",
    "UserKickedError":             "Was kicked",
    "ChatWriteForbiddenError":     "No write permission",
    "PeerFloodError":              "Peer flood — slow down!",
    "FloodWaitError":              "Flood wait",
}


async def add_members_to_group(
    client,
    target_entity,
    members,
    delay=45,
    batch_size=20,
    batch_pause=180,
):
    """
    Add a list of member dicts (with user_id) to target_entity.
    Automatically detects whether target is a supergroup/channel or a basic group.

    Telegram limits:
      - ~5 adds per minute per account
      - ~50 adds per day before PeerFlood kicks in
      - Recommended: delay >= 45s between adds, pause between batches

    Returns: (added, failed_list)
    """
    added       = 0
    failed      = []   # list of {"user": ..., "reason": ...}
    total       = len(members)
    batch_count = 0

    # Detect group type once up front so we use the correct API
    is_supergroup = hasattr(target_entity, "megagroup") or hasattr(target_entity, "gigagroup") or \
                    target_entity.__class__.__name__ in ("Channel", "ChannelFull")

    group_type = "supergroup/channel" if is_supergroup else "basic group"
    console.print(f"\n[bold cyan]➕ Adding {total} members to target group…[/]")
    console.print(f"[dim]   Group type         : {group_type}[/]")
    console.print(f"[dim]   Delay between adds : {delay}s[/]")
    console.print(f"[dim]   Batch size         : {batch_size} users[/]")
    console.print(f"[dim]   Pause between batches: {batch_pause}s[/]\n")

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold green]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("Adding members…", total=total)

        for i, member in enumerate(members):
            uid      = member.get("user_id")
            username = member.get("username", "")
            label    = f"@{username}" if username else str(uid)

            # Batch pause
            if batch_count > 0 and batch_count % batch_size == 0:
                console.print(
                    f"\n[yellow]⏸ Batch of {batch_size} done — pausing {batch_pause}s "
                    f"to avoid rate limits…[/]"
                )
                await asyncio.sleep(batch_pause)

            try:
                # Resolve the user entity from their numeric ID
                user_entity = await client.get_entity(uid)

                if is_supergroup:
                    # Supergroup / channel — use InviteToChannelRequest
                    await client(InviteToChannelRequest(
                        channel=target_entity,
                        users=[user_entity],
                    ))
                else:
                    # Basic group — use AddChatUserRequest
                    await client(AddChatUserRequest(
                        chat_id=target_entity.id,
                        user_id=user_entity,
                        fwd_limit=10,
                    ))

                added += 1
                batch_count += 1
                progress.advance(task, 1)
                progress.update(task, description=f"Adding… ✓ {label}")

            except errors.FloodWaitError as e:
                wait = e.seconds + 5
                console.print(f"\n[yellow]⚠ FloodWait — sleeping {wait}s…[/]")
                await asyncio.sleep(wait)
                # Retry this member after the flood wait
                try:
                    user_entity = await client.get_entity(uid)
                    if is_supergroup:
                        await client(InviteToChannelRequest(channel=target_entity, users=[user_entity]))
                    else:
                        await client(AddChatUserRequest(chat_id=target_entity.id, user_id=user_entity, fwd_limit=10))
                    added += 1
                    batch_count += 1
                except Exception:
                    failed.append({"user": label, "reason": "Flood wait — retry failed"})
                progress.advance(task, 1)

            except errors.PeerFloodError:
                console.print(
                    "\n[bold red]⛔ PeerFloodError — Telegram has temporarily limited "
                    "your account's ability to add members.\n"
                    "   Stop now and wait 24 hours before trying again.[/]"
                )
                failed.append({"user": label, "reason": "PeerFlood — stopped"})
                break

            except errors.UserPrivacyRestrictedError:
                failed.append({"user": label, "reason": "Privacy restricted"})
                progress.advance(task, 1)

            except errors.UserNotMutualContactError:
                failed.append({"user": label, "reason": "Not a mutual contact"})
                progress.advance(task, 1)

            except errors.UserAlreadyParticipantError:
                failed.append({"user": label, "reason": "Already in group"})
                progress.advance(task, 1)

            except errors.UserBannedInChannelError:
                failed.append({"user": label, "reason": "Banned"})
                progress.advance(task, 1)

            except errors.InputUserDeactivatedError:
                failed.append({"user": label, "reason": "Account deleted"})
                progress.advance(task, 1)

            except errors.ChatAdminRequiredError:
                console.print("\n[red]✗ You need admin rights to add members to this group.[/]")
                failed.append({"user": label, "reason": "Need admin rights"})
                break

            except Exception as ex:
                failed.append({"user": label, "reason": str(ex)[:80]})
                progress.advance(task, 1)

            # Delay between each add
            await asyncio.sleep(delay)

    return added, failed


async def create_new_group(client, title):
    """Create a new supergroup and return its entity."""
    try:
        result = await client(CreateChannelRequest(
            title=title,
            about=f"Created by TG Scraper on {datetime.now().strftime('%Y-%m-%d')}",
            megagroup=True,   # supergroup
        ))
        return result.chats[0]
    except errors.UserRestrictedError:
        console.print(Panel(
            "[bold red]⛔ Your account is spam-restricted by Telegram.[/bold red]\n\n"
            "You cannot create new groups right now. Here's how to fix it:\n\n"
            "  1. Open Telegram on your phone\n"
            "  2. Search for [bold cyan]@SpamBot[/bold cyan] and send it [bold]/start[/bold]\n"
            "  3. Click the [bold green]Appeal[/bold green] button\n"
            "  4. Wait 24–48 hours for Telegram to lift the restriction\n\n"
            "[dim]Meanwhile, pick an EXISTING group from the menu as your target instead.[/dim]",
            border_style="red",
            title="Account Restricted",
            padding=(1, 2),
        ))
        return None


# ── Exporters ─────────────────────────────────────────────────────────────────

FIELDNAMES = [
    "user_id", "username", "first_name", "last_name",
    "phone", "status", "is_bot", "is_premium",
    "is_verified", "is_scam", "scraped_at",
]


def export_csv(members, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(members)
    return path


def export_json(members, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(members, f, indent=2, ensure_ascii=False)
    return path


def export_txt(members, path):
    with open(path, "w", encoding="utf-8") as f:
        for m in members:
            if m["username"]:
                f.write(f"@{m['username']}\n")
    return path


def export_sqlite(members, path):
    con = sqlite3.connect(path)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS members (
            user_id INTEGER, username TEXT, first_name TEXT, last_name TEXT,
            phone TEXT, status TEXT, is_bot INTEGER, is_premium INTEGER,
            is_verified INTEGER, is_scam INTEGER, scraped_at TEXT
        )
    """)
    for m in members:
        cur.execute("INSERT INTO members VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
            m["user_id"], m["username"], m["first_name"], m["last_name"],
            m["phone"], m["status"],
            int(m["is_bot"]), int(m["is_premium"]),
            int(m["is_verified"]), int(m["is_scam"]),
            m["scraped_at"],
        ))
    con.commit()
    con.close()
    return path


def do_export(members, group_name, fmt):
    safe_name = re.sub(r"[^\w\-]", "_", group_name)[:40]
    ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
    base      = OUTPUT_DIR / f"{safe_name}_{ts}"
    paths = []
    if "csv"    in fmt: paths.append(export_csv(members,    str(base) + ".csv"))
    if "json"   in fmt: paths.append(export_json(members,   str(base) + ".json"))
    if "txt"    in fmt: paths.append(export_txt(members,    str(base) + ".txt"))
    if "sqlite" in fmt: paths.append(export_sqlite(members, str(base) + ".db"))
    return paths


def save_add_report(added, failed, group_name):
    """Save a CSV report of which users were added and which failed."""
    safe = re.sub(r"[^\w\-]", "_", group_name)[:40]
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = OUTPUT_DIR / f"add_report_{safe}_{ts}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["user", "reason"])
        writer.writeheader()
        writer.writerows(failed)
    return str(path)


# ── Preview table ─────────────────────────────────────────────────────────────

def preview_table(members, n=10):
    table = Table(
        title=f"Preview — first {min(n, len(members))} of {len(members)} members",
        box=box.ROUNDED, border_style="cyan", show_lines=True,
    )
    for col in ("ID", "Username", "Name", "Status", "Bot", "Premium"):
        table.add_column(col, style="white", no_wrap=True)
    for m in members[:n]:
        name = f"{m['first_name']} {m['last_name']}".strip()
        table.add_row(
            str(m["user_id"]),
            f"@{m['username']}" if m["username"] else "[dim]—[/dim]",
            name or "[dim]—[/dim]",
            m["status"],
            "🤖" if m["is_bot"] else "",
            "⭐" if m["is_premium"] else "",
        )
    console.print(table)


def add_result_table(added, failed):
    """Print a summary table after adding members."""
    total   = added + len(failed)
    success = added
    fail    = len(failed)

    table = Table(title="Add Members — Summary", box=box.ROUNDED, border_style="green")
    table.add_column("Metric",  style="white")
    table.add_column("Count",   style="bold", justify="right")
    table.add_row("Total attempted",   str(total))
    table.add_row("[green]✓ Added successfully[/]",  f"[green]{success}[/]")
    table.add_row("[red]✗ Failed / Skipped[/]",      f"[red]{fail}[/]")
    console.print(table)

    if failed:
        console.print("\n[bold red]Failed users:[/]")
        err_table = Table(box=box.SIMPLE, border_style="dim")
        err_table.add_column("User",   style="cyan")
        err_table.add_column("Reason", style="yellow")
        for f in failed[:30]:  # show max 30 in terminal
            err_table.add_row(f["user"], f["reason"])
        console.print(err_table)
        if len(failed) > 30:
            console.print(f"[dim]... and {len(failed)-30} more (see report file)[/]")


# ── CLI ───────────────────────────────────────────────────────────────────────

@click.command(context_settings=dict(help_option_names=["-h", "--help"]))
@click.option("--group",     "-g", default=None,
              help="Source group username/link (skip menu)")
@click.option("--format",    "-f", "fmt", default="csv,txt", show_default=True,
              help="Export formats: csv, json, txt, sqlite")
@click.option("--max",       "-m", default=0, show_default=True,
              help="Max members to scrape (0 = all)")
@click.option("--delay",     "-d", default=1.0, show_default=True,
              help="Delay (s) between scrape API calls")
@click.option("--usernames-only", is_flag=True, default=False,
              help="Filter: only members WITH a username")
@click.option("--no-bots",        is_flag=True, default=False,
              help="Filter: exclude bots")
@click.option("--premium-only",   is_flag=True, default=False,
              help="Filter: Premium users only")
@click.option("--regex",     "-r", default=None,
              help="Regex filter on name/username")
@click.option("--preview",   "-p", default=10, show_default=True,
              help="Rows to preview (0 = skip)")
# ── Add-to-group options ──────────────────────────────────────────────────────
@click.option("--add",       "-a", "do_add", is_flag=True, default=False,
              help="After scraping, add members to a target group")
@click.option("--target",    "-t", default=None,
              help="Target group username/link to add users to (or 'new' to create one)")
@click.option("--add-delay",       default=2, show_default=True,
              help="Seconds between each add (use 30+ for large batches to avoid limits)")
@click.option("--batch-size",      default=50, show_default=True,
              help="Add N users then pause")
@click.option("--batch-pause",     default=0, show_default=True,
              help="Seconds to pause between batches (0 = no pause)")
@click.option("--add-limit",       default=0, show_default=True,
              help="Max users to add in this run (0 = all scraped)")
def main(group, fmt, max, delay, usernames_only, no_bots, premium_only, regex,
         preview, do_add, target, add_delay, batch_size, batch_pause, add_limit):
    """
    \b
    ╔═══════════════════════════════════════════╗
    ║  TG Scraper v2 — Scraper + Adder          ║
    ╚═══════════════════════════════════════════╝
    Scrape group members → export files → optionally add to another group.
    """
    asyncio.run(_main(
        group, fmt, max, delay, usernames_only, no_bots, premium_only, regex,
        preview, do_add, target, add_delay, batch_size, batch_pause, add_limit,
    ))


async def _main(
    group, fmt, max_m, delay, usernames_only, no_bots, premium_only, regex,
    preview_n, do_add, target, add_delay, batch_size, batch_pause, add_limit,
):
    banner()

    cfg      = load_config()
    api_id   = int(cfg["telegram"]["api_id"])
    api_hash = cfg["telegram"]["api_hash"]
    session  = cfg["telegram"].get("session", "tg_scraper_session")
    formats  = [f.strip().lower() for f in fmt.split(",")]
    filters  = {
        "has_username": usernames_only,
        "no_bots":      no_bots,
        "premium_only": premium_only,
        "regex":        regex,
    }

    console.print(f"\n[dim]Session file:[/] [cyan]{session}.session[/]")
    console.print(f"[dim]Output dir:  [/] [cyan]{OUTPUT_DIR}/[/]\n")

    async with TelegramClient(str(Path(__file__).parent / session), api_id, api_hash) as client:

        # ── 1. Resolve SOURCE group ──────────────────────────────────────────
        console.print(Rule("[bold cyan]STEP 1 — Select Source Group to Scrape[/]"))
        if group:
            try:
                entity      = await client.get_entity(group)
                group_title = getattr(entity, "title", group)
            except Exception as e:
                console.print(f"[red]✗ Could not find '{group}': {e}[/]")
                return
        else:
            entity, group_title = await pick_group(client, "Enter source group number to scrape")
            if entity is None:
                return

        console.print(f"\n[bold green]▶ Scraping:[/] [cyan]{group_title}[/]\n")

        # ── 2. Scrape members ────────────────────────────────────────────────
        members = await scrape_members(
            client, entity,
            max_members=max_m,
            delay=delay,
            filters=filters,
        )

        if not members:
            console.print("[yellow]⚠ No members collected (check filters).[/]")
            return

        console.print(f"\n[bold green]✓ Collected {len(members):,} members[/]\n")

        if preview_n > 0:
            preview_table(members, preview_n)

        # ── 3. Export files ──────────────────────────────────────────────────
        paths = do_export(members, group_title, formats)
        console.print("\n[bold green]📁 Files saved:[/]")
        for p in paths:
            console.print(f"   [cyan]{p}[/]")

        # ── 4. Add to group (optional) ───────────────────────────────────────
        if not do_add:
            # Ask interactively if --add wasn't passed
            do_add = Confirm.ask(
                "\n[bold cyan]➕ Do you want to add these members to a Telegram group?[/]",
                default=False,
            )

        if do_add:
            console.print(Rule("\n[bold cyan]STEP 2 — Select or Create Target Group[/]"))

            # ── Resolve TARGET group ─────────────────────────────────────────
            target_entity = None
            target_title  = ""

            if target and target.lower() == "new":
                target_title = Prompt.ask("[bold cyan]Enter name for the new group[/]")
                console.print(f"[dim]Creating group '{target_title}'…[/]")
                target_entity = await create_new_group(client, target_title)
                if target_entity is None:
                    return   # restriction message already printed inside create_new_group
                console.print(f"[green]✓ Group '{target_title}' created![/]")

            elif target:
                try:
                    target_entity = await client.get_entity(target)
                    target_title  = getattr(target_entity, "title", target)
                except Exception as e:
                    console.print(f"[red]✗ Could not find target '{target}': {e}[/]")
                    return

            else:
                # Interactive: pick existing or create new
                console.print(
                    "\n[bold]Options:[/]\n"
                    "  [yellow]0[/] — Create a [bold]new group[/] automatically\n"
                    "  [yellow]N[/] — Pick an existing group from the list\n"
                )
                groups = await list_groups(client)

                table = Table(title="Your Groups", box=box.ROUNDED, border_style="cyan")
                table.add_column("#",       style="bold yellow", width=4)
                table.add_column("Title",   style="white")
                table.add_column("Members", justify="right", style="green")
                table.add_row("0", "[bold yellow]➕ Create new group[/]", "—")
                for i, (_, t, c) in enumerate(groups, 1):
                    table.add_row(str(i), t, str(c))
                console.print(table)

                choice = Prompt.ask("\n[bold cyan]Enter target group number[/]", default="0")
                idx    = int(choice)

                if idx == 0:
                    target_title  = Prompt.ask("[bold cyan]Enter name for the new group[/]")
                    console.print(f"[dim]Creating group '{target_title}'…[/]")
                    target_entity = await create_new_group(client, target_title)
                    if target_entity is None:
                        return   # restriction message already printed inside create_new_group
                    console.print(f"[green]✓ Group '{target_title}' created![/]")
                else:
                    idx -= 1
                    if idx < 0 or idx >= len(groups):
                        console.print("[red]✗ Invalid choice.[/]")
                        return
                    target_entity, target_title, _ = groups[idx]

            # ── Warn about limits ────────────────────────────────────────────
            console.print(Panel(
                "[yellow]⚠  Telegram Rate-Limit Warning[/]\n\n"
                "Telegram limits how many users you can add per day (~50).\n"
                "Adding too fast may temporarily restrict your account.\n\n"
                f"[dim]Delay between adds : {add_delay}s\n"
                f"Batch size           : {batch_size} users\n"
                f"Pause between batches: {batch_pause}s[/]",
                border_style="yellow",
                padding=(1, 2),
            ))

            to_add = members[:add_limit] if add_limit > 0 else members

            if not Confirm.ask(
                f"[bold]Ready to add [cyan]{len(to_add)}[/cyan] members to "
                f"[cyan]{target_title}[/cyan]?[/]",
                default=True,
            ):
                console.print("[dim]Aborted.[/]")
                return

            # ── Do the adding ────────────────────────────────────────────────
            added, failed = await add_members_to_group(
                client, target_entity, to_add,
                delay=add_delay,
                batch_size=batch_size,
                batch_pause=batch_pause,
            )

            # ── Results ──────────────────────────────────────────────────────
            console.print()
            add_result_table(added, failed)

            if failed:
                report_path = save_add_report(added, failed, target_title)
                console.print(f"\n[dim]Full failure report saved to:[/] [cyan]{report_path}[/]")

            console.print(
                f"\n[bold green]✅ Done![/] Added [bold cyan]{added}[/] members to "
                f"[cyan]{target_title}[/]\n"
            )

        else:
            console.print(
                f"\n[bold]Done![/] Scraped [bold cyan]{len(members):,}[/] members "
                f"from [cyan]{group_title}[/] ✅\n"
            )


if __name__ == "__main__":
    main()

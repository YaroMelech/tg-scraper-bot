# 🔭 TG Scraper — All-in-One Telegram Member Scraper

A powerful, Linux-native Telegram group member scraper built for data science research.

---

## 🚀 Quick Start

```bash
cd ~/Downloads/tg-scraper
chmod +x run.sh
./run.sh
```

On first run, Telegram will ask you to log in with your **phone number** and a **verification code** sent to your Telegram app. This is a one-time setup — after that a session file is saved automatically.

---

## 📦 What Gets Scraped

| Field | Description |
|---|---|
| `user_id` | Unique Telegram user ID |
| `username` | @handle (if set) |
| `first_name` | First name |
| `last_name` | Last name |
| `phone` | Phone (if visible in group) |
| `status` | Online / Recently / Last Week etc. |
| `is_bot` | Whether the account is a bot |
| `is_premium` | Telegram Premium subscriber |
| `is_verified` | Verified account (blue tick) |
| `is_scam` | Telegram-flagged scam account |
| `scraped_at` | Timestamp of scraping |

---

## 📁 Output Files

All files are saved to the `output/` folder automatically, named with the group name + timestamp.

| Format | Use case |
|---|---|
| `.csv` | Excel, pandas, spreadsheets |
| `.json` | Any programming language |
| `.txt` | Plain list of `@usernames` |
| `.db` | SQLite for SQL queries |

---

## 🛠️ Options / Flags

```
./run.sh --help

Options:
  -g, --group TEXT         Group username or invite link (skip menu)
  -f, --format TEXT        Formats: csv,json,txt,sqlite  [default: csv,txt]
  -m, --max INT            Max members to scrape (0 = all)
  -d, --delay FLOAT        Seconds between API calls     [default: 1.0]
  --usernames-only         Only members with a username
  --no-bots                Exclude bots
  --premium-only           Premium users only
  -r, --regex TEXT         Regex filter on name/username
  -p, --preview INT        Rows to preview in terminal   [default: 10]
```

---

## 💡 Usage Examples

```bash
# Interactive menu (just pick a group)
./run.sh

# Scrape specific group, get all formats
./run.sh -g mytargetgroup -f csv,json,txt,sqlite

# Usernames only, no bots, export to csv
./run.sh --usernames-only --no-bots -f csv

# Scrape only 500 members max
./run.sh -m 500

# Load data in pandas (Python)
import pandas as pd
df = pd.read_csv("output/MyGroup_20241009_010000.csv")
print(df.head())
```

---

## ⚠️ Notes

- Use this tool responsibly and only on groups you are a member of.
- Respect Telegram's Terms of Service for data research.
- If you hit rate limits, the tool waits automatically — do not spam requests.

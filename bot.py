#!/usr/bin/env python3
"""
Server Wrench — a read-only Telegram bot that understands your Linux server
===========================================================================

One file. No configuration of projects or services: after you start it, the bot
looks at the machine itself (systemd units, Docker containers, running
processes, web-server sites, project folders) and keeps that picture fresh.

  * Files      browse, preview, download, multi-select "basket", fuzzy search
  * Backups    whole project / essentials / hand-picked, streamed to Telegram
  * Monitoring CPU, RAM, disk, network, every service — charts and reports
  * Alerts     service down / crash loop / disk or RAM full / SSL about to expire
  * Logs       last lines, errors only, log file download

It never writes to, edits or deletes anything outside its own folder.
Only the Telegram accounts listed in ADMIN_IDS can talk to it, in a private chat.

What stays on disk (nothing else is ever created):
  data/monitor.sqlite3   monitoring history, hard-capped (DATA_MAX_MB, default 10)
  data/mtproto.session   only if big-file mode (API_ID / API_HASH) is enabled
  .work/                 temporary parts of a backup; emptied after every job
                         and on every start

Setup: copy .env.example to .env, fill BOT_TOKEN and ADMIN_IDS, run the bot.
See README.md (English) / README.fa.md (فارسی).

Requires Python 3.9+ and:  pip install -r requirements.txt
"""

from __future__ import annotations

import asyncio
import contextvars
import ctypes
import glob
import hashlib
import heapq
import html
import importlib.util
import io
import json
import logging
import math
import os
import re
import shlex
import shutil
import socket
import sqlite3
import ssl
import struct
import sys
import threading
import time
import unicodedata
import uuid
import zipfile
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache, partial
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    InputMediaPhoto,
    LinkPreviewOptions,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import (
    BadRequest,
    Conflict,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    TypeHandler,
    filters,
)

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # without Pillow the reports are text-only
    Image = ImageDraw = ImageFont = None

__version__ = "2.0.0"
APP_NAME = "Server Wrench"
EX_CONFIG = 78   # exit code for a configuration mistake; the service file tells systemd
#                  not to restart on it (a wrong token does not get better by retrying)

# ════════════════════════════════════════════════════════════════
#  Configuration  (.env next to this file, or real environment variables)
# ════════════════════════════════════════════════════════════════

BASE_DIR = Path(__file__).resolve().parent
_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _read_env_file(path: Path) -> dict[str, str]:
    """A small .env reader: KEY=value, optional quotes, `#` comments, `export KEY=…`."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if not sep or not _ENV_KEY_RE.fullmatch(key):
            continue
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        else:
            val = val.split(" #", 1)[0].strip()
        out[key] = val
    return out


# WRENCH_ENV_FILE=/some/other/file picks a different settings file (it is how the test-suite
# makes sure it never reads a real configuration)
_ENV_FILE = _read_env_file(Path(os.environ.get("WRENCH_ENV_FILE") or BASE_DIR / ".env"))


def env(key: str, default: str = "") -> str:
    """Real environment variables win over the .env file."""
    val = os.environ.get(key, "")
    if not val:
        val = _ENV_FILE.get(key, "")
    return val if val else default


def _env_int(key: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(float(env(key, str(default))))))
    except ValueError:
        return default


def _env_on(key: str, default: bool = True) -> bool:
    val = env(key).strip().lower()
    if not val:
        return default
    return val not in ("0", "off", "no", "false", "disable", "disabled")


def _env_list(key: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in re.split(r"[,\n;]+", env(key)) if x.strip())


MIB = 1024 * 1024

# ── identity ──
BOT_TOKEN = env("BOT_TOKEN").strip()
ADMIN_IDS: set[int] = {int(x) for x in re.split(r"[,\s;]+", env("ADMIN_IDS")) if x.isdigit()}
DEFAULT_LANG = env("LANGUAGE", "auto").strip().lower()   # auto | en | fa

# ── optional: big files (one file up to 2 GB instead of 45 MB parts) ──
try:
    API_ID = int(env("API_ID", "0") or 0)
except ValueError:
    API_ID = 0
API_HASH = env("API_HASH").strip()
BIG_PART_MB = _env_int("BIG_PART_MB", 1900, 64, 1990)

# ── optional: network ──
BOT_API_URL = env("BOT_API_URL").strip().rstrip("/")     # your own Bot API server
BOT_API_LIMIT_MB = _env_int("BOT_API_LIMIT_MB", 50, 10, 2000)
PROXY_URL = env("PROXY_URL").strip()                     # http://… or socks5://…
BUTTON_COLORS = _env_on("BUTTON_COLORS")

# ── folders the bot owns ──
DATA_DIR = BASE_DIR / "data"    # monitoring history (hard cap)
WORK_DIR = BASE_DIR / ".work"   # temporary files (always emptied)

# ── sending ──
TG_PART_SIZE = int(BOT_API_LIMIT_MB * 0.9) * MIB        # one Bot API upload
MT_MAX_BYTES = 2000 * MIB                               # Telegram's limit for one file
MT_PART_SIZE = BIG_PART_MB * MIB
MAX_JOB_BYTES = _env_int("MAX_JOB_MB", 4096, 64, 65536) * MIB   # bigger jobs never start
MIN_FREE_DISK = _env_int("MIN_FREE_DISK_MB", 500, 50, 100000) * MIB  # always left untouched
SEND_PAUSE = 0.6
PART_SEND_TIMEOUT = 3600

# ── interface ──
PAGE_SIZE = 8
PROJECTS_PER_PAGE = 12
SERVICES_PER_PAGE = 10
PREVIEW_CHARS = 3000
LOG_LINES = 40
LOG_FILE_LINES = 3000
MAX_SELECT_FILES = 40
MAX_BASKET = 100
FRESH_AGE = 45                   # opening a menu re-scans the server if older than this

# ── monitoring ──
DATA_MAX_BYTES = _env_int("DATA_MAX_MB", 10, 2, 1024) * MIB
SAMPLE_EVERY = 60                # system sample: every minute
SVC_EVERY = 300                  # per-service sample: every 5 minutes
RESCAN_EVERY = 300               # look for new projects / services every 5 minutes
RETENTION_DAYS = 35
EVENT_RETENTION_DAYS = 90
LOG_SCAN_MAX_LINES = 300_000
MEM_ALERT_SAMPLES = 5            # RAM must stay above the limit this many minutes
CRASH_ALERT_GAP = 1800           # at most one crash alert per service per 30 minutes

# ── discovery (everything here is only ever read) ──
AUTO_CONFIGS = _env_on("SYSTEM_CONFIGS")
EXTRA_PATHS = _env_list("EXTRA_PATHS")       # extra folders to show under "Places"
SCAN_PATHS = _env_list("SCAN_PATHS")         # extra parents whose sub-folders are checked
USER_IGNORE_DIRS = frozenset(_env_list("IGNORE_DIRS"))
USER_IGNORE_SERVICES = frozenset(_env_list("IGNORE_SERVICES"))
MAX_PROJECTS = 80
MAX_SERVICES = 120
MAX_CONTAINERS = 60
SCAN_DEPTH = 2                   # how deep under a home folder a project may sit
ROOT_DEPTH = 4                   # same, when a service or process points at it

# home-like parents: never a project themselves; their sub-folders are checked
HOME_BASES = ("/root", "/home/*")
# may be a project themselves, otherwise their sub-folders are checked
APP_BASES = ("/srv", "/app", "/apps", "/data", "/code", "/projects")
# every sub-folder is a website
WEB_BASES = ("/var/www", "/srv/www", "/www/wwwroot")
# never a project root
STOP_DIRS = ("/", "/home", "/opt", "/usr", "/usr/local", "/var", "/mnt", "/media", "/www")
# nothing below these is ever treated as a project
SYSTEM_PREFIXES = (
    "/proc", "/sys", "/dev", "/run", "/boot", "/bin", "/sbin", "/lib", "/lib32", "/lib64",
    "/libx32", "/etc", "/tmp", "/snap", "/lost+found", "/var/tmp", "/var/cache", "/var/log",
    "/var/spool", "/var/run", "/var/lock", "/var/backups", "/var/mail", "/var/snap",
    "/var/lib", "/usr/bin", "/usr/sbin", "/usr/lib", "/usr/lib32", "/usr/lib64",
    "/usr/libexec", "/usr/share", "/usr/include", "/usr/src", "/usr/games",
    "/usr/local/bin", "/usr/local/sbin", "/usr/local/lib", "/usr/local/lib64",
    "/usr/local/share", "/usr/local/include", "/usr/local/etc", "/usr/local/src",
    "/usr/local/games", "/usr/local/man",
)
IGNORE_DIRS = frozenset({"snap", "go", "tmp", "temp", "lost+found", "node_modules",
                         "__pycache__", "venv", "env"}) | USER_IGNORE_DIRS

PROC = "/proc"
SYSTEMD_RUN = "/run/systemd/system"
OS_RELEASE = ("/etc/os-release", "/usr/lib/os-release")
DMI_DIR = "/sys/class/dmi/id"
VIRT_FILES = {"container": "/run/systemd/container", "docker": "/.dockerenv",
              "vz": "/proc/vz", "bc": "/proc/bc", "hypervisor": "/sys/hypervisor/type"}
UNIT_DIR = "/etc/systemd/system"                  # units created on this server
PKG_UNIT_DIRS = ("/usr/lib/systemd/system", "/lib/systemd/system",
                 "/usr/local/lib/systemd/system")
DOCKER_SOCKETS = ("/var/run/docker.sock", "/run/docker.sock", "/run/podman/podman.sock")
CGROUP_ROOT = "/sys/fs/cgroup"
NGINX_GLOBS = ("/etc/nginx/nginx.conf", "/etc/nginx/sites-enabled/*", "/etc/nginx/conf.d/*.conf",
               "/www/server/panel/vhost/nginx/*.conf", "/usr/local/nginx/conf/vhost/*.conf")
APACHE_GLOBS = ("/etc/apache2/sites-enabled/*.conf", "/etc/httpd/conf.d/*.conf",
                "/etc/httpd/conf/httpd.conf", "/www/server/panel/vhost/apache/*.conf")
CADDY_FILES = ("/etc/caddy/Caddyfile",)
LETSENCRYPT_LIVE = "/etc/letsencrypt/live"
WEB_LOG_GLOBS = ("/var/log/nginx/*access*.log", "/var/log/apache2/*access*.log",
                 "/var/log/httpd/*access*log", "/www/wwwlogs/*.log")
# configuration folders offered under "Places" when they exist
CONFIG_PLACES = (
    ("/etc/nginx", "nginx"), ("/etc/apache2", "apache"), ("/etc/httpd", "httpd"),
    ("/etc/caddy", "caddy"), ("/etc/haproxy", "haproxy"), ("/etc/systemd/system", "systemd units"),
    ("/etc/cron.d", "cron.d"), ("/var/spool/cron", "crontabs"), ("/etc/fail2ban", "fail2ban"),
    ("/etc/supervisor", "supervisor"), ("/etc/mysql", "mysql"), ("/etc/postgresql", "postgresql"),
    ("/etc/redis", "redis"), ("/etc/php", "php"), ("/etc/docker", "docker"),
)

# ── what a backup skips / how files are classified ──
SKIP_DIRS = frozenset({"venv", ".venv", "__pycache__", "node_modules", ".git", ".cache",
                       ".npm", ".pytest_cache", ".mypy_cache", ".tox", ".ruff_cache"})
SIDE_SUFFIXES = ("-wal", "-shm", "-journal")     # SQLite's working files next to a database
# names that usually are SQLite databases (used for estimates; the real test is the file's
# own header, so a database called anything at all is still snapshotted)
SQLITE_SUFFIXES = (".sqlite3", ".sqlite", ".db", ".db3", ".session")
SNAP_ONE_GO = 64 * MIB           # larger rollback-journal databases are copied in steps
SNAP_SECONDS = 90.0              # …and given this long before they are copied as found
# already compressed: stored in the zip as-is so no CPU is wasted
STORE_SUFFIXES = (
    ".zip", ".gz", ".xz", ".bz2", ".7z", ".rar", ".zst", ".jpg", ".jpeg", ".png",
    ".gif", ".webp", ".mp3", ".m4a", ".ogg", ".opus", ".flac", ".mp4", ".mkv",
    ".webm", ".mov", ".pdf",
)
# "essentials" of a project: source, configuration and small data files
ESSENTIAL_SUFFIXES = (
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs", ".php", ".rb", ".java",
    ".kt", ".cs", ".sh", ".bash", ".pl", ".lua", ".html", ".htm", ".css", ".scss", ".vue",
    ".svelte", ".json", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".env",
    ".properties", ".xml", ".md", ".txt", ".sql", ".csv", ".session", ".sqlite3", ".sqlite",
    ".db", ".pem", ".crt", ".key", ".service",
)
ESSENTIAL_NAMES = frozenset({"Dockerfile", "Makefile", "Procfile", "Caddyfile", "Pipfile",
                             "Gemfile", ".gitignore", ".dockerignore"})
ESSENTIAL_SKIP_DIRS = SKIP_DIRS | {"logs", "log", "cache", "tmp", "temp", "dist", "build",
                                   ".next", ".nuxt", "target", "vendor", "coverage"}
ESSENTIAL_DEPTH = 3
ESSENTIAL_MAX_FILE = 25 * MIB
ESSENTIAL_MAX_COUNT = 400

# ── search ──
SEARCH_MAX_ENTRIES = 300_000
SEARCH_SECONDS = 8.0
SEARCH_MAX_HITS = 48
GREP_MAX_FILE = 1 * MIB
GREP_MAX_FILES = 30_000
GREP_MAX_BYTES = 300 * MIB
GREP_SECONDS = 12.0

# ── disk usage analysis ──
USAGE_SECONDS = 20.0
USAGE_CACHE = 900

DB_PATH = DATA_DIR / "monitor.sqlite3"
DB_MAX_BYTES = int(DATA_MAX_BYTES * 0.8)    # 20% headroom for SQLite's temporary journal
DB_SOFT_BYTES = int(DB_MAX_BYTES * 0.85)    # oldest rows are dropped from here on
WEB_KEY = "@web"                            # virtual name for website hits in the log table


def _detect_timezone() -> tuple[object, str]:
    """TIMEZONE from .env, else the TZ environment variable, else the server's own zone,
    else UTC."""
    names = [env("TIMEZONE").strip(), os.environ.get("TZ", "").strip().lstrip(":")]
    with suppress(OSError):
        names.append(Path("/etc/timezone").read_text().strip())
    with suppress(OSError):
        link = os.readlink("/etc/localtime")
        if "zoneinfo/" in link:
            names.append(link.split("zoneinfo/", 1)[1])
    for name in names:
        if name:
            with suppress(Exception):
                return ZoneInfo(name), name
    return timezone.utc, "UTC"


TZ, TZ_NAME = _detect_timezone()


# ════════════════════════════════════════════════════════════════
#  Languages: every text the bot shows — "key": (English, فارسی)
#  To add a language: add a third item to each pair and its code to LANGS.
# ════════════════════════════════════════════════════════════════

LANGS = ("en", "fa")
LANG: contextvars.ContextVar = contextvars.ContextVar("lang", default="en")
DAYS = {
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
    "fa": ("دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"),
}

TXT: dict[str, tuple[str, str]] = {
    # ── small words ──
    "u.d": ("{n}d", "{n} روز"),
    "u.h": ("{n}h", "{n} ساعت"),
    "u.m": ("{n}m", "{n} دقیقه"),
    "u.s": ("{n}s", "{n} ثانیه"),
    "on": ("on", "روشن"),
    "off": ("off", "خاموش"),
    "saved": ("Saved", "ذخیره شد"),
    "more": ("… and {n} more", "… و {n} مورد دیگر"),
    "sending": ("⏳ Sending…", "⏳ در حال ارسال…"),
    "preparing": ("⏳ Preparing…", "⏳ در حال آماده‌سازی…"),
    "expired": ("This button has expired — open the menu again.",
                "این دکمه منقضی شده؛ از منو دوباره وارد شو."),

    # ── buttons ──
    "b.home": ("🏠 Menu", "🏠 منو"),
    "b.back": ("⬅️ Back", "⬅️ برگشت"),
    "b.cancel": ("⛔️ Cancel", "⛔️ لغو"),
    "b.refresh": ("🔄 Refresh", "🔄 بروزرسانی"),
    "b.files": ("📁 Files", "📁 فایل‌ها"),
    "b.search": ("🔎 Search", "🔎 جستجو"),
    "b.backup": ("📦 Backup", "📦 بک‌آپ"),
    "b.basket": ("🧺 Basket ({n})", "🧺 سبد ({n})"),
    "b.status": ("📊 Status", "📊 وضعیت"),
    "b.services": ("⚙️ Services", "⚙️ سرویس‌ها"),
    "b.disk": ("💽 Disk", "💽 دیسک"),
    "b.procs": ("🧮 Processes", "🧮 پردازه‌ها"),
    "b.reports": ("📈 Reports", "📈 گزارش‌ها"),
    "b.events": ("🧾 Events", "🧾 رخدادها"),
    "b.settings": ("🛠 Settings", "🛠 تنظیمات"),
    "b.help": ("❔ Help", "❔ راهنما"),
    "b.configs": ("⚙️ Config folders ({n})", "⚙️ پوشه‌های تنظیمات ({n})"),
    "b.places": ("📌 Extra paths ({n})", "📌 مسیرهای اضافه ({n})"),
    "b.rescan": ("🔄 Scan again", "🔄 بررسی دوباره"),
    "b.open": ("📂 Open", "📂 باز کن"),
    "b.sel_page": ("✅ This page", "✅ همین صفحه"),
    "b.sel_none": ("⬜ None", "⬜ هیچ‌کدام"),
    "b.send_basket": ("📤 Send basket ({n})", "📤 ارسال سبد ({n})"),
    "b.up": ("⬆️ Up", "⬆️ بالا"),
    "b.sel_done": ("✔️ Done selecting", "✔️ پایان انتخاب"),
    "b.zip_dir": ("📦 Zip this folder", "📦 زیپ این پوشه"),
    "b.multi": ("☑️ Select several", "☑️ انتخاب چندتایی"),
    "b.project": ("ℹ️ About this project", "ℹ️ درباره‌ی این پروژه"),
    "b.projects": ("📁 Projects", "📁 پروژه‌ها"),
    "b.download": ("⬇️ Download", "⬇️ دانلود"),
    "b.preview": ("👁 Preview", "👁 پیش‌نمایش"),
    "b.basket_add": ("➕ To basket", "➕ به سبد"),
    "b.basket_del": ("➖ From basket", "➖ از سبد"),
    "b.pin": ("⭐ Mark essential", "⭐ علامت مهم"),
    "b.unpin": ("☆ Unmark", "☆ برداشتن علامت"),
    "b.rename": ("✏️ Rename", "✏️ تغییر نام"),
    "b.hide": ("🙈 Hide", "🙈 پنهان کن"),
    "b.autoname": ("↩️ Automatic name", "↩️ نام خودکار"),
    "b.one_zip": ("📦 All in one zip", "📦 همه در یک زیپ"),
    "b.one_by_one": ("📄 One by one", "📄 جداجدا"),
    "b.clear": ("🗑 Empty basket", "🗑 خالی‌کردن سبد"),
    "b.add_more": ("📁 Add more", "📁 افزودن بیشتر"),
    "b.several": ("☑️ Several projects at once", "☑️ چند پروژه با هم"),
    "b.all_ess": ("⭐ Essentials of everything (one zip)", "⭐ ضروری‌های همه (یک زیپ)"),
    "b.full": ("📦 Whole project (zip)", "📦 کل پروژه (زیپ)"),
    "b.ess": ("⭐ Essentials only ({n})", "⭐ فقط ضروری‌ها ({n})"),
    "b.pick": ("☑️ Pick files", "☑️ انتخاب دستی"),
    "b.all": ("✅ All", "✅ همه"),
    "b.none": ("⬜ None", "⬜ هیچ"),
    "b.zip_n": ("📦 One zip ({n})", "📦 یک زیپ ({n})"),
    "b.each_n": ("📄 One by one ({n})", "📄 جداجدا ({n})"),
    "b.m_full_zip": ("📦 Whole — one zip", "📦 کامل — یک زیپ"),
    "b.m_full_each": ("📦 Whole — separate", "📦 کامل — جداجدا"),
    "b.m_ess_zip": ("⭐ Essentials — one zip", "⭐ ضروری‌ها — یک زیپ"),
    "b.m_ess_each": ("⭐ Essentials — separate", "⭐ ضروری‌ها — جداجدا"),
    "b.new_search": ("🔎 New search", "🔎 جستجوی جدید"),
    "b.grep": ("🔍 Search inside files", "🔍 جستجو داخل فایل‌ها"),
    "b.by_name": ("🔤 Search names", "🔤 جستجوی نام"),
    "b.logs": ("📜 Logs", "📜 لاگ"),
    "b.errors": ("⚠️ Errors only", "⚠️ فقط خطاها"),
    "b.all_lines": ("📜 All lines", "📜 همه‌ی خط‌ها"),
    "b.logfile": ("⬇️ Log file ({n} lines)", "⬇️ فایل لاگ ({n} خط)"),
    "b.unitfile": ("📄 Unit file", "📄 فایل سرویس"),
    "b.mute": ("🔕 Mute alerts", "🔕 بی‌صدا کردن هشدار"),
    "b.unmute": ("🔔 Unmute alerts", "🔔 روشن کردن هشدار"),
    "b.usage": ("🔍 What takes the space?", "🔍 چه چیزی جا گرفته؟"),
    "b.chart": ("🖼 Chart", "🖼 تصویر"),
    "b.remeasure": ("🔄 Measure again", "🔄 اندازه‌گیری دوباره"),
    "b.r1": ("24 hours", "24 ساعت"),
    "b.r7": ("7 days", "7 روز"),
    "b.r30": ("30 days", "30 روز"),
    "b.schedule": ("🗓 Schedule", "🗓 زمان‌بندی"),
    "b.alerts_on": ("🔔 Turn alerts on", "🔔 روشن کردن هشدارها"),
    "b.alerts_off": ("🔕 Turn alerts off", "🔕 خاموش کردن هشدارها"),
    "b.disk_at": ("💽 Disk alert: {v}%", "💽 هشدار دیسک: {v}٪"),
    "b.mem_at": ("🧠 RAM alert: {v}%", "🧠 هشدار رم: {v}٪"),
    "b.report_mode": ("🗓 Scheduled report: {v}", "🗓 گزارش خودکار: {v}"),
    "b.week_start": ("📅 Week starts: {v}", "📅 شروع هفته: {v}"),
    "b.hidden": ("🙈 Hidden projects ({n})", "🙈 پروژه‌های پنهان ({n})"),

    # ── home ──
    "home.sub": ("Read-only: browse, back up and watch — nothing on the server is changed.",
                 "فقط‌خواندنی: مشاهده، بک‌آپ و مانیتورینگ — هیچ چیزی روی سرور تغییر نمی‌کند."),
    "home.svc": ("⚙️ Services: {up} of {n} running", "⚙️ سرویس‌ها: {up} از {n} در حال اجرا"),
    "home.bad": ("🔴 {n} in trouble", "🔴 {n} مورد مشکل دارد"),
    "home.res": ("📊 CPU {cpu} · RAM {mem} · Disk {disk}", "📊 CPU {cpu} · رم {mem} · دیسک {disk}"),
    "home.pick": ("Choose 👇", "یکی را انتخاب کن 👇"),

    # ── files ──
    "fl.title": ("Projects on this server", "پروژه‌های این سرور"),
    "fl.cfg_title": ("⚙️ Configuration folders", "⚙️ پوشه‌های تنظیمات"),
    "fl.extra_title": ("📌 Extra paths", "📌 مسیرهای اضافه"),
    "fl.count": ("{n} items", "{n} مورد"),
    "fl.count_auto": ("{n} found automatically", "{n} مورد — خودکار شناسایی شده"),
    "fl.missing": ("⚠️ folder not found", "⚠️ پوشه پیدا نشد"),
    "fl.none": ("Nothing was found yet. Tap «Scan again» after you add a project.",
                "هنوز چیزی پیدا نشده. بعد از اضافه کردن پروژه «بررسی دوباره» را بزن."),
    "fl.scanned": ("🕒 Last scan: {when}", "🕒 آخرین بررسی سرور: {when}"),
    "dir.error": ("❌ This folder cannot be read:\n<code>{err}</code>",
                  "❌ خواندن پوشه ممکن نیست:\n<code>{err}</code>"),
    "dir.count": ("📁 {dirs} folders · 📄 {files} files", "📁 {dirs} پوشه · 📄 {files} فایل"),
    "dir.empty": ("<i>This folder is empty.</i>", "<i>پوشه خالی است.</i>"),
    "dir.selmode": (
        "☑️ <b>Selecting</b> — tap an item to put it in the basket or take it out.\n"
        "In the basket: <b>{n}</b>",
        "☑️ <b>حالت انتخاب</b> — روی هر مورد بزن تا به سبد اضافه یا از آن حذف شود.\n"
        "در سبد: <b>{n}</b> مورد"),
    "file.pinned": ("⭐ Marked essential — included in «Essentials» backups",
                    "⭐ علامت «مهم» دارد — در بک‌آپ «ضروری‌ها» می‌آید"),
    "file.split": ("✂️ Too large for one message: it will arrive in {n} parts of up to {size}",
                   "✂️ برای یک پیام بزرگ است؛ در {n} تکه‌ی حداکثر {size} می‌آید"),
    "file.pin_on": ("Marked essential", "علامت مهم خورد"),
    "file.pin_off": ("Unmarked", "علامت برداشته شد"),
    "file.binary": ("🚫 This is a binary file; it can only be downloaded.",
                    "🚫 این فایل باینری است؛ فقط دانلود می‌شود."),

    # ── one project ──
    "pj.k_app": ("Application (something runs from it)", "برنامه (چیزی از آن اجرا می‌شود)"),
    "pj.k_site": ("Website", "وب‌سایت"),
    "pj.k_dir": ("Folder (nothing runs from it)", "پوشه (چیزی از آن اجرا نمی‌شود)"),
    "pj.k_flat": ("Loose files of this folder", "فایل‌های تکیِ این پوشه"),
    "pj.no_svc": ("<i>No service, container or running program was found for this folder.</i>",
                  "<i>برای این پوشه سرویس، کانتینر یا برنامه‌ی در حال اجرایی پیدا نشد.</i>"),
    "pj.ess": ("⭐ Essential files: <b>{n}</b>", "⭐ فایل‌های ضروری: <b>{n}</b>"),
    "pj.rename_how": (
        "✏️ Send the new name for <b>{title}</b> as a message (an emoji at the start is fine).",
        "✏️ نام جدید <b>{title}</b> را در یک پیام بفرست (می‌توانی اولش ایموجی بگذاری)."),
    "pj.renamed": ("✅ Renamed to <b>{title}</b>.", "✅ نام به <b>{title}</b> تغییر کرد."),
    "pj.hidden": ("Hidden. Bring it back from Settings → Hidden projects.",
                  "پنهان شد. از تنظیمات ← پروژه‌های پنهان برمی‌گردد."),
    "pj.auto": ("Automatic name restored", "نام خودکار برگشت"),

    # ── basket ──
    "bq.empty": ("The basket is empty", "سبد خالی است"),
    "bq.how": (
        "Open a folder in «Files», tap «Select several» and tick files and folders — from as "
        "many projects as you like. Then send them all from here.",
        "از «فایل‌ها» وارد یک پوشه شو، «انتخاب چندتایی» را بزن و فایل‌ها و پوشه‌ها را (از هر "
        "چند پروژه) علامت بزن؛ بعد همه را از همین‌جا بفرست."),
    "bq.title": ("Basket", "سبد انتخاب"),
    "bq.count": ("{n} items", "{n} مورد"),
    "bq.size": ("📄 Files: {size}", "📄 حجم فایل‌ها: {size}"),
    "bq.dirs": ("📁 {n} folders", "📁 {n} پوشه"),
    "bq.hint": (
        "<b>One zip:</b> everything in a single file · <b>One by one:</b> each item on its own",
        "<b>یک زیپ:</b> همه در یک فایل · <b>جداجدا:</b> هر مورد جدا، پشت‌سرهم"),
    "bq.removed": ("Removed from the basket", "از سبد حذف شد"),
    "bq.cannot": ("This item cannot be added (the basket is full, or it is not a regular file).",
                  "افزودن ممکن نیست (سبد پر است یا این مورد فایل معمولی نیست)."),
    "bq.full": ("The basket is full (at most {n} items).", "سبد پر است (حداکثر {n} مورد)."),
    "bq.added": ("Added ({n} in the basket)", "به سبد اضافه شد ({n})"),
    "bq.cleared": ("Basket emptied", "سبد خالی شد"),

    # ── backup ──
    "bk.title": ("Backup", "بک‌آپ"),
    "bk.sub": ("Pick a project, or several at once:", "یک پروژه را انتخاب کن، یا چند پروژه را با هم:"),
    "bk.note": ("Backups go to this chat only. Nothing is stored on the server.",
                "بک‌آپ فقط به همین چت فرستاده می‌شود؛ چیزی روی سرور ذخیره نمی‌شود."),
    "bp.ess": ("⭐ Essential files found: <b>{n}</b>", "⭐ فایل ضروری پیدا شد: <b>{n}</b>"),
    "bp.no_ess": ("<i>No essential files were recognised in this folder.</i>",
                  "<i>در این پوشه فایل ضروری‌ای شناسایی نشد.</i>"),
    "bp.legend": (
        "<b>Whole project:</b> everything except virtualenvs, node_modules, caches and .git\n"
        "<b>Essentials:</b> source code, configuration and small data files, plus anything "
        "you marked ⭐",
        "<b>کل پروژه:</b> همه‌چیز به‌جز venv، node_modules، کش‌ها و ‎.git\n"
        "<b>ضروری‌ها:</b> کد، تنظیمات و فایل‌های داده‌ی کوچک، به‌علاوه‌ی هرچه با ⭐ علامت زده‌ای"),
    "sel.how": ("Tick the files you want, then choose how to send them.",
                "فایل‌های مورد نظر را علامت بزن، بعد نوع ارسال را انتخاب کن."),
    "sel.count": ("Selected: <b>{n}</b> of {total}", "انتخاب‌شده: <b>{n}</b> از {total}"),
    "sel.capped": ("Showing the first {n} of {total}; use «Files» for the rest.",
                   "{n} مورد اول از {total} نشان داده شده؛ بقیه را از «فایل‌ها» بردار."),
    "sel.on": ("Selecting is on", "حالت انتخاب روشن شد"),
    "sel.off": ("Selecting is off", "حالت انتخاب خاموش شد"),
    "sel.nothing": ("Nothing is selected.", "چیزی انتخاب نشده."),
    "mp.title": ("Back up several projects", "بک‌آپ چند پروژه با هم"),
    "mp.how": ("Tick the projects, then choose the kind of backup.",
               "پروژه‌ها را علامت بزن، بعد نوع بک‌آپ را انتخاب کن."),
    "mp.legend": ("<b>One zip:</b> all in a single file · <b>Separate:</b> one zip per project",
                  "<b>یک زیپ:</b> همه در یک فایل · <b>جداجدا:</b> هر پروژه یک زیپ"),
    "mp.nothing": ("No project is selected.", "هیچ پروژه‌ای انتخاب نشده."),

    # ── jobs and sending ──
    "job.files": ("🗂 {done}/{total} files", "🗂 {done}/{total} فایل"),
    "job.sent": ("📤 {size} sent", "📤 {size} ارسال شد"),
    "job.busy": ("⏳ Another job is running. Wait for it, or tap «Cancel».",
                 "⏳ یک عملیات دیگر در حال اجراست؛ صبر کن تمام شود یا «لغو» را بزن."),
    "job.started": ("⏳ Started…", "⏳ شروع شد…"),
    "job.cancelled": ("⛔️ Cancelled.", "⛔️ عملیات لغو شد."),
    "job.failed": ("❌ The job failed (network, disk or permissions). See the bot's log.",
                   "❌ عملیات با خطا روبه‌رو شد (شبکه، دیسک یا دسترسی). لاگ ربات را ببین."),
    "job.none": ("Nothing is running.", "عملیاتی در جریان نیست."),
    "job.cancelling": ("⛔️ Cancelling… (after the current part)",
                       "⛔️ در حال لغو… (بعد از تکه‌ی فعلی)"),
    "job.basket": ("Sending the basket", "ارسال سبد انتخاب"),
    "job.download": ("Download {name}", "دانلود {name}"),
    "job.zip_dir": ("Zip folder {name}", "زیپ پوشه‌ی {name}"),
    "job.backup": ("Backup {title}", "بک‌آپ {title}"),
    "job.backup_all": ("Essentials of every project", "بک‌آپ ضروری‌های همه‌ی پروژه‌ها"),
    "job.backup_n": ("Backup of {n} projects", "بک‌آپ {n} پروژه"),
    "job.pick": ("Selected files of {title}", "ارسال انتخابی {title}"),
    "send.part": ("📎 {name} — part {i}/{n} · total {size}", "📎 {name} — تکه {i}/{n} · کل {size}"),
    "send.join": (
        "\n\nTo join the parts: Windows → 7-Zip on the <code>.001</code> file · "
        "Linux/macOS → <code>cat name.* &gt; name</code>",
        "\n\nبرای چسباندن تکه‌ها: ویندوز ← 7-Zip روی فایل <code>.001</code> · "
        "لینوکس/مک ← <code>cat name.* &gt; name</code>"),
    "send.empty": ("⚠️ <code>{name}</code> is empty.", "⚠️ فایل <code>{name}</code> خالی است."),
    "send.too_big": ("🚫 <code>{name}</code> is {size}, above the {cap} limit (MAX_JOB_MB).",
                     "🚫 <code>{name}</code> با حجم {size} از سقف {cap} بزرگ‌تر است (MAX_JOB_MB)."),
    "send.big_fallback": (
        "ℹ️ Big-file mode is not available right now (<code>{why}</code>); sending in parts.",
        "ℹ️ حالت فایل حجیم الان در دسترس نیست (<code>{why}</code>)؛ تکه‌تکه می‌فرستم."),
    "send.big_failed": (
        "❌ The big upload stopped midway (<code>{why}</code>). Try again: it will go out in "
        "small parts.",
        "❌ آپلود حجیم وسط کار قطع شد (<code>{why}</code>). دوباره امتحان کن؛ این بار تکه‌تکه "
        "فرستاده می‌شود."),
    "send.failed": ("❌ The file could not be sent.", "❌ ارسال فایل ناموفق بود."),
    "send.many_done": ("✅ {ok} of {n} sent.", "✅ {ok} از {n} مورد ارسال شد."),
    "zip.nothing": ("⚠️ <b>{title}</b>: there is nothing to send.",
                    "⚠️ <b>{title}</b>: فایلی برای ارسال پیدا نشد."),
    "zip.too_big": (
        "🚫 <b>{title}</b> is about {size}, above the {cap} limit. Pick smaller folders or "
        "raise MAX_JOB_MB.",
        "🚫 <b>{title}</b> حدود {size} است و از سقف {cap} بیشتر؛ پوشه‌های کوچک‌تر را انتخاب کن "
        "یا MAX_JOB_MB را بالا ببر."),
    "zip.no_workdir": (
        "❌ The bot cannot write to its own work folder (<code>{path}</code>), so it cannot "
        "build a zip. Single files can still be downloaded. Running <code>install.sh</code> "
        "again repairs the folder.",
        "❌ بات نمی‌تواند در پوشه‌ی کاری خودش (<code>{path}</code>) بنویسد، پس زیپ ساخته نمی‌شود. "
        "دانلود تک‌فایل همچنان کار می‌کند. اجرای دوباره‌ی <code>install.sh</code> پوشه را درست می‌کند."),
    "zip.no_space": (
        "💽 Free disk space ({free}) is too low for this. Nothing was done, to keep your "
        "services safe.",
        "💽 فضای خالی دیسک ({free}) برای این کار کافی نیست؛ برای آسیب نرسیدن به سرویس‌ها "
        "انجام نشد."),
    "zip.summary": ("📦 <b>{title}</b> · {label}\n🗂 {n} files · 💾 {size} · ⏱ {secs}s\n🕒 {when}",
                    "📦 <b>{title}</b> · {label}\n🗂 {n} فایل · 💾 {size} · ⏱ {secs}s\n🕒 {when}"),
    "zip.as_found": (
        "⚠️ {n} database(s) were busy or left unfinished by a crash: copied as found, with "
        "the -wal / -journal file beside them",
        "⚠️ {n} دیتابیس در حال نوشتن بود یا بعد از کرش نیمه‌کاره مانده: همان‌طور که بود کپی شد، "
        "همراه فایل ‎-wal / -journal کنارش"),
    "zip.skipped": ("⚠️ {n} files could not be read", "⚠️ {n} فایل خوانده نشد"),
    "zip.part": ("📎 {name} — part {i}", "📎 {name} — تکه {i}"),
    "zip.last": (" (last)", " (آخر)"),
    "zip.parts": ("✂️ {n} parts", "✂️ {n} تکه"),
    "zip.l_dir": ("folder zip", "زیپ پوشه"),
    "zip.l_full": ("whole project", "کل پروژه"),
    "zip.l_ess": ("essentials", "فایل‌های ضروری"),
    "zip.l_pick": ("selected ({n} files)", "انتخابی ({n} فایل)"),
    "zip.t_all": ("All projects", "همه‌ی پروژه‌ها"),
    "zip.t_n": ("{n} projects", "{n} پروژه"),

    # ── search ──
    "sq.title": ("Search", "جستجو"),
    "sq.how": (
        "Send a word — or part of one — from a file or folder name.\n\n"
        "• Typos are fine: <code>confg</code> finds <code>config.json</code>\n"
        "• Several words narrow it down: <code>bot env</code>\n"
        "• You never need this button: just type, at any time\n"
        "• After the results you can also search <b>inside</b> files",
        "یک کلمه — یا بخشی از آن — از نام فایل یا پوشه بفرست.\n\n"
        "• غلط تایپی مشکلی نیست: <code>confg</code> فایل <code>config.json</code> را پیدا می‌کند\n"
        "• چند کلمه نتیجه را دقیق‌تر می‌کند: <code>bot env</code>\n"
        "• لازم نیست این دکمه را بزنی؛ هر وقت خواستی فقط تایپ کن\n"
        "• بعد از نتیجه‌ها می‌توانی <b>داخل</b> فایل‌ها را هم بگردی"),
    "sq.short": ("Send at least 2 characters.", "حداقل 2 حرف بفرست."),
    "sq.busy": ("⏳ Another search is running; try again in a moment.",
                "⏳ یک جستجوی دیگر در جریان است؛ چند لحظه بعد دوباره بفرست."),
    "sq.wait": ("🔎 Searching for <code>{q}</code>…", "🔎 در حال جستجوی <code>{q}</code>…"),
    "sq.wait_c": ("🔍 Searching inside files for <code>{q}</code>…",
                  "🔍 در حال جستجوی <code>{q}</code> داخل فایل‌ها…"),
    "sq.wait_short": ("⏳ Searching…", "⏳ در حال جستجو…"),
    "sr.title": ("Search", "جستجو"),
    "sr.title_c": ("Inside files", "داخل فایل‌ها"),
    "sr.found": ("{n} results", "{n} نتیجه"),
    "sr.scanned": ("{n} checked", "{n} مورد بررسی شد"),
    "sr.fuzzy": ("≈ No exact match — these are the closest names.",
                 "≈ مطابقت دقیق نبود؛ این‌ها نزدیک‌ترین نام‌ها هستند."),
    "sr.cut": ("⏱ Stopped at the time or size limit; a more specific word gives full results.",
               "⏱ به سقف زمان یا حجم رسید؛ با کلمه‌ی دقیق‌تر نتیجه کامل می‌شود."),
    "sr.none": ("Nothing matches. Try a shorter or a different word.",
                "چیزی پیدا نشد. کلمه‌ی کوتاه‌تر یا دیگری را امتحان کن."),
    "sr.none_c": ("No text file contains this.", "هیچ فایل متنی این را ندارد."),

    # ── services and logs ──
    "sv.unknown": ("unknown", "نامشخص"),
    "sv.running": ("running", "در حال اجرا"),
    "sv.unhealthy": ("unhealthy", "ناسالم"),
    "sv.busy": ("changing: {state}", "در حال تغییر: {state}"),
    "sv.failed": ("failed", "از کار افتاده"),
    "sv.stopped": ("stopped", "متوقف"),
    "sv.off": ("off", "خاموش"),
    "sv.k_unit": ("systemd service", "سرویس systemd"),
    "sv.k_docker": ("Docker container", "کانتینر Docker"),
    "sv.k_proc": ("Program running without a service manager", "برنامه‌ی در حال اجرا بدون سرویس"),
    "sv.since": ("⏱ Running since {when} ({ago})", "⏱ از {when} در حال اجراست ({ago})"),
    "sv.mem": ("🧠 Memory: {mem}", "🧠 رم: {mem}"),
    "sv.nrest": ("♻️ Automatic restarts: {n}", "♻️ ری‌استارت خودکار: {n} بار"),
    "sv.result": ("❗️ Last result: <code>{why}</code>", "❗️ نتیجه‌ی آخر: <code>{why}</code>"),
    "sv.sub": ("State: <code>{sub}</code>", "حالت: <code>{sub}</code>"),
    "sv.muted": ("🔕 Alerts for this one are muted.", "🔕 هشدارهای این مورد بی‌صداست."),
    "sv.mute_on": ("Alerts muted", "هشدارها بی‌صدا شد"),
    "sv.mute_off": ("Alerts back on", "هشدارها روشن شد"),
    "sv.proc_note": (
        "<i>This program was started by hand (nohup, screen, tmux, pm2, cron…), so it has no "
        "journal. Its own log files are in the project folder.</i>",
        "<i>این برنامه دستی اجرا شده (nohup، screen، tmux، pm2، cron و …) و لاگ systemd ندارد. "
        "فایل‌های لاگ خودش در پوشه‌ی پروژه است.</i>"),
    "sv.no_unitfile": ("⚠️ The unit file could not be found.", "⚠️ فایل سرویس پیدا نشد."),
    "sl.title": ("Services", "سرویس‌ها"),
    "sl.g_app": ("Your apps", "برنامه‌های تو"),
    "sl.g_sys": ("System services", "سرویس‌های سیستم"),
    "sl.g_docker": ("Containers", "کانتینرها"),
    "sl.g_proc": ("Running without a service", "در حال اجرا بدون سرویس"),
    "sl.none": ("No service was found yet.", "هنوز سرویسی پیدا نشده."),
    "sl.no_systemd": (
        "systemd and Docker were not found on this machine, so there is no service list. "
        "Files, backups, search and system monitoring still work.",
        "روی این ماشین systemd و Docker پیدا نشد، پس فهرست سرویس وجود ندارد. فایل‌ها، بک‌آپ، "
        "جستجو و مانیتورینگ سیستم کار می‌کنند."),
    "sl.hint": ("Tap a service for details and logs.", "برای جزئیات و لاگ روی هر سرویس بزن."),
    "lg.errors": ("{n} errors/warnings in the last {lines} lines",
                  "{n} خطا/هشدار در {lines} خط آخر"),
    "lg.last": ("last {n} lines", "آخرین {n} خط"),
    "lg.empty": ("(empty)", "(خالی)"),
    "lg.none": ("⚠️ No log was found for this one.", "⚠️ لاگی برای این مورد پیدا نشد."),
    "lg.file": ("last {n} lines", "{n} خط آخر"),
    "lg.unknown": ("No service is called <code>{name}</code>. Pick one:",
                   "سرویسی به نام <code>{name}</code> نیست. یکی را انتخاب کن:"),

    # ── status ──
    "st.title": ("Server status", "وضعیت سرور"),
    "st.up": ("⏱ Up {up} · 🕒 {now} {tz}", "⏱ آپتایم {up} · 🕒 {now} {tz}"),
    "st.load": ("⚙️ Load: {a} · {b} · {c} ({cores} cores)", "⚙️ لود: {a} · {b} · {c} ({cores} هسته)"),
    "st.mem": ("🧠 RAM: {used} of {total}", "🧠 رم: {used} از {total}"),
    "st.swap": ("swap {used} of {total}", "سواپ {used} از {total}"),
    "st.disk": ("💽 <code>{path}</code>: {used} of {total} · {free} free",
                "💽 <code>{path}</code>: {used} از {total} · {free} خالی"),
    "st.net": ("🌐 Network: ⬇️ {rx}/s · ⬆️ {tx}/s", "🌐 شبکه: ⬇️ {rx}/s · ⬆️ {tx}/s"),
    "st.svc": ("⚙️ <b>Services:</b> 🟢 {up} of {n} running",
               "⚙️ <b>سرویس‌ها:</b> 🟢 {up} از {n} در حال اجرا"),
    "st.svc_bad": ("🔴 {n} down", "🔴 {n} مشکل‌دار"),
    "st.svc_off": ("⚪️ {n} off", "⚪️ {n} خاموش"),
    "st.no_svc": ("<i>No services were found on this machine yet.</i>",
                  "<i>هنوز سرویسی روی این ماشین پیدا نشده.</i>"),
    "st.failed": ("⚠️ Other failed units: {names}", "⚠️ یونیت‌های failed دیگر: {names}"),
    "st.sites": ("🌐 Sites: {n} ({d} domains)", "🌐 سایت‌ها: {n} ({d} دامنه)"),
    "st.ssl": (
        "{mark} SSL: <code>{name}</code> expires in <b>{days}</b> days ({n} certificates watched)",
        "{mark} SSL: <code>{name}</code> تا <b>{days}</b> روز دیگر اعتبار دارد ({n} گواهی زیر نظر)"),
    "st.self": ("🧰 This bot: {mem} RAM · {cpu}% CPU · history {data} of {cap}",
                "🧰 خود ربات: {mem} رم · {cpu}٪ CPU · داده {data} از {cap}"),

    # ── disk and processes ──
    "dk.title": ("Disk", "دیسک"),
    "dk.line": ("{used} of {total} used · <b>{free}</b> free",
                "{used} از {total} پر · <b>{free}</b> خالی"),
    "dk.silent": (
        "⚠️ <code>{path}</code> <i>({fs})</i> does not answer — a network drive whose server "
        "cannot be reached? It is left out until it answers again.",
        "⚠️ <code>{path}</code> <i>({fs})</i> جواب نمی‌دهد — احتمالاً سرورِ این درایو شبکه‌ای "
        "در دسترس نیست. تا وقتی جواب ندهد کنار گذاشته می‌شود."),
    "dk.inodes": ("⚠️ inodes: {pct}% used (very many small files)",
                  "⚠️ inode: {pct}٪ پر (تعداد فایل خیلی زیاد)"),
    "dk.hint": (
        "«What takes the space?» measures your projects and the usual suspects (logs, Docker, "
        "caches).",
        "«چه چیزی جا گرفته؟» حجم پروژه‌ها و جاهای معمول (لاگ‌ها، Docker، کش‌ها) را اندازه می‌گیرد."),
    "dk.caption": ("💽 <code>{path}</code> is {pct} full · {free} free",
                   "💽 <code>{path}</code> {pct} پر است · {free} خالی"),
    "du.title": ("What takes the space", "چه چیزی جا گرفته"),
    "du.when": ("measured at {when}", "اندازه‌گیری‌شده در {when}"),
    "du.none": ("Nothing measurable was found.", "چیزی برای اندازه‌گیری پیدا نشد."),
    "du.files": ("Largest files in your projects", "بزرگ‌ترین فایل‌های پروژه‌ها"),
    "du.partial": (
        "≥ means the {secs}-second limit was reached there: the real size is at least that.",
        "≥ یعنی آن‌جا به سقف {secs} ثانیه رسید؛ حجم واقعی دست‌کم همین است."),
    "du.busy": ("⏳ A measurement is already running.", "⏳ یک اندازه‌گیری در حال انجام است."),
    "du.wait": ("⏳ Measuring… up to {secs} seconds", "⏳ در حال اندازه‌گیری… تا {secs} ثانیه"),
    "du.working": ("🔍 Measuring folder sizes at low priority… (up to {secs} seconds)",
                   "🔍 در حال اندازه‌گیری حجم پوشه‌ها با اولویت پایین… (تا {secs} ثانیه)"),
    "tp.title": ("Processes", "پردازه‌ها"),
    "tp.count": ("{n} running", "{n} در حال اجرا"),
    "tp.cpu": ("CPU right now", "CPU همین الان"),
    "tp.idle": ("<i>Nothing is using noticeable CPU.</i>",
                "<i>چیزی CPU قابل‌توجهی مصرف نمی‌کند.</i>"),
    "tp.mem": ("Memory", "رم"),
    "tp.note": ("CPU is measured over one second; 100% = one full core ({cores} available).",
                "CPU در یک ثانیه اندازه‌گیری شده؛ 100٪ یعنی یک هسته‌ی کامل ({cores} هسته موجود)."),
    "tp.wait": ("⏳ Measuring for one second…", "⏳ یک ثانیه اندازه‌گیری…"),

    # ── reports ──
    "rp.title": ("Reports and charts", "گزارش و نمودار"),
    "rp.since": ("🗃 Data since <code>{when}</code> ({n} one-minute samples)",
                 "🗃 داده از <code>{when}</code> ({n} نمونه‌ی یک‌دقیقه‌ای)"),
    "rp.nothing": ("🗃 No samples yet (the first one arrives within a minute).",
                   "🗃 هنوز نمونه‌ای ثبت نشده (اولین نمونه تا یک دقیقه‌ی دیگر)."),
    "rp.size": ("💾 History: {size} of {cap} · kept for {days} days",
                "💾 حجم داده: {size} از سقف {cap} · نگهداری {days} روز"),
    "rp.pick": ("Pick a period:", "بازه را انتخاب کن:"),
    "rp.s_off": ("Scheduled report: off", "گزارش خودکار: خاموش"),
    "rp.s_daily": ("Scheduled report: every day at {hour}", "گزارش خودکار: هر روز ساعت {hour}"),
    "rp.s_weekly": ("Scheduled report: every {day} at {hour}",
                    "گزارش خودکار: هر {day} ساعت {hour}"),
    "rp.m_off": ("off", "خاموش"),
    "rp.m_daily": ("daily", "روزانه"),
    "rp.m_weekly": ("weekly", "هفتگی"),
    "rp.t_1": ("Last 24 hours", "گزارش 24 ساعت اخیر"),
    "rp.t_7": ("Last 7 days", "گزارش 7 روز اخیر"),
    "rp.t_30": ("Last 30 days", "گزارش 30 روز اخیر"),
    "rp.t_n": ("Last {n} days", "گزارش {n} روز اخیر"),
    "rp.t_week": ("Weekly server report", "گزارش هفتگی سرور"),
    "rp.t_day": ("Daily server report", "گزارش روزانه‌ی سرور"),
    "rp.h_res": ("Resources", "منابع"),
    "rp.h_rhythm": ("Rhythm", "الگوی مصرف"),
    "rp.h_svc": ("Services", "سرویس‌ها"),
    "rp.cpu": ("⚙️ CPU: avg <b>{avg}</b> · peak <b>{peak}</b> ({when})",
               "⚙️ CPU: میانگین <b>{avg}</b> · اوج <b>{peak}</b> ({when})"),
    "rp.ram": ("🧠 RAM: avg <b>{avg}</b> · peak <b>{peak}</b> ({when})",
               "🧠 رم: میانگین <b>{avg}</b> · اوج <b>{peak}</b> ({when})"),
    "rp.disk": ("💽 Disk: <b>{now}</b> ({delta} pt)", "💽 دیسک: <b>{now}</b> (تغییر {delta})"),
    "rp.net": ("🌐 Traffic: ⬇️ <b>{rx}</b> · ⬆️ <b>{tx}</b>",
               "🌐 ترافیک: ⬇️ <b>{rx}</b> · ⬆️ <b>{tx}</b>"),
    "rp.peak": ("🔥 Peak hour: <b>{hours}</b> (CPU {cpu})", "🔥 ساعت پیک: <b>{hours}</b> (CPU {cpu})"),
    "rp.quiet": ("🌙 Quietest hour: {hours}", "🌙 خلوت‌ترین ساعت: {hours}"),
    "rp.busiest": ("📍 Busiest slot: <b>{day} {hour}</b>", "📍 شلوغ‌ترین بازه: <b>{day} {hour}</b>"),
    "rp.netpeak": ("📡 Traffic peak: {hours}", "📡 پیک ترافیک: {hours}"),
    "rp.web": ("🌍 Site requests: <b>{n}</b>", "🌍 درخواست‌های سایت: <b>{n}</b>"),
    "rp.web_peak": ("peak {hours}", "پیک {hours}"),
    "rp.web_5xx": ("5xx errors: {n}", "خطای 5xx: {n}"),
    "rp.up": ("🟢 Availability: <b>{v}</b>", "🟢 دسترس‌پذیری: <b>{v}</b>"),
    "rp.crash": ("♻️ Crashes: <b>{n}</b>", "♻️ کرش: <b>{n}</b>"),
    "rp.restart": ("🔁 manual restarts: <b>{n}</b>", "🔁 ری‌استارت دستی: <b>{n}</b>"),
    "rp.errs": ("❗️ Errors in logs: <b>{n}</b>", "❗️ خطا در لاگ‌ها: <b>{n}</b>"),
    "rp.cover": ("📊 Data coverage: {v} ({n} samples)", "📊 پوشش داده: {v} ({n} نمونه)"),
    "rp.no_monitor": ("⚠️ Monitoring is not running.", "⚠️ مانیتورینگ فعال نیست."),
    "rp.no_data": (
        "⏳ Not enough data for this period yet.\n"
        "The bot takes one sample a minute; try again in a few minutes.",
        "⏳ هنوز داده‌ی کافی برای این بازه جمع نشده.\n"
        "ربات هر دقیقه یک نمونه می‌گیرد؛ چند دقیقه‌ی دیگر دوباره امتحان کن."),
    "rp.no_pillow": ("<i>Charts need Pillow: <code>venv/bin/pip install pillow</code></i>",
                     "<i>برای نمودار Pillow لازم است: <code>venv/bin/pip install pillow</code></i>"),
    "rp.no_pillow_short": ("Charts need the Pillow package.", "برای تصویر، پکیج Pillow لازم است."),
    "rp.chart_failed": ("<i>⚠️ The chart could not be drawn; see the bot's log.</i>",
                        "<i>⚠️ رسم نمودار ناموفق بود؛ لاگ ربات را ببین.</i>"),
    "rp.failed": ("❌ The report could not be built. See the bot's log.",
                  "❌ ساخت گزارش ناموفق بود. لاگ ربات را ببین."),
    "rp.building": ("⏳ Building the report…", "⏳ در حال ساخت گزارش…"),

    # ── events ──
    "ev.title": ("Latest events", "آخرین رخدادها"),
    "ev.sub": ("services, and what admins fetched", "سرویس‌ها و کارهای ادمین‌ها"),
    "ev.none": ("<i>Nothing has happened yet.</i>", "<i>هنوز رخدادی ثبت نشده.</i>"),
    "ev.crash": ("crashed and was restarted", "کرش و ری‌استارت خودکار"),
    "ev.restart": ("was restarted by hand", "ری‌استارت دستی"),
    "ev.down": ("went down", "از کار افتاد"),
    "ev.up": ("came up", "بالا آمد"),
    "ev.alert": ("alert", "هشدار"),
    "ev.new": ("found on the server", "روی سرور شناسایی شد"),
    "ev.gone": ("is no longer on the server", "دیگر روی سرور نیست"),

    # ── settings ──
    "se.title": ("Settings", "تنظیمات"),
    "se.lang": ("🌐 Language: <b>{v}</b>", "🌐 زبان: <b>{v}</b>"),
    "se.alerts": ("🔔 Alerts: <b>{v}</b>", "🔔 هشدارها: <b>{v}</b>"),
    "se.disk": ("💽 Disk alert at <b>{v}%</b>", "💽 هشدار دیسک از <b>{v}٪</b>"),
    "se.mem": ("🧠 RAM alert at <b>{v}%</b> (held for {n} minutes)",
               "🧠 هشدار رم از <b>{v}٪</b> ({n} دقیقه پشت‌سرهم)"),
    "se.week": ("📅 Week starts on <b>{v}</b>", "📅 شروع هفته: <b>{v}</b>"),
    "se.hidden": ("🙈 Hidden projects: <b>{n}</b>", "🙈 پروژه‌های پنهان: <b>{n}</b>"),
    "se.big_on": ("📦 Big files: <b>on</b> — one file up to 2 GB, larger ones in {size} parts",
                  "📦 فایل حجیم: <b>روشن</b> — هر فایل تا 2 گیگ، بزرگ‌تر در تکه‌های {size}"),
    "se.big_off": (
        "📦 Big files: <b>off</b> — anything above the Bot API limit is sent in parts. To send "
        "one file of up to 2 GB, put <code>API_ID</code> and <code>API_HASH</code> in "
        "<code>.env</code>.",
        "📦 فایل حجیم: <b>خاموش</b> — هرچه از سقف Bot API بزرگ‌تر باشد تکه‌تکه فرستاده می‌شود. "
        "برای ارسال یکجا تا 2 گیگ، <code>API_ID</code> و <code>API_HASH</code> را در "
        "<code>.env</code> بگذار."),
    "se.big_missing": (
        "📦 Big files: <b>API_ID is set, but Telethon is not installed</b> — run "
        "<code>venv/bin/pip install -r requirements-bigfiles.txt</code> and restart.",
        "📦 فایل حجیم: <b>API_ID تنظیم شده ولی Telethon نصب نیست</b> — دستور "
        "<code>venv/bin/pip install -r requirements-bigfiles.txt</code> را بزن و ری‌استارت کن."),
    "se.big_paused": (
        "📦 Big files: <b>paused after an error</b> — it retries by itself in a few minutes; "
        "until then files go out in parts.",
        "📦 فایل حجیم: <b>بعد از یک خطا موقتاً متوقف است</b> — چند دقیقه‌ی دیگر خودش دوباره "
        "امتحان می‌کند؛ تا آن موقع تکه‌تکه می‌فرستد."),
    "se.limits": ("✉️ One message carries up to {one} · one job up to {job}",
                  "✉️ سقف هر پیام {one} · سقف هر عملیات {job}"),
    "hd.title": ("Hidden projects", "پروژه‌های پنهان"),
    "hd.sub": ("Tap one to show it again.", "برای برگرداندن روی هر مورد بزن."),
    "hd.none": ("<i>Nothing is hidden.</i>", "<i>چیزی پنهان نیست.</i>"),
    "hd.shown": ("Visible again", "دوباره نمایش داده می‌شود"),

    # ── help, access ──
    "hp.tag": ("the pocket wrench for your server", "آچار جیبی سرور"),
    "hp.body": (
        "• <b>Files</b> — browse every project, preview, download, collect a basket\n"
        "• <b>Search</b> — just type part of a name; typos are forgiven\n"
        "• <b>Backup</b> — whole project, essentials or hand-picked, straight to this chat\n"
        "• <b>Status · Services · Disk · Processes</b> — what the server is doing right now\n"
        "• <b>Reports</b> — charts for 24 hours, 7 or 30 days, and a scheduled report\n"
        "• <b>Alerts</b> — a service goes down or crashes, disk or RAM fills up, an SSL "
        "certificate is about to expire, a new project appears",
        "• <b>فایل‌ها</b> — مرور همه‌ی پروژه‌ها، پیش‌نمایش، دانلود، جمع کردن در سبد\n"
        "• <b>جستجو</b> — فقط بخشی از نام را تایپ کن؛ غلط تایپی هم قبول است\n"
        "• <b>بک‌آپ</b> — کل پروژه، ضروری‌ها یا انتخابی، مستقیم به همین چت\n"
        "• <b>وضعیت · سرویس‌ها · دیسک · پردازه‌ها</b> — سرور همین الان چه می‌کند\n"
        "• <b>گزارش‌ها</b> — نمودار 24 ساعت، 7 و 30 روز، و گزارش خودکار\n"
        "• <b>هشدارها</b> — افتادن یا کرش سرویس، پر شدن دیسک یا رم، نزدیک شدن انقضای SSL، "
        "پیدا شدن پروژه‌ی جدید"),
    "hp.cmds": ("Commands", "دستورها"),
    "hp.cmdlist": (
        "/status · /services · /disk · /top\n"
        "/report <code>7</code> · /logs <code>name</code> · /find <code>word</code>\n"
        "/backup · /events · /settings · /id",
        "/status · /services · /disk · /top\n"
        "/report <code>7</code> · /logs <code>نام</code> · /find <code>کلمه</code>\n"
        "/backup · /events · /settings · /id"),
    "hp.safe": (
        "The bot never writes, edits or deletes anything on the server. Only the admins "
        "listed in ADMIN_IDS can use it.",
        "ربات هیچ‌وقت چیزی را روی سرور نمی‌نویسد، ویرایش یا حذف نمی‌کند. فقط ادمین‌های "
        "ADMIN_IDS به آن دسترسی دارند."),
    "setup.id": (
        "👋 This bot has no admin yet.\n\nYour Telegram ID is <code>{uid}</code>.\n"
        "Put it in the <code>.env</code> file as <code>ADMIN_IDS={uid}</code> and restart the bot.",
        "👋 این ربات هنوز ادمین ندارد.\n\nشناسه‌ی تلگرام تو <code>{uid}</code> است.\n"
        "آن را در فایل <code>.env</code> به شکل <code>ADMIN_IDS={uid}</code> بگذار و ربات را "
        "ری‌استارت کن."),
    "gate.denied": ("⛔️ You do not have access to this bot.\nYour Telegram ID: <code>{uid}</code>",
                    "⛔️ به این ربات دسترسی نداری.\nشناسه‌ی تلگرام تو: <code>{uid}</code>"),
    "gate.short": ("⛔️ No access", "⛔️ دسترسی نداری"),
    "id.yours": ("Your Telegram ID: <code>{uid}</code>", "شناسه‌ی تلگرام تو: <code>{uid}</code>"),
    "rs.failed": ("⚠️ The scan failed; the previous list is still in place. See the bot's log.",
                  "⚠️ بررسی سرور ناموفق بود؛ فهرست قبلی سر جایش است. لاگ ربات را ببین."),
    "rs.done": ("✅ {p} projects · {s} services", "✅ {p} پروژه · {s} سرویس"),
    "rs.new": ("{n} new", "{n} مورد تازه"),
    "rs.same": ("nothing new", "چیز تازه‌ای نبود"),

    # ── alerts ──
    "al.conflict": (
        "⚠️ <b>Another program is using this bot's token</b> — a second copy of this bot, or a "
        "bot on another server.\nUntil only one is left, buttons answer at random. Every "
        "server needs a bot (token) of its own.",
        "⚠️ <b>برنامه‌ی دیگری هم با توکن همین بات کار می‌کند</b> — نسخه‌ی دوم همین بات، یا باتی روی "
        "یک سرور دیگر.\nتا وقتی فقط یکی بماند، دکمه‌ها گاهی جواب نمی‌دهند. هر سرور باید بات (توکن) "
        "جدای خودش را داشته باشد."),
    "err.generic": ("⚠️ That did not work (an internal error). The details are in the bot's log.",
                    "⚠️ انجام نشد (خطای داخلی). جزئیات در لاگ بات هست."),
    "al.many": ("🆕 Several new projects or services were found on the server; see «Files».",
                "🆕 چند پروژه یا سرویس جدید روی سرور شناسایی شد؛ فهرست کامل در «فایل‌ها»."),
    "al.first": (
        "🧰 <b>Server Wrench is now watching this server</b>\n<code>{host}</code> · {os}\n\n"
        "Found by itself: <b>{n}</b> projects · <b>{s}</b> services ({d} containers) · "
        "<b>{w}</b> sites\n\n"
        "Send /start for the menu. From now on you will hear about new projects, services "
        "going down, a filling disk and expiring certificates.",
        "🧰 <b>Server Wrench این سرور را زیر نظر گرفت</b>\n<code>{host}</code> · {os}\n\n"
        "خودکار شناسایی شد: <b>{n}</b> پروژه · <b>{s}</b> سرویس ({d} کانتینر) · "
        "<b>{w}</b> سایت\n\n"
        "برای منو /start را بفرست. از این به بعد پروژه‌ی جدید، افتادن سرویس، پر شدن دیسک و "
        "انقضای گواهی را خبر می‌دهم."),
    "al.new_head": ("🆕 <b>New on the server</b>", "🆕 <b>پروژه‌ی جدید روی سرور</b>"),
    "al.new_item": ("\n{title}\n{info}", "\n{title}\n{info}"),
    "al.more": ("\n… and {n} more", "\n… و {n} مورد دیگر"),
    "al.new_tail": ("\nIt is already in «Files» and «Backup»; what runs from it is in «Services».",
                    "\nاز همین حالا در «فایل‌ها» و «بک‌آپ» هست و هرچه از آن اجرا می‌شود در «سرویس‌ها»."),
    "al.new_svc": ("⚙️ <b>New service</b> — watched from now on: {names}",
                   "⚙️ <b>سرویس جدید</b> — از این به بعد مانیتور می‌شود: {names}"),
    "al.crash": ("♻️ {name} crashed and was restarted automatically (restart #{n}).",
                 "♻️ {name} کرش کرد و خودکار ری‌استارت شد (بار {n})."),
    "al.up": ("🟢 {name} is running again.", "🟢 {name} دوباره بالا آمد."),
    "al.unhealthy": ("🟠 {name} is running but reports <b>unhealthy</b>.",
                     "🟠 {name} در حال اجراست ولی وضعیتش <b>ناسالم</b> است."),
    "al.down": ("🔴 {name} is down (<code>{why}</code>).", "🔴 {name} از کار افتاده (<code>{why}</code>)."),
    "al.disk": ("💽 Disk <code>{path}</code> is <b>{pct}%</b> full ({free} free).",
                "💽 دیسک <code>{path}</code> <b>{pct}٪</b> پر شده ({free} خالی)."),
    "al.mem": ("🧠 RAM has been above <b>{pct}%</b> for {n} minutes.",
               "🧠 رم {n} دقیقه است که بالای <b>{pct}٪</b> مانده."),
    "al.ssl": ("🔐 The SSL certificate of <code>{name}</code> expires in <b>{days}</b> days ({date}).",
               "🔐 گواهی SSL <code>{name}</code> تا <b>{days}</b> روز دیگر منقضی می‌شود ({date})."),
    "al.ssl_expired": ("🔐 The SSL certificate of <code>{name}</code> <b>has expired</b> ({date}).",
                       "🔐 گواهی SSL <code>{name}</code> <b>منقضی شده</b> ({date})."),

    # ── command descriptions (the "/" menu) ──
    "cmd.start": ("Main menu", "منوی اصلی"),
    "cmd.status": ("Server status", "وضعیت سرور"),
    "cmd.services": ("Services and containers", "سرویس‌ها و کانتینرها"),
    "cmd.disk": ("Disk space", "فضای دیسک"),
    "cmd.top": ("Busiest processes", "پرمصرف‌ترین پردازه‌ها"),
    "cmd.report": ("Report with charts", "گزارش و نمودار"),
    "cmd.logs": ("Service logs", "لاگ سرویس‌ها"),
    "cmd.find": ("Find a file", "جستجوی فایل"),
    "cmd.backup": ("Back up a project", "بک‌آپ پروژه"),
    "cmd.events": ("Recent events", "رخدادهای اخیر"),
    "cmd.settings": ("Settings", "تنظیمات"),
    "cmd.help": ("Help", "راهنما"),
}


RLM = "\u200f"                  # right-to-left mark: invisible, sets a line's direction
_MAYBE_RTL = re.compile("[\u0590-\u08ff\ufb1d-\ufefc]")
_ANY_TAG = re.compile(r"<[^>]+>")


def _direction(plain: str) -> tuple[str, bool]:
    """(direction of the first letter — "L", "R" or "" —, has right-to-left letters).
    Letters only: a Persian comma or percent sign in a line of Latin names does not count."""
    first = ""
    for ch in plain:
        cls = unicodedata.bidirectional(ch)
        if cls in ("R", "AL"):
            return first or "R", True
        if cls == "L" and not first:
            first = "L"
    return first, False


def rtl_lines(text: str) -> str:
    """Telegram lays out every line by its first letter. A Persian line that happens to
    begin with a Latin word (a service name, "CPU", a file name) would turn left-to-right
    on its own and break the alignment of the message; a right-to-left mark in front of it
    keeps the whole message one way. Lines without Persian letters, and everything inside
    <pre>, are left exactly as they are."""
    if not _MAYBE_RTL.search(text):
        return text
    out, in_pre = [], False
    for line in text.split("\n"):
        if not in_pre and "<pre" not in line and _MAYBE_RTL.search(line):
            if _direction(html.unescape(_ANY_TAG.sub("", line))) == ("L", True):
                line = RLM + line
        in_pre = (in_pre or "<pre" in line) and "</pre>" not in line
        out.append(line)
    return "\n".join(out)


def aligned(text: str) -> str:
    """A finished message in the language of the current update (see rtl_lines)."""
    return rtl_lines(text) if LANG.get() == "fa" else text


def t(key: str, **kw) -> str:
    """The text for `key` in the language of the current update."""
    pair = TXT.get(key)
    if pair is None:
        return key
    lang = LANG.get()
    text = pair[LANGS.index(lang)] if lang in LANGS else pair[0]
    if kw:
        text = text.format(**kw)
    return rtl_lines(text) if lang == "fa" else text


def day_name(weekday: int) -> str:
    return DAYS.get(LANG.get(), DAYS["en"])[weekday % 7]


# ════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════

_SECRETS = tuple((s, label) for s, label in (
    (BOT_TOKEN, "<BOT_TOKEN>"),
    (BOT_TOKEN.partition(":")[2], "<BOT_TOKEN>"),      # also inside an encoded URL
    (API_HASH, "<API_HASH>"),
) if len(s) >= 8)


class _LogFormat(logging.Formatter):
    """The bot token and the API hash never reach the log, whoever writes the line and
    whatever is in a traceback. (The Telegram library, for one, puts the token into its
    error message when Telegram rejects it.)"""

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret, label in _SECRETS:
            if secret in text:
                text = text.replace(secret, label)
        return text


class _LogFilter(logging.Filter):
    """A failed start is reported by main() in one clear line; the library's own
    multi-page traceback about the same event adds nothing and is left out."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not (record.levelno >= logging.ERROR and record.name.startswith("telegram")
                    and "Network Retry Loop (Bootstrap" in record.getMessage())


_log_handler = logging.StreamHandler()
_log_handler.setFormatter(_LogFormat("%(asctime)s | %(levelname)-7s | %(message)s"))
_log_handler.addFilter(_LogFilter())
logging.basicConfig(
    handlers=[_log_handler],
    level=getattr(logging, env("LOG_LEVEL", "INFO").upper(), logging.INFO),
)
for _noisy in ("httpx", "httpcore", "telethon"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger("wrench")

_BG_TASKS: set[asyncio.Task] = set()
PAGE_BYTES = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def esc(s: object) -> str:
    return html.escape(str(s), quote=False)


def human(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def bar(pct: float, width: int = 10) -> str:
    """A text gauge:  ████░░░░░░"""
    filled = max(0, min(width, int(round(max(0.0, pct) / 100 * width))))
    return "█" * filled + "░" * (width - filled)


def level(pct: float, warn: float = 75, crit: float = 90) -> str:
    return "🔴" if pct >= crit else ("🟡" if pct >= warn else "🟢")


def pct(v: float, digits: int = 0) -> str:
    return f"{v:.{digits}f}" + ("٪" if LANG.get() == "fa" else "%")


def dur(seconds: float) -> str:
    """Compact duration in the current language: `3d 4h` / `3 روز 4 ساعت` style."""
    s = max(0, int(seconds))
    d, r = divmod(s, 86400)
    h, r = divmod(r, 3600)
    m, sec = divmod(r, 60)
    if d:
        parts = [t("u.d", n=d)] + ([t("u.h", n=h)] if h else [])
    elif h:
        parts = [t("u.h", n=h)] + ([t("u.m", n=m)] if m else [])
    elif m:
        parts = [t("u.m", n=m)]
    else:
        parts = [t("u.s", n=sec)]
    return " ".join(parts)


def local(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, TZ)


def stamp(ts: float) -> str:
    return local(ts).strftime("%Y-%m-%d %H:%M")


def inside(root: str, path: str) -> bool:
    r = os.path.realpath(root)
    p = os.path.realpath(path)
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


def btn(text: str, data: str, style: str | None = None) -> InlineKeyboardButton:
    """style: primary (blue) · success (green) · danger (red) — Bot API 9.4+; older
    clients and servers simply ignore it."""
    if style and BUTTON_COLORS:
        return InlineKeyboardButton(text, callback_data=data, api_kwargs={"style": style})
    return InlineKeyboardButton(text, callback_data=data)


def kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([r for r in rows if r])


def grid(buttons: list[InlineKeyboardButton], cols: int = 2) -> list[list[InlineKeyboardButton]]:
    return [buttons[i:i + cols] for i in range(0, len(buttons), cols)]


def short(name: str, n: int) -> str:
    return name if len(name) <= n else name[:n - 1] + "…"


class LazyLock:
    """An asyncio.Lock that is created on first use, inside the running event loop.
    (On Python 3.9 a lock made at import time is tied to whatever loop existed then.)"""

    def __init__(self) -> None:
        self._lock: asyncio.Lock | None = None

    def locked(self) -> bool:
        return self._lock is not None and self._lock.locked()

    async def __aenter__(self) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
        await self._lock.acquire()

    async def __aexit__(self, *exc) -> None:
        self._lock.release()


# Monitoring has a small pool of threads of its own, so that it never has to queue behind
# file work: a backup, a search, or a folder on a network drive that has stopped answering.
_WATCH_POOL = ThreadPoolExecutor(max_workers=3, thread_name_prefix="wrench-watch")


async def watch_thread(fn, *args):
    """asyncio.to_thread() for the monitor's own chores."""
    call = partial(contextvars.copy_context().run, fn, *args)
    return await asyncio.get_running_loop().run_in_executor(_WATCH_POOL, call)


def spawn(coro) -> asyncio.Task:
    """Background task with a kept reference (so it is not garbage-collected mid-flight)."""
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return task


def be_gentle() -> None:
    """Make the bot the lowest-priority process on the server."""
    with suppress(OSError):
        cur = os.nice(0)
        if cur < 10:
            os.nice(10 - cur)
    with suppress(OSError):  # when memory runs out, this bot goes first — not your apps
        with open("/proc/self/oom_score_adj", "w") as f:
            f.write("600")
    with suppress(Exception):  # I/O class "idle"
        nr = {"x86_64": 251, "aarch64": 30, "armv7l": 314, "i686": 289}.get(os.uname().machine)
        if nr:
            ctypes.CDLL(None, use_errno=True).syscall(nr, 1, 0, 3 << 13)


def clean_workdir() -> None:
    """Empty the temporary folder (the folder itself stays: under the systemd sandbox
    it is a mount point)."""
    with suppress(OSError):
        WORK_DIR.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        os.chmod(WORK_DIR, 0o700)
    with suppress(OSError):
        for entry in os.scandir(WORK_DIR):
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path, ignore_errors=True)
            else:
                with suppress(OSError):
                    os.remove(entry.path)


def skip_file(path: str, name: str) -> bool:
    """Files that never go into a backup or a search result: compiled Python, and SQLite's
    working files (data.db-wal next to data.db). A file that merely ends in "-journal" and
    has no database beside it is an ordinary file and is kept."""
    if name.endswith(".pyc"):
        return True
    for suffix in SIDE_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            return os.path.isfile(path[:-len(suffix)])
    return False


def free_bytes() -> int:
    try:
        return shutil.disk_usage(WORK_DIR).free
    except OSError:
        return 0


def read_text(path: str, limit: int = 65536) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


class PathRegistry:
    """callback_data is limited to 64 bytes, so paths travel as short ids."""

    def __init__(self, cap: int = 6000) -> None:
        self._d: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self._cap = cap

    def put(self, pkey: str, path: str) -> str:
        h = hashlib.sha1(f"{pkey}\0{path}".encode("utf-8", "surrogateescape")).hexdigest()[:10]
        self._d[h] = (pkey, path)
        self._d.move_to_end(h)
        while len(self._d) > self._cap:
            self._d.popitem(last=False)
        return h

    def resolve(self, h: str) -> "tuple[Project, str] | None":
        item = self._d.get(h)
        if not item:
            return None
        pkey, path = item
        p = PMAP.get(pkey)
        if not p or not allowed(p, path):
            return None
        return p, path


REG = PathRegistry()


async def run_cmd(*args: str, timeout: float = 15, limit: int = 2_000_000) -> str:
    """Run a read-only command with a time limit and an output size limit."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except (FileNotFoundError, PermissionError) as e:
        return f"({type(e).__name__})"
    chunks: list[bytes] = []
    size = 0

    async def pump() -> None:
        nonlocal size
        while size < limit:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            size += len(chunk)
            chunks.append(chunk)

    timed_out = False
    try:
        await asyncio.wait_for(pump(), timeout)
    except asyncio.TimeoutError:
        timed_out = True
    finally:
        if proc.returncode is None:
            with suppress(ProcessLookupError):
                proc.kill()
        with suppress(Exception):
            await asyncio.wait_for(proc.wait(), 5)
    out = b"".join(chunks).decode("utf-8", "replace").strip()
    return out or ("(TimeoutError)" if timed_out else "")


# ════════════════════════════════════════════════════════════════
#  Monitoring store (SQLite with a hard size cap)
# ════════════════════════════════════════════════════════════════

SCHEMA = """
CREATE TABLE IF NOT EXISTS sys(
    ts INTEGER PRIMARY KEY, cpu INTEGER, load INTEGER, mem INTEGER,
    swap INTEGER, disk INTEGER, rx INTEGER, tx INTEGER);
CREATE TABLE IF NOT EXISTS svc(
    ts INTEGER, sid INTEGER, up INTEGER, mem INTEGER, cpu INTEGER,
    PRIMARY KEY(ts, sid)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS logstat(
    ts INTEGER, sid INTEGER, lines INTEGER, errs INTEGER, warns INTEGER,
    PRIMARY KEY(ts, sid)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY, ts INTEGER, kind TEXT, who TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS names(sid INTEGER PRIMARY KEY, name TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS pins(
    root TEXT, rel TEXT, PRIMARY KEY(root, rel)) WITHOUT ROWID;
"""
# units: cpu/mem/swap/disk = tenths of a percent · load = hundredths · rx/tx = bytes per sample
# svc.up = percent of checks that were "active" · svc.mem = KiB · svc.cpu = tenths of % of one core


class Store:
    """Every write is safe: a database error is logged and never takes the bot down."""

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self.lock = threading.Lock()
        self.page = 4096
        self.db = self._open()

    def _open(self) -> sqlite3.Connection:
        for _ in range(2):
            db = None
            try:
                db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
                db.execute("PRAGMA journal_mode=DELETE")
                db.execute("PRAGMA synchronous=NORMAL")
                db.execute("PRAGMA temp_store=MEMORY")   # never needs a temporary folder
                self.page = db.execute("PRAGMA page_size").fetchone()[0]
                # hard cap: SQLite itself refuses to grow the file beyond this
                db.execute(f"PRAGMA max_page_count={max(64, DB_MAX_BYTES // self.page)}")
                db.executescript(SCHEMA)
                db.commit()
                return db
            except sqlite3.DatabaseError as e:
                with suppress(Exception):
                    if db is not None:
                        db.close()
                msg = str(e).lower()
                if self.path != ":memory:" and ("malformed" in msg or "not a database" in msg):
                    log.error("monitor db is corrupt (%s) — recreating it", e)
                    for suffix in ("", "-journal", "-wal", "-shm"):
                        with suppress(OSError):
                            os.remove(self.path + suffix)
                    continue
                log.error("monitor db cannot be opened (%s) — using memory only", e)
                break
        self.path = ":memory:"
        db = sqlite3.connect(":memory:", check_same_thread=False)
        db.executescript(SCHEMA)
        return db

    # ── basics ──
    def _tx(self, fn, *, retry: bool = True):
        with self.lock:
            try:
                with self.db:
                    return fn(self.db)
            except sqlite3.OperationalError as e:
                if not (retry and "full" in str(e).lower()):
                    log.warning("monitor db write failed: %s", e)
                    return None
                try:  # hit the cap: drop the oldest rows and try once more
                    with self.db:
                        self._drop_oldest(self.db, 0.2)
                    with self.db:
                        return fn(self.db)
                except sqlite3.Error as e2:
                    log.warning("monitor db write failed after shrink: %s", e2)
            except sqlite3.Error as e:
                log.warning("monitor db write failed: %s", e)
        return None

    def _q(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self.lock:
            try:
                return self.db.execute(sql, args).fetchall()
            except sqlite3.Error as e:
                log.warning("monitor db read failed: %s", e)
                return []

    @staticmethod
    def _sid(db: sqlite3.Connection, name: str) -> int:
        db.execute("INSERT OR IGNORE INTO names(name) VALUES(?)", (name,))
        return db.execute("SELECT sid FROM names WHERE name=?", (name,)).fetchone()[0]

    @staticmethod
    def _drop_oldest(db: sqlite3.Connection, frac: float) -> None:
        for table in ("sys", "svc", "logstat"):
            lo, hi = db.execute(f"SELECT MIN(ts), MAX(ts) FROM {table}").fetchone()
            if lo is not None:
                cut = lo + max(SAMPLE_EVERY, int((hi - lo) * frac))
                db.execute(f"DELETE FROM {table} WHERE ts < ?", (cut,))
        lo, hi = db.execute("SELECT MIN(id), MAX(id) FROM events").fetchone()
        if lo is not None and hi - lo > 200:
            db.execute("DELETE FROM events WHERE id < ?", (lo + int((hi - lo) * frac),))

    # ── writes ──
    def add_sample(self, sys_row: tuple, svc_rows: list[tuple], events: list[tuple]) -> None:
        def fn(db: sqlite3.Connection) -> None:
            db.execute("INSERT OR REPLACE INTO sys VALUES(?,?,?,?,?,?,?,?)", sys_row)
            for ts, name, up, mem, cpu in svc_rows:
                db.execute("INSERT OR REPLACE INTO svc VALUES(?,?,?,?,?)",
                           (ts, self._sid(db, name), up, mem, cpu))
            db.executemany("INSERT INTO events(ts,kind,who,detail) VALUES(?,?,?,?)", events)

        self._tx(fn)

    def add_logstat(self, rows: list[tuple]) -> None:
        def fn(db: sqlite3.Connection) -> None:
            for ts, name, lines, errs, warns in rows:
                db.execute("INSERT OR REPLACE INTO logstat VALUES(?,?,?,?,?)",
                           (ts, self._sid(db, name), lines, errs, warns))

        if rows:
            self._tx(fn)

    def add_event(self, kind: str, who: str, detail: str = "", ts: float | None = None) -> None:
        row = (int(ts or time.time()), kind, who, detail[:300])
        self._tx(lambda db: db.execute(
            "INSERT INTO events(ts,kind,who,detail) VALUES(?,?,?,?)", row))

    def set_meta(self, k: str, v: object) -> None:
        self._tx(lambda db: db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, str(v))))

    def get_meta(self, k: str, default: str | None = None) -> str | None:
        rows = self._q("SELECT v FROM meta WHERE k=?", (k,))
        return rows[0][0] if rows else default

    # ── pinned "essential" files (chosen by the admin, per project folder) ──
    def pins(self, root: str) -> list[str]:
        return [r[0] for r in self._q("SELECT rel FROM pins WHERE root=? ORDER BY rel", (root,))]

    def toggle_pin(self, root: str, rel: str) -> bool:
        """Returns True when the file is pinned after the call."""
        def fn(db: sqlite3.Connection) -> bool:
            if db.execute("SELECT 1 FROM pins WHERE root=? AND rel=?", (root, rel)).fetchone():
                db.execute("DELETE FROM pins WHERE root=? AND rel=?", (root, rel))
                return False
            db.execute("INSERT INTO pins VALUES(?,?)", (root, rel))
            return True

        return bool(self._tx(fn))

    # ── size upkeep ──
    def used_bytes(self) -> int:
        with self.lock:
            try:
                pages = self.db.execute("PRAGMA page_count").fetchone()[0]
                free = self.db.execute("PRAGMA freelist_count").fetchone()[0]
            except sqlite3.Error:
                return 0
        return (pages - free) * self.page

    def _expire(self, table: str, cut: int) -> None:
        # two days at a time, so the temporary journal stays small too
        for _ in range(200):
            rows = self._q(f"SELECT MIN(ts) FROM {table}")
            lo = rows[0][0] if rows else None
            if lo is None or lo >= cut:
                return
            step = min(cut, lo + 2 * 86400)
            if self._tx(lambda db: db.execute(
                    f"DELETE FROM {table} WHERE ts < ?", (step,)).rowcount) is None:
                return

    def prune(self, now: float) -> None:
        for table in ("sys", "svc", "logstat"):
            self._expire(table, int(now) - RETENTION_DAYS * 86400)
        self._expire("events", int(now) - EVENT_RETENTION_DAYS * 86400)
        for _ in range(60):
            if self.used_bytes() <= DB_SOFT_BYTES:
                break
            if self._tx(lambda db: self._drop_oldest(db, 0.03) or True) is None:
                break

    # ── reads ──
    def fetch_sys(self, t0: int, t1: int) -> list[tuple]:
        return self._q("SELECT ts,cpu,load,mem,swap,disk,rx,tx FROM sys "
                       "WHERE ts >= ? AND ts < ? ORDER BY ts", (t0, t1))

    def fetch_svc(self, t0: int, t1: int) -> list[tuple]:
        return self._q("SELECT n.name, s.up, s.mem, s.cpu FROM svc s JOIN names n USING(sid) "
                       "WHERE s.ts >= ? AND s.ts < ?", (t0, t1))

    def fetch_logstat(self, t0: int, t1: int) -> list[tuple]:
        return self._q("SELECT n.name, l.ts, l.lines, l.errs, l.warns FROM logstat l "
                       "JOIN names n USING(sid) WHERE l.ts >= ? AND l.ts < ?", (t0, t1))

    def fetch_events(self, t0: int = 0, t1: int = 2 ** 62, limit: int = 100000) -> list[tuple]:
        return self._q("SELECT ts,kind,who,detail FROM events WHERE ts >= ? AND ts < ? "
                       "ORDER BY id DESC LIMIT ?", (t0, t1, limit))

    def span(self) -> tuple[int | None, int | None, int]:
        rows = self._q("SELECT MIN(ts), MAX(ts), COUNT(*) FROM sys")
        return rows[0] if rows else (None, None, 0)

    def close(self) -> None:
        with self.lock, suppress(sqlite3.Error):
            self.db.close()


def data_dir_bytes() -> int:
    total = 0
    with suppress(OSError):
        for e in os.scandir(DATA_DIR):
            with suppress(OSError):
                total += e.stat().st_size
    return total


STORE: Store | None = None
MON: "Monitor | None" = None


class Settings:
    """Everything the admin can change from inside Telegram. Kept in the store's
    `meta` table as one small JSON document."""

    DEFAULTS: dict = {
        "alerts": True,        # service down / crash / disk / RAM / SSL / new project
        "disk_pct": 90,        # disk alert threshold
        "mem_pct": 95,         # RAM alert threshold (sustained)
        "report": "weekly",    # off | daily | weekly
        "report_wd": 0,        # weekly report day: Monday=0 … Sunday=6
        "report_hour": 9,
        "week_start": 0,       # first day of the week in charts
        "hidden": [],          # project folders hidden from the menus
        "titles": {},          # project folder → custom title
        "muted": [],           # services whose alerts are silenced
        "langs": {},           # admin id → "fa" | "en"
    }

    def __init__(self) -> None:
        self.d: dict = json.loads(json.dumps(self.DEFAULTS))

    def load(self) -> None:
        if STORE is None:
            return
        try:
            saved = json.loads(STORE.get_meta("settings", "{}") or "{}")
        except ValueError:
            saved = {}
        for k, default in self.DEFAULTS.items():
            if isinstance(saved.get(k), type(default)):
                self.d[k] = saved[k]

    def __getitem__(self, key: str):
        return self.d[key]

    def set(self, key: str, value) -> None:
        self.d[key] = value
        self.save()

    def save(self) -> None:
        if STORE is None:
            return
        blob = json.dumps(self.d, ensure_ascii=False)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            STORE.set_meta("settings", blob)
        else:
            spawn(asyncio.to_thread(STORE.set_meta, "settings", blob))


SET = Settings()


def audit(update: Update, action: str, target: str = "") -> None:
    """Record what an admin fetched (who took what) in the events list."""
    user = update.effective_user
    who = str(user.id) if user else "?"
    log.info("admin %s: %s %s", who, action, target)
    if STORE is not None:
        spawn(asyncio.to_thread(STORE.add_event, "admin", who, f"{action} {target}".strip()))


# ════════════════════════════════════════════════════════════════
#  The machine: OS, virtualisation, hardware, live counters
#  (reads /proc, /sys and /etc/os-release — nothing else)
# ════════════════════════════════════════════════════════════════

# vendor strings found in DMI → provider name shown to the admin
_PROVIDERS = (
    ("digitalocean", "DigitalOcean"), ("hetzner", "Hetzner"), ("amazon", "AWS"),
    ("google", "Google Cloud"), ("vultr", "Vultr"), ("linode", "Linode"), ("akamai", "Linode"),
    ("ovh", "OVHcloud"), ("scaleway", "Scaleway"), ("oraclecloud", "Oracle Cloud"),
    ("alibaba", "Alibaba Cloud"), ("tencent", "Tencent Cloud"), ("upcloud", "UpCloud"),
    ("contabo", "Contabo"), ("ionos", "IONOS"), ("exoscale", "Exoscale"),
    ("huawei", "Huawei Cloud"), ("openstack", "OpenStack"), ("nutanix", "Nutanix"),
)
_HYPERVISORS = (
    ("vmware", "VMware"), ("virtualbox", "VirtualBox"), ("innotek", "VirtualBox"),
    ("parallels", "Parallels"), ("xen", "Xen"), ("kvm", "KVM"), ("qemu", "KVM"),
    ("bochs", "KVM"), ("red hat", "KVM"), ("microsoft", "Hyper-V"), ("bhyve", "bhyve"),
)
_AZURE_TAG = "7783-7084-3265-9085-8269-3286-77"


@dataclass
class Host:
    hostname: str = ""
    os: str = "Linux"
    os_id: str = ""
    kernel: str = ""
    arch: str = ""
    virt: str = ""          # KVM, VMware, LXC, Docker, … or "" when unknown / bare metal
    provider: str = ""      # DigitalOcean, Hetzner, AWS, … when it can be recognised
    cpu: str = ""
    cores: int = 1
    mem_total: int = 0
    systemd: bool = False
    journal: bool = False
    docker_sock: str = ""


HOST = Host()


def _os_release() -> dict[str, str]:
    for path in OS_RELEASE:
        text = read_text(path)
        if text:
            out = {}
            for line in text.splitlines():
                k, sep, v = line.partition("=")
                if sep:
                    out[k.strip()] = v.strip().strip("\"'")
            return out
    return {}


def _detect_virt() -> tuple[str, str]:
    """(virtualisation, provider) — best effort, from files only."""
    dmi = " | ".join(read_text(os.path.join(DMI_DIR, n), 200).strip() for n in (
        "sys_vendor", "product_name", "bios_vendor", "board_vendor", "chassis_asset_tag"))
    low = dmi.lower()
    provider = ""
    if _AZURE_TAG in dmi:
        provider = "Microsoft Azure"
    else:
        for needle, name in _PROVIDERS:
            if needle in low:
                provider = name
                break
    virt = ""
    container = read_text(VIRT_FILES["container"], 64).strip()
    if os.path.exists(VIRT_FILES["vz"]) and not os.path.exists(VIRT_FILES["bc"]):
        virt = "OpenVZ"
    elif container:
        virt = {"lxc": "LXC", "lxc-libvirt": "LXC", "docker": "Docker", "podman": "Podman",
                "systemd-nspawn": "nspawn", "wsl": "WSL"}.get(container, container)
    elif os.path.exists(VIRT_FILES["docker"]):
        virt = "Docker"
    else:
        for needle, name in _HYPERVISORS:
            if needle in low:
                virt = name
                break
        hyp = read_text(VIRT_FILES["hypervisor"], 32).strip()
        if not virt and hyp:
            virt = hyp.capitalize()
        if not virt and " hypervisor" in read_text(os.path.join(PROC, "cpuinfo"), 8192):
            virt = "VM"
    if provider in ("AWS", "Google Cloud", "Microsoft Azure") and not virt:
        virt = "VM"
    return virt, provider


def detect_host() -> Host:
    h = Host()
    with suppress(Exception):
        h.hostname = socket.gethostname()
    rel = _os_release()
    h.os = rel.get("PRETTY_NAME") or rel.get("NAME") or "Linux"
    h.os_id = (rel.get("ID") or "").lower()
    with suppress(Exception):
        un = os.uname()
        h.kernel, h.arch = un.release, un.machine
    h.cores = os.cpu_count() or 1
    for line in read_text(os.path.join(PROC, "cpuinfo"), 16384).splitlines():
        key, _, val = line.partition(":")
        if key.strip() in ("model name", "Hardware", "cpu model") and val.strip():
            h.cpu = re.sub(r"\s+", " ", val.strip())
            break
    with suppress(Exception):
        h.mem_total = read_mem()[1]
    h.virt, h.provider = _detect_virt()
    h.systemd = os.path.isdir(SYSTEMD_RUN)
    h.journal = h.systemd and shutil.which("journalctl") is not None
    for sock in DOCKER_SOCKETS:
        if os.path.exists(sock):
            h.docker_sock = sock
            break
    return h


_VIRT_NAMES = {
    "kvm": "KVM", "qemu": "QEMU", "amazon": "KVM", "vmware": "VMware", "microsoft": "Hyper-V",
    "xen": "Xen", "oracle": "VirtualBox", "parallels": "Parallels", "bhyve": "bhyve",
    "lxc": "LXC", "lxc-libvirt": "LXC", "openvz": "OpenVZ", "docker": "Docker",
    "podman": "Podman", "systemd-nspawn": "nspawn", "wsl": "WSL", "zvm": "z/VM",
}


async def refine_virt(h: Host) -> None:
    """Ask systemd-detect-virt once (it reads the CPU's hypervisor id, which files cannot)."""
    out = (await run_cmd("systemd-detect-virt", timeout=5)).strip().lower()
    if out == "none":
        h.virt = ""
    elif re.fullmatch(r"[a-z0-9-]{2,20}", out):
        h.virt = _VIRT_NAMES.get(out, out.upper() if len(out) <= 4 else out.capitalize())


def host_line(h: Host) -> str:
    """`KVM · Hetzner · 2 vCPU · 3.8 GB RAM`"""
    bits = [b for b in (h.virt, h.provider) if b]
    bits.append(f"{h.cores} vCPU" if h.virt else f"{h.cores} CPU")
    if h.mem_total:
        bits.append(f"{human(h.mem_total)} RAM")
    return " · ".join(bits)


# ── live counters ──
_NET_SKIP = ("lo", "docker", "veth", "br-", "virbr", "tun", "tap", "cni", "flannel", "cali",
             "kube", "podman", "lxc", "wg", "tailscale", "zt")
_REAL_FS = frozenset({
    "ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "jfs", "reiserfs", "bcachefs",
    "vfat", "exfat", "ntfs", "ntfs3", "fuseblk", "nfs", "nfs4", "cifs", "smb3", "ceph",
    "glusterfs", "overlay", "simfs", "virtiofs", "9p",
})
_MOUNT_SKIP = ("/proc", "/sys", "/dev", "/run", "/snap", "/boot/efi", "/var/lib/docker",
               "/var/lib/containers", "/var/lib/kubelet", "/var/lib/lxcfs", "/tmp/.mount")


def read_cpu() -> tuple[int, int]:
    """(busy jiffies, total jiffies)"""
    with open(os.path.join(PROC, "stat")) as f:
        v = [int(x) for x in f.readline().split()[1:9]]
    total = sum(v)
    return total - v[3] - v[4], total


def read_mem() -> tuple[int, int, int, int]:
    """(RAM used, RAM total, swap used, swap total) in bytes"""
    mem: dict[str, int] = {}
    with open(os.path.join(PROC, "meminfo")) as f:
        for line in f:
            k, _, v = line.partition(":")
            with suppress(ValueError, IndexError):
                mem[k] = int(v.split()[0]) * 1024
    total = mem.get("MemTotal", 0)
    avail = mem.get("MemAvailable", mem.get("MemFree", 0) + mem.get("Cached", 0))
    st = mem.get("SwapTotal", 0)
    return max(0, total - avail), total, st - mem.get("SwapFree", 0), st


def read_net() -> tuple[int, int]:
    """(bytes received, bytes sent) on real network interfaces"""
    rx = tx = 0
    with suppress(OSError, ValueError, IndexError):
        with open(os.path.join(PROC, "net/dev")) as f:
            for line in f.readlines()[2:]:
                name, _, rest = line.partition(":")
                if name.strip().startswith(_NET_SKIP):
                    continue
                cols = rest.split()
                rx += int(cols[0])
                tx += int(cols[8])
    return rx, tx


def boot_time() -> float:
    for line in read_text(os.path.join(PROC, "stat"), 1 << 20).splitlines():
        if line.startswith("btime "):
            with suppress(ValueError, IndexError):
                return float(line.split()[1])
    with suppress(Exception):
        return time.time() - float(read_text(os.path.join(PROC, "uptime"), 64).split()[0])
    return time.time()


@dataclass
class Mount:
    path: str
    device: str
    fstype: str
    total: int
    used: int
    free: int
    inodes_pct: float | None = None

    @property
    def pct(self) -> float:
        """Same figure `df` prints: used / (used + available to users)."""
        return 100.0 * self.used / (self.used + self.free) if self.used + self.free else 0.0


# statvfs() on one of these never returns while its server is unreachable
_SLOW_FS = frozenset({"nfs", "nfs4", "cifs", "smb3", "ceph", "glusterfs", "9p", "fuseblk",
                      "virtiofs"})
MOUNT_WAIT = 3.0
_WAITING: dict[str, threading.Thread] = {}      # mount point → the helper still waiting for it
SILENT_MOUNTS: list[tuple[str, str]] = []       # (mount point, type) that did not answer last time


def _ask_mount(path: str, slow: bool):
    """(statvfs result, device number), or None. Network filesystems are asked from a helper
    thread and given a few seconds; while one is still waiting, no second helper is started."""
    box: list = []

    def ask() -> None:
        with suppress(OSError):
            box.append((os.statvfs(path), os.stat(path).st_dev))

    if not slow:
        ask()
        return box[0] if box else None
    old = _WAITING.get(path)
    if old is not None and old.is_alive():
        return "silent"
    helper = threading.Thread(target=ask, daemon=True, name="wrench-statvfs")
    helper.start()
    helper.join(MOUNT_WAIT)
    if helper.is_alive():
        _WAITING[path] = helper
        return "silent"
    _WAITING.pop(path, None)
    return box[0] if box else None


def read_mounts() -> list[Mount]:
    """Real filesystems, one row per device (the shortest mount point wins)."""
    seen: dict[int, Mount] = {}
    silent: list[tuple[str, str]] = []
    rows: list[tuple[str, str, str]] = []
    for line in read_text(os.path.join(PROC, "mounts"), 1 << 20).splitlines():
        cols = line.split()
        if len(cols) >= 3:
            rows.append((cols[0], cols[1].replace("\\040", " "), cols[2]))
    rows.sort(key=lambda r: len(r[1]))
    for device, path, fstype in rows:
        if fstype not in _REAL_FS:
            continue
        if path != "/" and path.startswith(_MOUNT_SKIP):
            continue
        got = _ask_mount(path, fstype in _SLOW_FS)
        if got == "silent":
            silent.append((path, fstype))
            continue
        if got is None:
            continue
        st, dev = got
        total = st.f_blocks * st.f_frsize
        if total <= 0 or dev in seen:
            continue
        if path != "/" and total < 512 * MIB:
            continue  # tiny boot / firmware partitions are noise
        free = st.f_bavail * st.f_frsize
        used = total - st.f_bfree * st.f_frsize
        m = Mount(path, device, fstype, total, used, free)
        if st.f_files:
            m.inodes_pct = 100.0 * (st.f_files - st.f_ffree) / st.f_files
        seen[dev] = m
    SILENT_MOUNTS[:] = silent
    out = sorted(seen.values(), key=lambda m: (m.path != "/", m.path))
    if not out:  # containers and odd setups: fall back to "/"
        du = shutil.disk_usage("/")
        out = [Mount("/", "", "", du.total, du.used, du.free)]
    return out


def primary_ip() -> str:
    """The address used for outgoing traffic (no packet is sent)."""
    for family, probe in ((socket.AF_INET, "192.0.2.1"), (socket.AF_INET6, "2001:db8::1")):
        with suppress(OSError):
            with closing(socket.socket(family, socket.SOCK_DGRAM)) as s:
                s.connect((probe, 9))
                return s.getsockname()[0]
    return ""


# ════════════════════════════════════════════════════════════════
#  Discovery — what is on this server?   (read-only, nothing is written)
# ════════════════════════════════════════════════════════════════
#
#  Five sources are combined into one picture:
#   1) systemd units made on this server (/etc/systemd/system): the folder of each
#      one comes from WorkingDirectory / ExecStart
#   2) well-known packaged services that are installed and in use (nginx, mysql,
#      docker, php-fpm, …)
#   3) running processes: which folder they run from, which unit they belong to —
#      this also finds things started with nohup / screen / tmux / pm2
#   4) Docker containers and their compose folders
#   5) folders that look like projects, and the sites of nginx / Apache / Caddy


@dataclass(frozen=True)
class Svc:
    """Anything with an up/down state the bot can watch."""
    sid: str            # "nginx" (systemd unit) · "d/<container>" · "p/<project key>"
    kind: str           # unit | docker | proc
    label: str
    group: str          # app (yours) | sys (packaged service) | docker | proc
    root: str = ""      # project folder, when known
    ref: str = ""       # container id


@dataclass(frozen=True)
class Project:
    key: str            # short and stable: used in buttons and zip names
    title: str
    root: str
    kind: str = "dir"   # app | site | dir | flat | cfg | extra
    services: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    flat: bool = False  # only the loose files directly inside `root`
    custom: bool = False  # title was set by the admin


@dataclass(frozen=True)
class Site:
    domains: tuple[str, ...]
    root: str
    cert: str
    server: str


@dataclass
class Proc:
    pid: int
    ppid: int
    comm: str
    ticks: int          # utime + stime
    start: int          # start time in clock ticks since boot
    rss: int            # bytes
    cmd: list[str]
    cwd: str = ""
    exe: str = ""
    unit: str = ""      # systemd system unit, "" when started by hand
    boxed: bool = False  # lives in a container
    uid: int = 0


@dataclass
class Raw:
    """What one scan found, before unit states are known."""
    roots: dict[str, set[str]] = field(default_factory=dict)       # folder → evidence
    flat: set[str] = field(default_factory=set)                    # folders shown as loose files
    names: dict[str, list[str]] = field(default_factory=dict)      # folder → top-level names
    custom: dict[str, str] = field(default_factory=dict)           # your unit → folder
    oneshots: set[str] = field(default_factory=set)
    known: list[str] = field(default_factory=list)                 # packaged services present
    live: dict[str, str] = field(default_factory=dict)             # running unit → folder
    runners: dict[str, list[int]] = field(default_factory=dict)    # folder → pids without a unit
    containers: list[dict] = field(default_factory=list)
    sites: list[Site] = field(default_factory=list)
    domains: dict[str, list[str]] = field(default_factory=dict)    # folder → domains
    certs: dict[str, str] = field(default_factory=dict)            # certificate file → label


@dataclass(frozen=True)
class Catalog:
    projects: tuple[Project, ...]
    hidden: tuple[Project, ...]
    services: tuple[Svc, ...]
    sites: tuple[Site, ...]
    certs: tuple[tuple[str, str], ...]


# the live picture: replaced as a whole by apply_catalog() after every scan
PROJECTS: tuple[Project, ...] = ()
HIDDEN: tuple[Project, ...] = ()
PMAP: dict[str, Project] = {}
SERVICES: tuple[Svc, ...] = ()
SMAP: dict[str, Svc] = {}
SITES: tuple[Site, ...] = ()
CERTS: tuple[tuple[str, str], ...] = ()

_MARK_NAMES = frozenset({
    "package.json", "requirements.txt", "pyproject.toml", "Pipfile", "Dockerfile",
    "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml", "go.mod",
    "Cargo.toml", "composer.json", "Gemfile", "pom.xml", "build.gradle", ".env", "venv",
    ".venv", "node_modules", ".git", "index.html", "index.php", "manage.py", "Makefile",
})
_MARK_SUFFIXES = (".py", ".js", ".mjs", ".ts", ".go", ".php", ".rb", ".jar", ".session")
_LOOSE_SUFFIXES = _MARK_SUFFIXES + (".sh", ".json", ".yml", ".yaml", ".env", ".conf", ".sql")
_COMPOSE_NAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
_BIN_DIRS = frozenset({"bin", "sbin", "dist", "build", "target", "release", "out", "cmd"})
_UNIT_SUFFIXES = (".service", ".socket", ".timer", ".target", ".mount", ".path", ".slice",
                  ".scope", ".swap", ".automount", ".device")
_IGNORE_UNIT_PREFIXES = ("snap.", "systemd-", "cloud-", "dbus-")
# absolute paths inside one ExecStart word (after a space, "=" or a shell operator)
_EXEC_PATH_RE = re.compile(r"""(?:^|(?<=[\s=;&|(]))/[^\s"';,&|)]+""")
_KNOWN_UNITS = re.compile(r"""^(?:
    nginx|apache2|httpd|caddy|haproxy|traefik|lighttpd|openresty|varnish|tomcat\d*
  | mysql|mysqld|mariadb|postgresql(?:@.+|-\d+)?|redis(?:-server)?(?:@.+)?|valkey(?:-server)?
  | keydb(?:-server)?|mongod|mongodb|memcached|clickhouse-server|influxdb|elasticsearch
  | opensearch|rabbitmq-server|etcd|minio|cassandra|couchdb
  | docker|containerd|podman|k3s|kubelet|php[\d.]*-fpm|supervisor|supervisord|pm2-.+
  | fail2ban|crowdsec|ufw|firewalld|ssh|sshd|cron|crond
  | wg-quick@.+|openvpn(?:-server)?(?:@.+)?|strongswan(?:-starter)?|ocserv|xray(?:@.+)?
  | v2ray(?:@.+)?|x-ui|3x-ui|sing-box(?:@.+)?|hysteria(?:-server)?(?:@.+)?|trojan(?:-go)?
  | shadowsocks.*|squid|danted|tailscaled|zerotier-one|cloudflared.*|frps|frpc
  | postfix|dovecot|exim4|named|bind9|unbound|dnsmasq|pdns(?:-recursor)?
  | grafana-server|prometheus|prometheus-node-exporter|node_exporter|netdata
  | zabbix-(?:agent2?|server)|telegraf|loki|promtail|jenkins|gitea|gitlab-runner|n8n
  | jellyfin|plexmediaserver|transmission-daemon|vsftpd|proftpd|smbd|nfs-server|webmin
  | uptime-kuma|vaultwarden
)$""", re.X)
# units that only host other programs (pm2, supervisor, cron @reboot, rc.local, an ssh
# session): the processes inside them are judged one by one
_HOST_UNITS = re.compile(r"^(?:pm2-.+|supervisor|supervisord|cron|crond|atd|ssh|sshd|"
                         r"rc-local|rc\.local|getty@.+|serial-getty@.+)$")
_BOX_RE = re.compile(r"(?:^|/)(?:docker|lxc|kubepods[^/]*|machine\.slice)(?:/|$)"
                     r"|docker-[0-9a-f]{12,}\.scope|libpod-|crio-|cri-containerd-|lxc\.payload")
_RUNNER_RE = re.compile(r"^(?:python[\d.]*|node|nodejs|deno|bun|php[\d.]*|ruby[\d.]*|java|"
                        r"dotnet|perl|gunicorn|uvicorn|celery|uwsgi|hypercorn|daphne|"
                        r"streamlit|npm|yarn|pnpm|go|caddy)$")
_NOT_RUNNER_RE = re.compile(r"^(?:ba|z|da|fi|k|c|tc)?sh$|^(?:tmux.*|screen|sudo|su|ssh|sshd|"
                            r"sftp-server|nano|vim?|nvim|emacs|less|more|tail|cat|git|top|"
                            r"htop|watch|sleep|man|mc|tee|grep|find|tar|rsync|scp|wget|curl|"
                            r"pip[\d.]*|apt.*|dpkg|yum|dnf|make|gcc|cc1.*)$", re.I)
_NGX_SERVER_RE = re.compile(r"\bserver\s*\{")
_NGX_NAME_RE = re.compile(r"\bserver_name\s+([^;]+);")
_NGX_ROOT_RE = re.compile(r"\broot\s+([^;\s]+)\s*;")
_NGX_CERT_RE = re.compile(r"\bssl_certificate\s+([^;\s]+)\s*;")
_AP_VHOST_RE = re.compile(r"<VirtualHost\b[^>]*>(.*?)</VirtualHost>", re.S | re.I)
_AP_NAME_RE = re.compile(r"^\s*Server(?:Name|Alias)\s+(.+)$", re.M | re.I)
_AP_ROOT_RE = re.compile(r"^\s*DocumentRoot\s+\"?([^\"\s]+)", re.M | re.I)
_AP_CERT_RE = re.compile(r"^\s*SSLCertificateFile\s+\"?([^\"\s]+)", re.M | re.I)
_DOMAIN_RE = re.compile(r"^(?=.{4,253}$)[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
_KIND_ORDER = {"app": 0, "site": 1, "dir": 2, "flat": 3, "cfg": 4, "extra": 5}


def _expand(patterns: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for pat in patterns:
        for b in (sorted(glob.glob(pat)) if glob.has_magic(pat) else [pat]):
            if os.path.isdir(b):
                out.append(os.path.normpath(b))
    return list(dict.fromkeys(out))


def _clean_domains(items: list[str]) -> list[str]:
    out: list[str] = []
    for raw in items:
        d = raw.strip().lower().strip("\"'")
        d = re.sub(r"^[a-z]+://", "", d).split("/", 1)[0]
        if d.count(":") == 1:
            d = d.split(":", 1)[0]
        if _DOMAIN_RE.match(d) and not re.fullmatch(r"[\d.]+", d) and d not in out:
            out.append(d)
    out.sort(key=lambda d: d.startswith("www."))  # plain name first
    return out


class _Layout:
    """Knows which folders may be project roots on this machine. One per scan."""

    def __init__(self) -> None:
        self.home = _expand(HOME_BASES)
        self.web = _expand(WEB_BASES)
        self.app = [b for b in _expand(APP_BASES + SCAN_PATHS) if b not in self.web]
        self.stop = ({os.path.normpath(p) for p in STOP_DIRS} | set(self.home) | set(self.web))
        self.system = tuple(os.path.normpath(p) for p in SYSTEM_PREFIXES)
        self.named = tuple(_expand(SCAN_PATHS))      # places the admin pointed at (SCAN_PATHS)
        self._names: dict[str, list[str]] = {}
        self.listings = 0
        self.max_listings = 900

    def is_system(self, path: str) -> bool:
        if any(path == n or path.startswith(n + "/") for n in self.named):
            return False        # the admin's word beats the built-in list (/var/lib/apps, …)
        return any(path == s or path.startswith(s + "/") for s in self.system)

    def names(self, path: str) -> list[str]:
        """Top-level names of a folder (cached; the scan as a whole has a budget)."""
        got = self._names.get(path)
        if got is None:
            got = []
            if self.listings < self.max_listings:
                self.listings += 1
                with suppress(OSError):
                    with os.scandir(path) as it:
                        for e in it:
                            got.append(e.name)
                            if len(got) >= 600:
                                break
            self._names[path] = got
        return got

    def like(self, path: str) -> bool:
        names = self.names(path)
        if "pyvenv.cfg" in names:  # a virtualenv, not a project
            return False
        return any(n in _MARK_NAMES or n.lower().endswith(_MARK_SUFFIXES) for n in names)

    def subdirs(self, base: str, cap: int = 80) -> list[str]:
        """Real, visible sub-folders. "Not allowed" simply means nothing to see (the bot
        may run as an ordinary user); any other read error is raised, so that a
        momentary failure never makes projects look "removed"."""
        out: list[str] = []
        self.listings += 1
        try:
            it = os.scandir(base)
        except PermissionError:
            return out
        with it:
            for e in it:
                if e.name.startswith(".") or e.name in IGNORE_DIRS:
                    continue
                with suppress(OSError):
                    if e.is_dir(follow_symlinks=False):
                        out.append(os.path.join(base, e.name))
        return sorted(out)[:cap]

    def root_for(self, path: str, is_dir: bool | None = None, strict: bool = False) -> str:
        """The project folder a path belongs to, or "".

        Walks down from the nearest "container" folder (/root, /home/<user>, /opt, …)
        and takes the first folder that looks like a project. When nothing does, the
        folder that was pointed at is used as it is — unless `strict`, which is how
        mere command-line arguments are treated (a --data-dir is not a project).
        A leading "=" means: loose files directly inside a home folder (/root/bot.py)."""
        if not path or not path.startswith("/"):
            return ""
        np = os.path.normpath(path)
        if is_dir is None:
            is_dir = os.path.isdir(np)
        d = np if is_dir else os.path.dirname(np)
        if self.is_system(d):
            return ""
        chain: list[str] = []
        cur = ""
        for comp in d.split("/"):
            if comp:
                cur += "/" + comp
                chain.append(cur)
        start = 0
        for i, c in enumerate(chain):
            if c in self.stop:
                start = i + 1
        below = chain[start:]
        if not below:  # a file directly in a home folder, e.g. /root/bot.py
            return "=" + d if (not is_dir and d in self.home and os.path.isfile(np)) else ""
        if start and chain[start - 1] in self.web:
            first = below[0]                 # under a web base, each sub-folder is one site
            ok = not os.path.basename(first).startswith(".") and os.path.isdir(first)
            return first if ok else ""
        for c in below[:ROOT_DEPTH]:
            name = os.path.basename(c)
            if name.startswith(".") or name in IGNORE_DIRS or name in SKIP_DIRS:
                return ""
            if self.like(c):
                return c
        if strict or len(below) > ROOT_DEPTH or not os.path.isdir(d):
            return ""
        if os.path.basename(d) in _BIN_DIRS and len(below) > 1:
            return below[-2]
        return d


# ── processes ──
def _cgroup_unit(text: str) -> tuple[str, bool]:
    """(systemd system unit, runs inside a container) from /proc/<pid>/cgroup."""
    unit, boxed = "", False
    for line in text.splitlines():
        path = line.split(":", 2)[-1]
        if _BOX_RE.search(path):
            boxed = True
        if "user@" in path or "/user.slice" in path:
            continue  # a login session or a user-level unit: not a system service
        for comp in reversed(path.split("/")):
            if comp.endswith(".service"):
                unit = unit or comp[:-len(".service")]
                break
    return unit, boxed


def scan_procs() -> list[Proc]:
    """Every user-space process, oldest first. Cheap: a few small files per process."""
    out: list[Proc] = []
    try:
        pids = [n for n in os.listdir(PROC) if n.isdigit()]
    except OSError:
        return out
    host_ns = ""
    with suppress(OSError):
        host_ns = os.readlink(os.path.join(PROC, "1", "ns", "pid"))
    for n in pids:
        base = os.path.join(PROC, n)
        try:
            with open(base + "/stat", "rb") as f:
                raw = f.read().decode("utf-8", "replace")
            comm = raw[raw.index("(") + 1:raw.rindex(")")]
            rest = raw[raw.rindex(")") + 2:].split()
            with open(base + "/cmdline", "rb") as f:
                cmd = [c.decode("utf-8", "replace") for c in f.read(4096).split(b"\0") if c]
            if not cmd:
                continue  # kernel thread
            pr = Proc(pid=int(n), ppid=int(rest[1]), comm=comm,
                      ticks=int(rest[11]) + int(rest[12]), start=int(rest[19]),
                      rss=int(rest[21]) * PAGE_BYTES, cmd=cmd)
        except (OSError, ValueError, IndexError):
            continue
        with suppress(OSError):
            pr.uid = os.stat(base).st_uid
        with suppress(OSError):
            pr.cwd = os.readlink(base + "/cwd")
        with suppress(OSError):
            pr.exe = os.readlink(base + "/exe").replace(" (deleted)", "")
        pr.unit, pr.boxed = _cgroup_unit(read_text(base + "/cgroup", 4096))
        with suppress(OSError):
            if os.readlink(base + "/root") != "/":
                pr.boxed = True  # chroot
        with suppress(OSError):
            if host_ns and os.readlink(base + "/ns/pid") != host_ns:
                pr.boxed = True  # its own PID namespace: a container
        out.append(pr)
    out.sort(key=lambda p: (p.start, p.pid))
    return out


def _proc_root(pr: Proc, lay: _Layout) -> str:
    """The project folder a process runs from, or ""."""
    if pr.cwd and pr.cwd != "/":
        r = lay.root_for(pr.cwd, True)
        if r:
            return r
    for i, arg in enumerate(pr.cmd[1:6] + pr.cmd[:1]):
        if arg.startswith("-") or len(arg) > 400:
            continue
        cand = arg if arg.startswith("/") else (os.path.join(pr.cwd, arg) if pr.cwd else "")
        if cand and os.path.isfile(cand):
            # only the program itself may define a folder without project markers
            r = lay.root_for(cand, False, strict=i < len(pr.cmd[1:6]))
            if r:
                return r
    return ""


def _is_runner(pr: Proc, root: str, now: float, btime: float) -> bool:
    """A long-running program started by hand (nohup, screen, tmux, pm2, …)."""
    # a cron child must outlive ordinary jobs before it counts (@reboot programs do)
    min_age = 3600 if pr.unit in ("cron", "crond", "atd") else 120
    if now - (btime + pr.start / CLK_TCK) < min_age:
        return False
    if pr.cmd[0].startswith("PM2 ") or pr.comm == "supervisord":
        return False  # the manager itself, not an app
    if _NOT_RUNNER_RE.match(pr.comm):
        return False
    name = os.path.basename(pr.exe) or pr.comm
    if _RUNNER_RE.match(name) or _RUNNER_RE.match(pr.comm):
        return True
    return bool(pr.exe) and pr.exe.startswith(root.lstrip("=") + "/")


# ── systemd unit files ──
def _exec_paths(value: str) -> tuple[list[str], list[str]]:
    """Absolute paths named by one ExecStart line → (the program, everything else).
    Quoted words may contain spaces; `bash -c '…'` is looked into."""
    try:
        words = shlex.split(value, posix=True)
    except ValueError:
        words = value.split()
    head: list[str] = []
    rest: list[str] = []
    for i, word in enumerate(words):
        if i == 0:
            word = word.lstrip("-@:+!|")   # systemd's ExecStart prefixes
        if word.startswith("/"):
            if " " not in word or os.path.exists(word):   # spaces only in a path that exists
                (head if i == 0 else rest).append(word)
        else:
            rest += _EXEC_PATH_RE.findall(word)
    return head, rest


def _parse_unit(path: str) -> tuple[str, list[str], str]:
    """(WorkingDirectory, every ExecStart, Type) from the [Service] section."""
    data = read_text(path, 64 * 1024)
    wd, execs, typ, section, cont = "", [], "", "", ""
    for raw in data.splitlines():
        line = (cont + " " + raw.strip()).strip() if cont else raw.strip()
        cont = ""
        if line.endswith("\\"):
            cont = line[:-1].strip()
            continue
        if not line or line[0] in "#;":
            continue
        if line.startswith("["):
            section = line
            continue
        if section != "[Service]" or "=" not in line:
            continue
        k, v = (x.strip() for x in line.split("=", 1))
        if k == "WorkingDirectory":
            wd = v.lstrip("-").strip().strip("\"'")
        elif k == "ExecStart":
            if v:
                execs.append(v)
            else:
                execs.clear()  # an empty "ExecStart=" resets the list
        elif k == "Type":
            typ = v.lower()
    return wd, execs, typ


def _known_unit_names() -> list[str]:
    """Packaged services we know how to name, that have a unit file on this machine."""
    found: set[str] = set()
    listings = [UNIT_DIR] + list(PKG_UNIT_DIRS) + glob.glob(os.path.join(UNIT_DIR, "*.wants"))
    for d in listings:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for fn in names:
            if fn.endswith(".service") and not fn.endswith("@.service"):
                name = fn[:-len(".service")]
                if _KNOWN_UNITS.match(name):
                    found.add(name)
    return sorted(found)[:80]


# ── web servers ──
def _conf_files(patterns: tuple[str, ...], cap: int = 300) -> list[str]:
    out: list[str] = []
    for pat in patterns:
        for fp in sorted(glob.glob(pat)):
            with suppress(OSError):
                if os.path.isfile(fp) and os.path.getsize(fp) <= 512 * 1024:
                    out.append(fp)
    return list(dict.fromkeys(out))[:cap]


def _caddy_sites(text: str) -> list[tuple[list[str], str]]:
    text = re.sub(r"#[^\n]*", "", text)
    out: list[tuple[list[str], str]] = []
    depth, addrs, root = 0, [], ""
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if depth == 0:
            if s.endswith("{"):
                head = s[:-1].strip()
                addrs = [] if head.startswith("(") else [a for a in re.split(r"[,\s]+", head) if a]
                root, depth = "", 1
            continue
        if depth == 1:
            m = re.match(r"root\s+(?:\*\s+)?(\S+)", s)
            if m:
                root = m.group(1)
        depth += s.count("{") - s.count("}")
        if depth <= 0:
            depth = 0
            if addrs:
                out.append((addrs, root))
    return out


def scan_sites() -> list[Site]:
    """Sites of nginx, Apache and Caddy: domains, document root, certificate."""
    sites: list[Site] = []
    for fp in _conf_files(NGINX_GLOBS):
        text = re.sub(r"#[^\n]*", "", read_text(fp, 512 * 1024))
        for block in _NGX_SERVER_RE.split(text)[1:]:
            m = _NGX_NAME_RE.search(block)
            doms = _clean_domains(m.group(1).split() if m else [])
            roots = _NGX_ROOT_RE.findall(block)
            cert = _NGX_CERT_RE.search(block)
            if doms or roots:
                sites.append(Site(tuple(doms), roots[0].strip("\"'") if roots else "",
                                  cert.group(1).strip("\"'") if cert else "", "nginx"))
    for fp in _conf_files(APACHE_GLOBS):
        text = re.sub(r"(?m)^\s*#[^\n]*", "", read_text(fp, 512 * 1024))
        for block in _AP_VHOST_RE.findall(text):
            doms = _clean_domains([x for m in _AP_NAME_RE.findall(block) for x in m.split()])
            root = _AP_ROOT_RE.search(block)
            cert = _AP_CERT_RE.search(block)
            if doms or root:
                sites.append(Site(tuple(doms), root.group(1) if root else "",
                                  cert.group(1) if cert else "", "apache"))
    for fp in CADDY_FILES:
        for addrs, root in _caddy_sites(read_text(fp, 512 * 1024)):
            doms = _clean_domains(addrs)
            if doms:
                sites.append(Site(tuple(doms), root, "", "caddy"))
    merged: dict[tuple, Site] = {}
    for s in sites:  # the :80 and :443 blocks of one site collapse into one row
        key = (s.domains, s.root)
        old = merged.get(key)
        merged[key] = s if old is None or (s.cert and not old.cert) else old
    return list(merged.values())[:200]


def _default_web(root: str, names: list[str]) -> bool:
    """An empty folder or the stock nginx/apache page — not a real site."""
    if not names:
        return True
    return os.path.basename(root) == "html" and all(
        n.startswith("index.nginx-debian") or n in ("index.html", "50x.html") for n in names)


def loose_files(base: str, cap: int = 400) -> list[str]:
    """Visible regular files directly inside a folder (names only)."""
    out: list[str] = []
    with suppress(OSError):
        with os.scandir(base) as it:
            for e in it:
                if e.name.startswith("."):
                    continue
                with suppress(OSError):
                    if e.is_file(follow_symlinks=False):
                        out.append(e.name)
                        if len(out) >= cap:
                            break
    return sorted(out, key=str.lower)


# ── the scan ──
def scan_server(containers: list[dict] | None = None) -> Raw:
    """Look at the whole machine once. Pure reading; safe to run in a worker thread."""
    lay = _Layout()
    raw = Raw()
    now, btime = time.time(), boot_time()

    def add(root: str, why: str) -> str:
        if not root:
            return ""
        real = root.lstrip("=")
        if root.startswith("="):
            raw.flat.add(real)
        raw.roots.setdefault(real, set()).add(why)
        return real

    # 1) units made on this server
    if os.path.isdir(UNIT_DIR):
        for fn in sorted(os.listdir(UNIT_DIR)):
            fp = os.path.join(UNIT_DIR, fn)
            if (not fn.endswith(".service") or fn.endswith("@.service")
                    or fn.startswith(_IGNORE_UNIT_PREFIXES)
                    or os.path.islink(fp) or not os.path.isfile(fp)):
                continue  # symlinks here are the distribution's own units
            wd, execs, typ = _parse_unit(fp)
            if not execs:
                continue
            root = lay.root_for(wd, True) if wd.startswith("/") else ""
            if not root:
                heads: list[str] = []
                rest: list[str] = []
                for v in execs:
                    h, r = _exec_paths(v)
                    heads += h           # the program itself (e.g. python inside a venv)
                    rest += r            # script and arguments — more telling
                for cand, strict in [(c, True) for c in rest] + [(c, False) for c in heads]:
                    root = lay.root_for(cand, strict=strict)
                    if root:
                        break
            name = fn[:-len(".service")]
            real = add(root, "unit")
            if typ == "oneshot":
                raw.oneshots.add(name)  # a one-off job: watching it "up/down" is meaningless
            else:
                raw.custom[name] = real

    # 2) packaged services we recognise
    raw.known = [n for n in _known_unit_names() if n not in raw.custom]

    # 3) running processes
    mains: dict[str, str] = {}
    for pr in scan_procs():
        if pr.boxed:
            continue
        hosted = bool(pr.unit) and bool(_HOST_UNITS.match(pr.unit))
        if pr.unit and not hosted:
            if pr.unit not in mains:        # only the unit's first process counts
                mains[pr.unit] = _proc_root(pr, lay)
            continue
        root = _proc_root(pr, lay)
        if root and _is_runner(pr, root, now, btime):
            raw.runners.setdefault(add(root, "proc"), []).append(pr.pid)
    for unit, root in mains.items():
        if root and not raw.custom.get(unit):
            raw.live[unit] = add(root, "unit")

    # 4) containers
    for c in (containers or [])[:MAX_CONTAINERS]:
        c = dict(c)
        c["root"] = add(lay.root_for(c.get("dir") or "", True), "docker") if c.get("dir") else ""
        raw.containers.append(c)

    # 5) sites and certificates
    for s in scan_sites():
        root = lay.root_for(s.root, True) if s.root else ""
        if root and root not in raw.roots and _default_web(root, lay.names(root)):
            root = ""                    # the web server's stock page
        if root:
            raw.domains.setdefault(add(root, "site"), []).extend(s.domains)
        if not (s.domains or root):
            continue                     # a default server block with nothing behind it
        raw.sites.append(s)
        if s.cert and os.path.isfile(s.cert):
            raw.certs.setdefault(s.cert, ", ".join(s.domains[:2]) or os.path.basename(s.cert))
    for cert in sorted(glob.glob(os.path.join(LETSENCRYPT_LIVE, "*", "cert.pem")))[:60]:
        real_cert = os.path.realpath(cert)
        known = {os.path.realpath(c) for c in raw.certs}
        full = os.path.realpath(os.path.join(os.path.dirname(cert), "fullchain.pem"))
        if real_cert not in known and full not in known:
            raw.certs[cert] = os.path.basename(os.path.dirname(cert))

    # 6) folders
    def consider(d: str, depth: int, web: bool) -> None:
        if d in lay.stop or lay.is_system(d) or d in raw.roots:
            return
        names = lay.names(d)
        if "pyvenv.cfg" in names:
            return
        if web:
            if not _default_web(d, names):
                add(d, "web")
        elif lay.like(d):
            add(d, "scan")
        elif depth < SCAN_DEPTH:
            for sub in lay.subdirs(d):
                consider(sub, depth + 1, False)

    for base in lay.home:
        for child in lay.subdirs(base):
            consider(child, 1, False)
        if base not in raw.flat and any(n.lower().endswith(_LOOSE_SUFFIXES)
                                        for n in loose_files(base)):
            add("=" + base, "scan")
    for base in lay.app:
        if lay.like(base):
            add(base, "scan")
        else:
            for child in lay.subdirs(base):
                if child not in lay.web:
                    consider(child, 1, False)
    for base in lay.web:
        for child in lay.subdirs(base):
            consider(child, 1, True)

    for root in raw.roots:
        raw.names[root] = lay.names(root)
    if lay.listings >= lay.max_listings:
        log.warning("scan: folder budget reached (%d listings) — some folders were skipped",
                    lay.listings)
    return raw


# ── from a scan to the catalog ──
def _stack_icon(names: list[str], kind: str) -> str:
    """One emoji that says what a project is made of."""
    low = {n.lower() for n in names}
    if kind == "flat":
        return "📄"
    if any(n in low for n in _COMPOSE_NAMES) or "dockerfile" in low:
        return "🐳"
    if ({"requirements.txt", "pyproject.toml", "pipfile", "manage.py"} & low
            or any(n.endswith(".py") for n in low)):
        return "🐍"
    if "package.json" in low:
        return "🟨"
    if "composer.json" in low or any(n.endswith(".php") for n in low):
        return "🐘"
    if "go.mod" in low:
        return "🐹"
    if "cargo.toml" in low:
        return "🦀"
    if {"pom.xml", "build.gradle"} & low or any(n.endswith(".jar") for n in low):
        return "☕"
    if "gemfile" in low:
        return "💎"
    if kind == "site" or {"index.html", "index.php"} & low:
        return "🌐"
    return "⚙️" if kind == "app" else "📂"


def _make_key(root: str, taken: set[str]) -> str:
    """A short, stable key (for buttons and zip names) from the folder name."""
    slug = re.sub(r"[^a-z0-9]+", "_", os.path.basename(root).lower()).strip("_")[:20]
    key = slug or "p"
    if not slug or key in taken:
        key = f"{key[:15]}_{hashlib.sha1(root.encode('utf-8', 'surrogateescape')).hexdigest()[:4]}"
    taken.add(key)
    return key


def build_catalog(raw: Raw, units: dict[str, dict] | None) -> Catalog:
    """Combine a scan with the unit states into the list the menus are made from.
    `units` is None when systemd could not be asked."""
    svcs: list[Svc] = []
    # services: systemd units
    for name in dict.fromkeys(list(raw.custom) + raw.known + list(raw.live)):
        if name in USER_IGNORE_SERVICES or name in raw.oneshots:
            continue
        custom = name in raw.custom
        root = raw.custom.get(name) or raw.live.get(name, "")
        if units is None:
            if not custom:
                continue
        else:
            st = units.get(name)
            if st is None or st.get("LoadState") in ("not-found", "masked"):
                continue  # an alias of another unit, or gone
            if not custom and not root:
                used = (st.get("ActiveState") in ("active", "activating", "reloading", "failed")
                        or (st.get("UnitFileState") or "").startswith("enabled"))
                if not used:
                    continue  # installed, but nobody uses it
                if st.get("Type") == "oneshot" and st.get("SubState") == "exited":
                    continue  # an umbrella unit such as postgresql.service on Debian
        svcs.append(Svc(name, "unit", name, "app" if (custom or root) else "sys", root))
    for c in raw.containers:
        name = c.get("name") or (c.get("id") or "")[:12]
        if name and f"d/{name}" not in USER_IGNORE_SERVICES:
            svcs.append(Svc(f"d/{name}", "docker", name, "docker", c.get("root", ""),
                            c.get("id", "")))

    hidden_roots = set(SET["hidden"])
    titles: dict = SET["titles"]
    taken: set[str] = set()
    built: list[tuple[tuple, Project]] = []
    for root in sorted(raw.roots):
        why = raw.roots[root]
        names = raw.names.get(root, [])
        flat = root in raw.flat
        mine = [s.sid for s in svcs if s.root == root]
        key = _make_key(root + ("=" if flat else ""), taken)
        if not mine and root in raw.runners:  # runs, but nothing manages it
            label = os.path.basename(root) or root
            svcs.append(Svc(f"p/{key}", "proc", label, "proc", root))
            mine = [f"p/{key}"]
        if flat:
            kind = "flat"
        elif mine:
            kind = "app"
        elif why & {"web", "site"}:
            kind = "site"
        else:
            kind = "dir"
        base_name = root if flat else (os.path.basename(root) or root)
        auto = f"{_stack_icon(names, kind)} {base_name}"
        title = str(titles.get(root) or auto)[:48]
        built.append(((_KIND_ORDER[kind], base_name.lower()), Project(
            key, title, root, kind, tuple(mine),
            tuple(dict.fromkeys(raw.domains.get(root, []))), flat, root in titles)))
    built.sort(key=lambda b: b[0])
    projects = [p for _, p in built][:MAX_PROJECTS]

    if AUTO_CONFIGS:
        for path, label in CONFIG_PLACES:
            if os.path.isdir(path) and path not in raw.roots:
                projects.append(Project(_make_key("cfg_" + label, taken), f"⚙️ {label}",
                                        path, "cfg"))
    for path in EXTRA_PATHS:
        path = os.path.normpath(path)
        if os.path.isdir(path) and all(p.root != path for p in projects):
            projects.append(Project(_make_key(path, taken), f"📌 {os.path.basename(path) or path}",
                                    path, "extra"))

    order = {"app": 0, "sys": 1, "docker": 2, "proc": 3}
    svcs.sort(key=lambda s: (order[s.group], s.label.lower()))
    certs = tuple(sorted(raw.certs.items()))[:40]
    return Catalog(
        tuple(p for p in projects if p.root not in hidden_roots),
        tuple(p for p in projects if p.root in hidden_roots),
        tuple(svcs[:MAX_SERVICES]), tuple(raw.sites), certs)


def apply_catalog(cat: Catalog) -> None:
    global PROJECTS, HIDDEN, PMAP, SERVICES, SMAP, SITES, CERTS
    PROJECTS, HIDDEN = cat.projects, cat.hidden
    PMAP = {p.key: p for p in PROJECTS}
    SERVICES = cat.services
    SMAP = {s.sid: s for s in SERVICES}
    SITES, CERTS = cat.sites, cat.certs


def flat_ok(name: str) -> bool:
    return bool(name) and not name.startswith(".")


def allowed(p: Project, path: str) -> bool:
    """May this path be shown / sent as part of project `p`?"""
    if not inside(p.root, path):
        return False
    if p.flat:
        rp, rr = os.path.realpath(path), os.path.realpath(p.root)
        if rp == rr:
            return True
        return (os.path.dirname(rp) == rr and flat_ok(os.path.basename(rp))
                and os.path.isfile(rp) and not os.path.islink(path))
    return True


def svc_token(sid: str) -> str:
    """A service id that fits callback_data (64 bytes, no ':')."""
    if ":" in sid or len(sid.encode()) > 44:
        return "#" + hashlib.sha1(sid.encode()).hexdigest()[:12]
    return sid


def svc_from_token(token: str) -> Svc | None:
    for s in SERVICES:
        if svc_token(s.sid) == token:
            return s
    return None


def svc_icon(s: Svc) -> str:
    return {"docker": "🐳", "proc": "▶️"}.get(s.kind, "⚙️")


def svc_plain(sid: str) -> str:
    """Name for charts and compact lists (no emoji: the chart font has none)."""
    s = SMAP.get(sid)
    if s is None:
        return sid.split("/", 1)[-1] if sid[:2] in ("d/", "p/") else sid
    return {"docker": f"{s.label} (docker)", "proc": f"{s.label} (process)"}.get(s.kind, s.label)


# ════════════════════════════════════════════════════════════════
#  Live state: systemd units, containers, hand-started processes
# ════════════════════════════════════════════════════════════════

SHOW_PROPS = ("Id,LoadState,ActiveState,SubState,UnitFileState,Type,Result,MemoryCurrent,"
              "CPUUsageNSec,NRestarts,ActiveEnterTimestampMonotonic,FragmentPath,Description")
ERR_RE = re.compile(
    rb"\b(?:ERROR|CRITICAL|FATAL)\b|Traceback \(most recent call last\)|\[(?:error|crit|alert|emerg)\]")
WARN_RE = re.compile(rb"\bWARN(?:ING)?\b|\[warn\]")
HTTP_5XX_RE = re.compile(rb'" 5\d\d ')
_UP_STATES = ("active",)
_BUSY_STATES = ("activating", "reloading", "deactivating", "restarting", "paused", "created")


@dataclass
class UnitState:
    state: str = "unknown"      # active | inactive | failed | activating | exited | …
    sub: str = ""
    enter: int | None = None    # changes whenever it is (re)started
    nrest: int | None = None    # automatic restarts so far
    mem: int | None = None      # bytes
    cpu_ns: int | None = None
    since: float | None = None  # when it became active (epoch)
    result: str = ""
    enabled: str = ""
    health: str = ""


@dataclass
class Acc:
    cpu0: int | None
    t0: float
    polls: int = 0
    up: int = 0
    mem: int = 0
    cpu1: int | None = None


def _num(v: object) -> int | None:
    s = str(v) if v is not None else ""
    if s.isdigit():
        n = int(s)
        return None if n >= 2 ** 63 else n
    return None


async def read_units(names: tuple[str, ...] | list[str]) -> dict[str, dict[str, str]] | None:
    """State of many units with one `systemctl show`. None = systemd could not be asked."""
    if not names:
        return {}
    units = [n if n.endswith(_UNIT_SUFFIXES) else n + ".service" for n in names]
    out = await run_cmd("systemctl", "show", *units, "-p", SHOW_PROPS, "--no-pager", timeout=20)
    if "Id=" not in out:
        return None
    res: dict[str, dict[str, str]] = {}
    for block in out.split("\n\n"):
        d = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        uid = d.get("Id", "")
        if uid:
            res[uid[:-len(".service")] if uid.endswith(".service") else uid] = d
    return res


def unit_state(d: dict[str, str], now: float, mono_us: float) -> UnitState:
    st = UnitState(
        state=d.get("ActiveState", "unknown"), sub=d.get("SubState", ""),
        enter=_num(d.get("ActiveEnterTimestampMonotonic")), nrest=_num(d.get("NRestarts")),
        mem=_num(d.get("MemoryCurrent")), cpu_ns=_num(d.get("CPUUsageNSec")),
        result=d.get("Result", ""), enabled=d.get("UnitFileState", ""),
    )
    if st.state == "active" and st.enter:
        st.since = now - max(0.0, (mono_us - st.enter) / 1e6)
    return st


# ── Docker (Engine API over the local socket; no CLI, no extra process) ──
def _dechunk(body: bytes) -> bytes:
    out, pos = b"", 0
    while pos < len(body):
        end = body.find(b"\r\n", pos)
        if end < 0:
            break
        try:
            size = int(body[pos:end].split(b";", 1)[0], 16)
        except ValueError:
            break
        if size == 0:
            break
        out += body[end + 2:end + 2 + size]
        pos = end + 2 + size + 2
    return out


async def docker_api(path: str, timeout: float = 6.0, limit: int = 8 * MIB) -> tuple[int, bytes]:
    """GET on the Docker / Podman socket → (HTTP status, body). (0, b"") = not reachable."""
    sock = HOST.docker_sock
    if not sock:
        return 0, b""
    writer = None

    async def talk() -> bytes:
        nonlocal writer
        reader, writer = await asyncio.open_unix_connection(sock)
        writer.write(f"GET {path} HTTP/1.0\r\nHost: docker\r\nAccept: */*\r\n"
                     "Connection: close\r\n\r\n".encode())
        await writer.drain()
        buf = b""
        want = None                      # total size once the headers say so
        while len(buf) < limit:
            chunk = await reader.read(65536)
            if not chunk:
                break
            buf += chunk
            if want is None and b"\r\n\r\n" in buf:
                head = buf.split(b"\r\n\r\n", 1)[0]
                m = re.search(rb"(?im)^content-length:\s*(\d+)", head)
                if m:
                    want = len(head) + 4 + int(m.group(1))
                elif b"transfer-encoding: chunked" in head.lower():
                    want = -1
            if want is not None and (len(buf) >= want > 0
                                     or (want == -1 and buf.endswith(b"0\r\n\r\n"))):
                break
        return buf

    try:
        buf = await asyncio.wait_for(talk(), timeout)
    except (OSError, asyncio.TimeoutError):
        return 0, b""
    finally:
        if writer is not None:
            with suppress(Exception):
                writer.close()
    head, _, body = buf.partition(b"\r\n\r\n")
    try:
        status = int(head.split(b" ", 2)[1])
    except (IndexError, ValueError):
        return 0, b""
    if b"transfer-encoding: chunked" in head.lower():
        body = _dechunk(body)
    return status, body


async def docker_list() -> list[dict] | None:
    """All containers (running or not). None = the daemon did not answer."""
    status, body = await docker_api("/containers/json?all=1")
    if status != 200:
        return None
    try:
        items = json.loads(body)
    except ValueError:
        return None
    out = []
    for c in items if isinstance(items, list) else []:
        labels = c.get("Labels") or {}
        names = c.get("Names") or []
        out.append({
            "id": c.get("Id", ""), "name": (names[0] if names else "").lstrip("/"),
            "state": c.get("State", ""), "status": c.get("Status", ""),
            "image": c.get("Image", ""),
            "dir": labels.get("com.docker.compose.project.working_dir", ""),
        })
    out.sort(key=lambda c: c["name"].lower())
    return out


def _read_int(path: str) -> int | None:
    return _num(read_text(path, 64).strip())


def cgroup_usage(cid: str) -> tuple[int | None, int | None]:
    """(memory bytes, CPU nanoseconds) of a container, straight from its cgroup."""
    for base in (f"{CGROUP_ROOT}/system.slice/docker-{cid}.scope", f"{CGROUP_ROOT}/docker/{cid}",
                 f"{CGROUP_ROOT}/machine.slice/libpod-{cid}.scope"):
        mem = _read_int(base + "/memory.current")
        if mem is not None:
            cpu = None
            for line in read_text(base + "/cpu.stat", 512).splitlines():
                if line.startswith("usage_usec"):
                    cpu = (_num(line.split()[-1]) or 0) * 1000
            return mem, cpu
    for mem_base, cpu_base in (
            (f"{CGROUP_ROOT}/memory/docker/{cid}", f"{CGROUP_ROOT}/cpuacct/docker/{cid}"),
            (f"{CGROUP_ROOT}/memory/system.slice/docker-{cid}.scope",
             f"{CGROUP_ROOT}/cpuacct/system.slice/docker-{cid}.scope")):
        mem = _read_int(mem_base + "/memory.usage_in_bytes")
        if mem is not None:
            return mem, _read_int(cpu_base + "/cpuacct.usage")
    return None, None


def _iso_epoch(value: str) -> float | None:
    try:
        return datetime.strptime((value or "")[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


async def docker_state(svc: Svc) -> UnitState | None:
    status, body = await docker_api(f"/containers/{svc.label}/json")
    if status == 404:
        return UnitState(state="removed")
    if status != 200:
        return None
    try:
        d = json.loads(body)
    except ValueError:
        return None
    st = d.get("State") or {}
    running = bool(st.get("Running")) and not st.get("Restarting") and not st.get("Paused")
    started = _iso_epoch(st.get("StartedAt", ""))
    mem, cpu = cgroup_usage(d.get("Id", "")) if running else (None, None)
    result = ""
    if not running and st.get("ExitCode") not in (None, 0):
        result = f"exit {st.get('ExitCode')}"
    if st.get("OOMKilled"):
        result = (result + " OOM").strip()
    return UnitState(
        state="active" if running else (st.get("Status") or "inactive"),
        sub=st.get("Status", ""), enter=int(started * 1e6) if started else None,
        nrest=_num(d.get("RestartCount")), mem=mem, cpu_ns=cpu,
        since=started if running else None, result=result,
        enabled=((d.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name", ""),
        health=(st.get("Health") or {}).get("Status", ""),
    )


async def docker_logs(name: str, lines: int) -> str:
    status, body = await docker_api(
        f"/containers/{name}/logs?stdout=1&stderr=1&timestamps=1&tail={int(lines)}",
        timeout=15, limit=8 * MIB)
    if status != 200:
        return ""
    # multiplexed stream: 8-byte header (stream, 0, 0, 0, big-endian size) + payload
    out, pos = [], 0
    if len(body) >= 8 and body[0] in (0, 1, 2) and body[1:4] == b"\0\0\0":
        while pos + 8 <= len(body):
            size = struct.unpack(">I", body[pos + 4:pos + 8])[0]
            out.append(body[pos + 8:pos + 8 + size])
            pos += 8 + size
        body = b"".join(out)
    return body.decode("utf-8", "replace").strip()


def proc_states(svcs: list[Svc], procs: list[Proc]) -> dict[str, UnitState]:
    """State of programs that run without a service manager."""
    out: dict[str, UnitState] = {}
    now, btime = time.time(), boot_time()
    loose = [p for p in procs if not p.boxed and (not p.unit or _HOST_UNITS.match(p.unit))]
    for svc in svcs:
        root = svc.root
        proj = next((p for p in PROJECTS + HIDDEN if p.root == root), None)
        flat = bool(proj and proj.flat)
        mine: list[Proc] = []
        for pr in loose:
            hit = False
            if not flat and pr.cwd and (pr.cwd == root or pr.cwd.startswith(root + "/")):
                hit = True
            else:
                for arg in pr.cmd[:6]:
                    if arg.startswith("-"):
                        continue
                    path = arg if arg.startswith("/") else (os.path.join(pr.cwd, arg) if pr.cwd else "")
                    path = os.path.normpath(path) if path else ""
                    if flat:
                        hit = os.path.dirname(path) == root and os.path.isfile(path)
                    else:
                        hit = path.startswith(root + "/")
                    if hit:
                        break
            if hit and _is_runner(pr, root, now, btime):
                mine.append(pr)
        if not mine:
            out[svc.sid] = UnitState(state="inactive", sub="not running")
            continue
        first = mine[0]
        out[svc.sid] = UnitState(
            state="active", sub=f"pid {first.pid}", enter=first.start,
            mem=sum(p.rss for p in mine),
            cpu_ns=int(sum(p.ticks for p in mine) / CLK_TCK * 1e9),
            since=btime + first.start / CLK_TCK,
        )
    return out


async def poll_states(services: tuple[Svc, ...]) -> dict[str, UnitState]:
    """Ask every source once. A service missing from the result is "unknown"."""
    out: dict[str, UnitState] = {}
    now, mono_us = time.time(), time.monotonic() * 1e6
    names = tuple(s.sid for s in services if s.kind == "unit")
    if names and HOST.systemd:
        raw = await read_units(names) or {}
        for n in names:
            d = raw.get(n)
            if d is not None and d.get("LoadState") != "not-found":
                out[n] = unit_state(d, now, mono_us)
    if HOST.docker_sock:
        for s in services:
            if s.kind == "docker":
                st = await docker_state(s)
                if st is None:
                    break                # the daemon is not answering: do not queue up behind it
                out[s.sid] = st
    runners = [s for s in services if s.kind == "proc"]
    if runners:
        procs = await watch_thread(scan_procs)
        out.update(proc_states(runners, procs))
    return out


def is_up(st: UnitState | None) -> bool:
    return st is not None and st.state in _UP_STATES


def state_icon(sid: str, st: UnitState | None) -> str:
    """🟢 running · 🟡 changing · 🔴 should run but does not · ⚪️ switched off / unknown"""
    if st is None:
        return "⚪️"
    if st.state in _UP_STATES:
        return "🟠" if st.health == "unhealthy" else "🟢"
    if st.state in _BUSY_STATES:
        return "🟡"
    if st.state == "failed" or (MON is not None and sid in MON.seen_up):
        return "🔴"
    if st.enabled.startswith("enabled") or st.enabled in ("always", "unless-stopped"):
        return "🔴"
    return "⚪️"


async def count_log(svc: str, t0: int, t1: int) -> tuple[int, int, int]:
    """(lines, errors, warnings) of a unit's journal in a period — the text is not kept."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "journalctl", "-u", svc, "--since", f"@{t0}", "--until", f"@{t1}",
            "-o", "cat", "-q", "--no-pager",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (FileNotFoundError, PermissionError):
        return 0, 0, 0
    lines = errs = warns = 0

    async def pump() -> None:
        nonlocal lines, errs, warns
        tail = b""
        while lines < LOG_SCAN_MAX_LINES:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            *full, tail = (tail + chunk).split(b"\n")
            if len(tail) > 1_000_000:
                tail = b""
            for ln in full:
                lines += 1
                if ERR_RE.search(ln):
                    errs += 1
                elif WARN_RE.search(ln):
                    warns += 1

    try:
        await asyncio.wait_for(pump(), 30)
    except asyncio.TimeoutError:
        pass
    finally:
        if proc.returncode is None:
            with suppress(ProcessLookupError):
                proc.kill()
        with suppress(Exception):
            await asyncio.wait_for(proc.wait(), 5)
    return lines, errs, warns


def cert_not_after(path: str) -> float | None:
    """Expiry time of the first certificate in a PEM file."""
    try:
        info = ssl._ssl._test_decode_cert(path)  # stdlib helper; avoids a dependency
        return float(ssl.cert_time_to_seconds(info["notAfter"]))
    except Exception:
        return None


# ════════════════════════════════════════════════════════════════
#  Keeping the picture fresh
# ════════════════════════════════════════════════════════════════

CATALOG_LOCK = LazyLock()
CATALOG_AT = 0.0                                   # time of the last scan (good or bad)
_KNOWN: tuple[set[str], set[str]] | None = None    # (folders, services) already announced
_LAST_CONTAINERS: list[dict] = []
_LAST_UNITS: dict[str, dict] | None = None
_SCAN_JOB: "asyncio.Future | None" = None
SCAN_TIMEOUT = 90.0


async def _scan_files(containers: list[dict]) -> Raw:
    """scan_server() in a worker thread. If it hangs (a project on a network mount whose
    server is gone), the caller gets an error after SCAN_TIMEOUT and no second scan is
    started behind the first one."""
    global _SCAN_JOB
    if _SCAN_JOB is not None and not _SCAN_JOB.done():
        raise TimeoutError("the previous scan of the server has not finished yet")
    job = asyncio.ensure_future(watch_thread(scan_server, containers))
    job.add_done_callback(lambda f: f.cancelled() or f.exception())   # never "unretrieved"
    _SCAN_JOB = job
    try:
        return await asyncio.wait_for(asyncio.shield(job), SCAN_TIMEOUT)
    except asyncio.TimeoutError:
        raise TimeoutError(f"scanning the server took longer than {SCAN_TIMEOUT:.0f} s") from None


async def scan_now() -> Catalog:
    global _LAST_CONTAINERS, _LAST_UNITS
    containers = await docker_list() if HOST.docker_sock else []
    if containers is None:          # daemon busy: keep what we knew instead of "losing" them
        containers = _LAST_CONTAINERS
    else:
        _LAST_CONTAINERS = containers
    raw = await _scan_files(containers)
    units = None
    if HOST.systemd:
        names = tuple(dict.fromkeys(list(raw.custom) + raw.known + list(raw.live)))
        units = await read_units(names)
        if units is None:
            units = _LAST_UNITS
        else:
            _LAST_UNITS = units
    return build_catalog(raw, units)


def _load_known() -> tuple[set[str], set[str]] | None:
    if STORE is None:
        return None
    raw = STORE.get_meta("known_roots")
    if raw is None:
        return None
    try:
        return (set(json.loads(raw)), set(json.loads(STORE.get_meta("known_svcs", "[]") or "[]")))
    except (ValueError, TypeError):
        return None


def _save_known(roots: set[str], svcs: set[str], events: list[tuple[str, str, str]]) -> None:
    if STORE is None:
        return
    STORE.set_meta("known_roots", json.dumps(sorted(roots), ensure_ascii=False))
    STORE.set_meta("known_svcs", json.dumps(sorted(svcs), ensure_ascii=False))
    for kind, who, detail in events:
        STORE.add_event(kind, who, detail)


def project_info(p: Project, states: dict[str, UnitState] | None = None) -> str:
    """One line: folder, services (with a status dot when states are given), domains."""
    bits = [f"<code>{esc(p.root)}</code>"]
    names = []
    for sid in p.services:
        s = SMAP.get(sid)
        if s is not None:
            dot = (state_icon(sid, states[sid]) + " ") if states and sid in states else ""
            names.append(f"{dot}{svc_icon(s)} {esc(s.label)}")
    if names:
        bits.append("، ".join(names) if LANG.get() == "fa" else ", ".join(names))
    shown = p.title + " " + " ".join(p.domains)
    doms = [d for d in p.domains
            if d not in p.title and not (d.startswith("www.") and d[4:] in shown)][:2]
    if doms:
        bits.append("🌍 " + ", ".join(esc(d) for d in doms))
    return " · ".join(bits)


def lang_for(uid: int) -> str:
    saved = SET["langs"].get(str(uid))
    if saved in ("fa", "en"):
        return saved
    return DEFAULT_LANG if DEFAULT_LANG in ("fa", "en") else "en"


async def notify_admins(bot, items: list[tuple[str, dict]]) -> None:
    """Send one message per admin, each in that admin's language."""
    if not items:
        return
    for uid in sorted(ADMIN_IDS):
        token = LANG.set(lang_for(uid))
        try:
            text = "\n".join(t(key, **kw) for key, kw in items)
            if len(text) > 3900:
                text = t("al.many")
            await bot.send_message(uid, text)
        except Exception as e:
            log.warning("notify %s failed: %s", uid, e)
        finally:
            LANG.reset(token)


async def sync_catalog(app: "Application | None" = None, *, force: bool = False,
                       max_age: float = RESCAN_EVERY) -> bool:
    """Scan the server again and swap in the new picture. Never raises: when a scan
    fails the previous picture stays and False is returned."""
    global CATALOG_AT, _KNOWN
    if not force and time.time() - CATALOG_AT < max_age:
        return True
    notes: list[tuple[str, dict]] = []
    try:
        async with CATALOG_LOCK:
            if not force and time.time() - CATALOG_AT < max_age:
                return True
            CATALOG_AT = time.time()
            cat = await scan_now()
            apply_catalog(cat)
            if not ADMIN_IDS:
                return True              # setup mode: nobody to tell yet
            first_ever = False
            if _KNOWN is None:
                _KNOWN = await watch_thread(_load_known)
                if _KNOWN is None:
                    first_ever, _KNOWN = True, (set(), set())
            k_roots, k_svcs = _KNOWN
            mine = [p for p in cat.projects + cat.hidden if p.kind not in ("cfg", "extra")]
            roots = {p.root for p in mine}
            svcs = {s.sid for s in cat.services}
            new_p = [p for p in mine if p.root not in k_roots]
            gone_p = sorted(k_roots - roots)
            covered = {sid for p in new_p for sid in p.services}
            new_s = [s for s in cat.services if s.sid not in k_svcs and s.sid not in covered]
            if not (first_ever or new_p or gone_p or svcs != k_svcs):
                return True
            _KNOWN = (roots, svcs)
            events: list[tuple[str, str, str]] = []
            if not first_ever:
                events += [("new", os.path.basename(p.root) or p.root, p.root) for p in new_p]
                events += [("gone", os.path.basename(r) or r, r) for r in gone_p]
                events += [("new", s.sid, "service") for s in new_s]
            await watch_thread(_save_known, roots, svcs, events)
            log.info("catalog: %d projects · %d services · %d sites · new=%d gone=%d",
                     len(cat.projects), len(cat.services), len(cat.sites),
                     len(new_p), len(gone_p))
            if first_ever:
                notes.append(("al.first", dict(
                    host=esc(HOST.hostname), os=esc(HOST.os), n=len(cat.projects),
                    s=len(cat.services), d=sum(1 for s in cat.services if s.kind == "docker"),
                    w=len(cat.sites))))
            else:
                if new_p:
                    notes.append(("al.new_head", {}))
                    for p in new_p[:12]:
                        notes.append(("al.new_item", dict(title=esc(p.title),
                                                          info=project_info(p))))
                    if len(new_p) > 12:
                        notes.append(("al.more", dict(n=len(new_p) - 12)))
                    notes.append(("al.new_tail", {}))
                if new_s:
                    notes.append(("al.new_svc", dict(names=", ".join(
                        f"<code>{esc(s.label)}</code>" for s in new_s[:20]))))
    except Exception:
        log.exception("server scan failed — keeping the previous picture")
        return False
    bot = app.bot if app is not None else (MON.app.bot if MON is not None else None)
    if notes and SET["alerts"] and bot is not None:
        await notify_admins(bot, notes)
    return True


# ════════════════════════════════════════════════════════════════
#  The monitor: one light sample a minute, reading only
# ════════════════════════════════════════════════════════════════


class Monitor:
    def __init__(self, app: Application, store: Store) -> None:
        self.app = app
        self.store = store
        self.cpu = read_cpu()
        self.net = read_net()
        self.states: dict[str, UnitState] = {}
        self.view: dict[str, UnitState] = {}
        self.view_at = 0.0
        self.acc: dict[str, Acc] = {}
        self.seen_up: set[str] = set()
        self.down_polls: dict[str, int] = {}
        self.alerted_down: set[str] = set()
        self.last_crash_alert: dict[str, float] = {}
        self.unhealthy: set[str] = set()
        self.disk_alert_day: dict[str, str] = {}
        self.mem_high = 0
        self.mem_alert_at = 0.0
        self.last: dict[str, float] = {}
        self.mounts: list[Mount] = []
        self.mounts_at = 0.0
        self.report_last: float | None = None
        self.report_retry_at = 0.0
        self.web_pos: dict[str, tuple[int, int]] = {}
        self.certs: list[tuple[str, str, float]] = []     # (label, file, expiry)
        self.certs_at = 0.0
        self.cert_alert_day: dict[str, str] = {}
        self.self_cpu = (time.process_time(), time.monotonic())
        self.self_pct = 0.0
        with suppress(Exception):
            self.scan_web()  # remember where the access logs end; counting starts now

    async def run(self) -> None:
        while True:
            await asyncio.sleep(SAMPLE_EVERY - time.time() % SAMPLE_EVERY + 0.05)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("monitor tick failed")

    async def snapshot(self, max_age: float = 20.0) -> dict[str, UnitState]:
        """Fresh states for a screen (the minute sample is reused when it is recent)."""
        if time.time() - self.view_at > max_age:
            self.view = await poll_states(SERVICES)
            self.view_at = time.time()
        return self.view

    async def get_mounts(self, max_age: float = 30.0) -> list[Mount]:
        if not self.mounts or time.time() - self.mounts_at > max_age:
            self.mounts = await watch_thread(read_mounts)
            self.mounts_at = time.time()
        return self.mounts

    async def tick(self) -> None:
        now = time.time()
        ts = int(now) // SAMPLE_EVERY * SAMPLE_EVERY
        cpu, net = read_cpu(), read_net()
        dt = cpu[1] - self.cpu[1]
        cpu_pct = 100.0 * (cpu[0] - self.cpu[0]) / dt if dt > 0 else 0.0
        rx, tx = net[0] - self.net[0], net[1] - self.net[1]
        if rx < 0 or tx < 0:  # counters were reset
            rx = tx = 0
        self.cpu, self.net = cpu, net
        mu, mt, su, st = read_mem()
        mem_pct = 100.0 * mu / mt if mt else 0.0
        swap_pct = 100.0 * su / st if st else 0.0
        mounts = await self.get_mounts(max_age=30)
        root = next((m for m in mounts if m.path == "/"), mounts[0])
        disk_pct = root.pct
        load1 = os.getloadavg()[0]
        pt, pm = time.process_time(), time.monotonic()
        if pm > self.self_cpu[1]:
            self.self_pct = 100.0 * (pt - self.self_cpu[0]) / (pm - self.self_cpu[1])
        self.self_cpu = (pt, pm)
        self.last = {"ts": ts, "cpu": cpu_pct, "mem": mem_pct, "disk": disk_pct,
                     "rx": rx / SAMPLE_EVERY, "tx": tx / SAMPLE_EVERY}
        sys_row = (ts, round(cpu_pct * 10), round(load1 * 100), round(mem_pct * 10),
                   round(swap_pct * 10), round(disk_pct * 10), rx, tx)

        events: list[tuple] = []
        alerts: list[tuple[str, dict]] = []
        mono = time.monotonic()
        if now - CATALOG_AT >= RESCAN_EVERY - 5:  # anything new on the server?
            await sync_catalog(self.app, force=True)
        services = SERVICES
        muted = set(SET["muted"])
        states = await poll_states(services)
        self.view, self.view_at = states, time.time()
        for svc in services:
            cur = states.get(svc.sid)
            if cur is None:
                continue
            sid, name = svc.sid, f"{svc_icon(svc)} <code>{esc(svc.label)}</code>"
            prev = self.states.get(sid)
            up = is_up(cur)
            loud = sid not in muted
            if prev is not None:
                was = is_up(prev)
                if cur.nrest is not None and prev.nrest is not None and cur.nrest > prev.nrest:
                    events.append((ts, "crash", sid, f"auto-restart #{cur.nrest}"))
                    if loud and now - self.last_crash_alert.get(sid, 0) > CRASH_ALERT_GAP:
                        self.last_crash_alert[sid] = now
                        alerts.append(("al.crash", dict(name=name, n=cur.nrest)))
                elif was and up and cur.enter and prev.enter and cur.enter != prev.enter:
                    events.append((ts, "restart", sid, "manual"))
                if was and not up:
                    events.append((ts, "down", sid, f"{cur.state}/{cur.sub}".strip("/")))
                elif not was and up:
                    events.append((ts, "up", sid, ""))
            self.states[sid] = cur

            if up:
                settled = svc.kind != "proc" or (cur.since is not None and now - cur.since >= 600)
                if settled:
                    self.seen_up.add(sid)
                self.down_polls[sid] = 0
                if sid in self.alerted_down:
                    self.alerted_down.discard(sid)
                    if loud:
                        alerts.append(("al.up", dict(name=name)))
                if cur.health == "unhealthy":
                    if sid not in self.unhealthy:
                        self.unhealthy.add(sid)
                        events.append((ts, "alert", sid, "unhealthy"))
                        if loud:
                            alerts.append(("al.unhealthy", dict(name=name)))
                else:
                    self.unhealthy.discard(sid)
            elif sid in self.seen_up and cur.state not in _BUSY_STATES:
                n = self.down_polls.get(sid, 0) + 1
                self.down_polls[sid] = n
                if n == 2 and sid not in self.alerted_down:
                    self.alerted_down.add(sid)
                    if loud:
                        why = "/".join(x for x in (cur.state, cur.sub, cur.result)
                                       if x and x != "success")
                        alerts.append(("al.down", dict(name=name, why=esc(why))))

            a = self.acc.get(sid)
            if a is None:
                a = self.acc[sid] = Acc(cpu0=cur.cpu_ns, t0=mono)
            a.polls += 1
            a.up += up
            a.mem = max(a.mem, cur.mem or 0)
            a.cpu1 = cur.cpu_ns

        live = {s.sid for s in services}
        for old in [n for n in self.states if n not in live]:  # removed from the server
            for d in (self.states, self.acc, self.down_polls, self.last_crash_alert):
                d.pop(old, None)
            for group in (self.seen_up, self.alerted_down, self.unhealthy):
                group.discard(old)

        svc_rows: list[tuple] = []
        if ts % SVC_EVERY == 0:
            for sid, a in self.acc.items():
                if not a.polls:
                    continue
                cpu_t = None
                if (a.cpu0 is not None and a.cpu1 is not None
                        and a.cpu1 >= a.cpu0 and mono - a.t0 > 1):
                    cpu_t = round((a.cpu1 - a.cpu0) / (mono - a.t0) / 1e9 * 1000)
                svc_rows.append((ts, sid, round(100 * a.up / a.polls),
                                 a.mem // 1024 if a.mem else None, cpu_t))
            self.acc = {n: Acc(cpu0=u.cpu_ns, t0=mono) for n, u in self.states.items()}

        day = local(now).strftime("%Y-%m-%d")
        for m in mounts:
            if m.pct >= SET["disk_pct"] and self.disk_alert_day.get(m.path) != day:
                self.disk_alert_day[m.path] = day
                events.append((ts, "alert", "disk", f"{m.path} {m.pct:.0f}%"))
                alerts.append(("al.disk", dict(path=esc(m.path), pct=f"{m.pct:.0f}",
                                               free=human(m.free))))
        if mem_pct >= SET["mem_pct"]:
            self.mem_high += 1
            if self.mem_high == MEM_ALERT_SAMPLES and now - self.mem_alert_at > 6 * 3600:
                self.mem_alert_at = now
                events.append((ts, "alert", "ram", f"{mem_pct:.0f}%"))
                alerts.append(("al.mem", dict(pct=f"{mem_pct:.0f}", n=MEM_ALERT_SAMPLES)))
        elif mem_pct < SET["mem_pct"] - 3:
            self.mem_high = 0

        await watch_thread(self.store.add_sample, sys_row, svc_rows, events)
        if SET["alerts"] and alerts:
            await notify_admins(self.app.bot, alerts)
        if ts % 3600 == 0:
            spawn(self.hourly(ts))
        await self.maybe_report(now)

    # ── hourly: count log lines, check certificates, keep the store small ──
    def scan_web(self) -> tuple[int, int] | None:
        """New website requests in the access logs (counted from the last position)."""
        hits = errs = 0
        seen_any = False
        files = [f for pat in WEB_LOG_GLOBS for f in sorted(glob.glob(pat))
                 if not f.endswith((".gz", ".bz2", ".xz", ".zst"))][:12]
        budget = 64 * MIB
        for path in files:
            try:
                st = os.stat(path)
            except OSError:
                continue
            pos = self.web_pos.get(path)
            if pos is None:                      # first sight: start from the end
                self.web_pos[path] = (st.st_ino, st.st_size)
                continue
            if pos[0] != st.st_ino or st.st_size < pos[1]:
                pos = (st.st_ino, 0)             # rotated
            seen_any = True
            try:
                with open(path, "rb") as f:
                    f.seek(pos[1])
                    while budget > 0:
                        chunk = f.read(MIB)
                        if not chunk:
                            break
                        budget -= len(chunk)
                        hits += chunk.count(b"\n")
                        errs += len(HTTP_5XX_RE.findall(chunk))
                    self.web_pos[path] = (st.st_ino, f.tell())
            except OSError:
                continue
        for gone in [p for p in self.web_pos if p not in files]:
            self.web_pos.pop(gone, None)
        return (hits, errs) if seen_any else None

    async def check_certs(self) -> list[tuple[str, dict]]:
        """Read expiry dates; return alerts for certificates that are about to expire."""
        found: list[tuple[str, str, float]] = []
        for path, label in CERTS:
            exp = await watch_thread(cert_not_after, path)
            if exp is None:
                out = await run_cmd("openssl", "x509", "-noout", "-enddate", "-in", path, timeout=5)
                m = re.search(r"notAfter=(.+)", out)
                with suppress(Exception):
                    exp = float(ssl.cert_time_to_seconds(m.group(1).strip())) if m else None
            if exp is not None:
                found.append((label, path, exp))
        found.sort(key=lambda c: c[2])
        self.certs, self.certs_at = found, time.time()
        alerts: list[tuple[str, dict]] = []
        day = local(time.time()).strftime("%Y-%m-%d")
        for label, path, exp in found:
            days = math.floor((exp - time.time()) / 86400)
            if days in (14, 7, 3, 2, 1) or days <= 0:
                if self.cert_alert_day.get(path) != day:
                    self.cert_alert_day[path] = day
                    key = "al.ssl_expired" if days < 0 else "al.ssl"
                    alerts.append((key, dict(name=esc(label), days=max(0, days),
                                             date=local(exp).strftime("%Y-%m-%d"))))
        return alerts

    async def certs_task(self) -> None:
        """Read certificate dates now (at start, then every six hours)."""
        try:
            alerts = await self.check_certs()
            if alerts and SET["alerts"]:
                await notify_admins(self.app.bot, alerts)
        except Exception:
            log.exception("certificate check failed")

    async def hourly(self, ts: int) -> None:
        try:
            rows: list[tuple] = []
            if HOST.journal:
                for svc in SERVICES:
                    if svc.kind != "unit" or svc.sid not in self.states:
                        continue
                    lines, errs, warns = await count_log(svc.sid, ts - 3600, ts)
                    if lines:
                        rows.append((ts - 3600, svc.sid, lines, errs, warns))
                    await asyncio.sleep(0.3)
            web = await watch_thread(self.scan_web)
            if web and web[0]:
                rows.append((ts - 3600, WEB_KEY, web[0], web[1], 0))
            await watch_thread(self.store.add_logstat, rows)
            await watch_thread(self.store.prune, ts)
            if CERTS and (ts % (6 * 3600) == 0 or not self.certs_at):
                await self.certs_task()
        except Exception:
            log.exception("hourly maintenance failed")

    # ── the scheduled report (daily or weekly) ──
    async def maybe_report(self, now: float) -> None:
        mode = SET["report"]
        if mode not in ("daily", "weekly"):
            return
        dt = local(now)
        slot = dt.replace(hour=int(SET["report_hour"]) % 24, minute=0, second=0, microsecond=0)
        if mode == "weekly":
            slot -= timedelta(days=(slot.weekday() - int(SET["report_wd"])) % 7)
        step = timedelta(days=7 if mode == "weekly" else 1)
        if slot > dt:
            slot -= step
        if self.report_last is None:
            try:
                self.report_last = float(
                    await watch_thread(self.store.get_meta, "report_last", "0") or 0)
            except ValueError:
                self.report_last = 0.0
        if self.report_last == 0:  # first run: start with the next slot
            await self._mark_report(now)
            return
        if self.report_last >= slot.timestamp() or now < self.report_retry_at:
            return
        if now - slot.timestamp() > (2 * 86400 if mode == "weekly" else 6 * 3600):
            await self._mark_report(now)  # the bot was off for a long time: skip this one
            return
        if mode == "weekly":
            end = slot.replace(hour=0)
            t1, t0, key = int(end.timestamp()), int((end - step).timestamp()), "rp.t_week"
        else:
            t1, t0, key = int(slot.timestamp()), int((slot - step).timestamp()), "rp.t_day"
        ok = False
        for uid in sorted(ADMIN_IDS):
            token = LANG.set(lang_for(uid))
            try:
                await send_report(self.app.bot, uid, t0, t1, t(key),
                                  "Weekly server report" if mode == "weekly"
                                  else "Daily server report", quiet=True)
                ok = True
            except Exception as e:
                log.warning("scheduled report to %s failed: %s", uid, e)
            finally:
                LANG.reset(token)
        if ok:
            await self._mark_report(now)
            log.info("%s report sent", mode)
        else:
            self.report_retry_at = now + 900

    async def _mark_report(self, now: float) -> None:
        self.report_last = now
        await watch_thread(self.store.set_meta, "report_last", now)


# ════════════════════════════════════════════════════════════════
#  Reports: the numbers
# ════════════════════════════════════════════════════════════════


@dataclass
class SvcStat:
    name: str
    up: float | None = None        # availability, percent
    mem_avg: float = 0.0           # bytes
    mem_max: float = 0.0
    cpu: float | None = None       # percent of one core
    crashes: int = 0
    restarts: int = 0
    downs: int = 0
    errs: int = 0
    warns: int = 0
    lines: int = 0


@dataclass
class Report:
    t0: int
    t1: int
    off: int = 0
    week_start: int = 0
    n: int = 0
    coverage: float = 0.0
    cpu_avg: float = 0.0
    cpu_max: float = 0.0
    cpu_max_ts: int = 0
    mem_avg: float = 0.0
    mem_max: float = 0.0
    mem_max_ts: int = 0
    load_max: float = 0.0
    swap_max: float = 0.0
    disk_first: float = 0.0
    disk_last: float = 0.0
    rx: int = 0
    tx: int = 0
    bucket: int = 3600
    tl_cpu: list = field(default_factory=list)       # average per bucket (or None)
    tl_cpu_max: list = field(default_factory=list)
    tl_mem: list = field(default_factory=list)
    hod_cpu: list = field(default_factory=list)      # 24 hours of the day
    hod_net: list = field(default_factory=list)      # bytes per hour
    heat: list = field(default_factory=list)         # 7 × 24, first row = first day of the week
    peak_hour: int | None = None
    quiet_hour: int | None = None
    net_peak_hour: int | None = None
    heat_peak: tuple[int, int] | None = None         # (row, hour)
    services: list = field(default_factory=list)
    up_avg: float | None = None
    web_hits: int = 0
    web_5xx: int = 0
    web_peak_hour: int | None = None


def build_report(store: Store, t0: int, t1: int) -> Report:
    r = Report(t0=t0, t1=t1)
    r.off = off = int(local(t1).utcoffset().total_seconds())
    r.week_start = ws = int(SET["week_start"]) % 7
    rows = store.fetch_sys(t0, t1)
    r.n = len(rows)
    span = max(1, t1 - t0)
    r.coverage = min(1.0, r.n / (span / SAMPLE_EVERY))
    r.bucket = bucket = 300 if span <= 36 * 3600 else 3600
    nb = max(1, math.ceil(span / bucket))
    b_cpu, b_mem, b_n, b_max = [0.0] * nb, [0.0] * nb, [0] * nb, [0.0] * nb
    h_cpu, h_n, h_net = [0.0] * 24, [0] * 24, [0] * 24
    g_sum = [[0.0] * 24 for _ in range(7)]
    g_n = [[0] * 24 for _ in range(7)]
    cpu_sum = mem_sum = 0.0
    for ts, cpu, load, mem, swap, disk, rx, tx in rows:
        cpu, mem = (cpu or 0) / 10, (mem or 0) / 10
        cpu_sum += cpu
        mem_sum += mem
        if cpu >= r.cpu_max:
            r.cpu_max, r.cpu_max_ts = cpu, ts
        if mem >= r.mem_max:
            r.mem_max, r.mem_max_ts = mem, ts
        r.load_max = max(r.load_max, (load or 0) / 100)
        r.swap_max = max(r.swap_max, (swap or 0) / 10)
        r.rx += rx or 0
        r.tx += tx or 0
        i = max(0, min(nb - 1, (ts - t0) // bucket))
        b_cpu[i] += cpu
        b_mem[i] += mem
        b_n[i] += 1
        b_max[i] = max(b_max[i], cpu)
        lt = ts + off
        h = (lt // 3600) % 24
        wd = ((lt // 86400) + 3) % 7          # Monday = 0
        row = (wd - ws) % 7                   # first day of the week = 0
        h_cpu[h] += cpu
        h_n[h] += 1
        h_net[h] += (rx or 0) + (tx or 0)
        g_sum[row][h] += cpu
        g_n[row][h] += 1
    if rows:
        r.cpu_avg, r.mem_avg = cpu_sum / r.n, mem_sum / r.n
        r.disk_first, r.disk_last = (rows[0][5] or 0) / 10, (rows[-1][5] or 0) / 10
    r.tl_cpu = [b_cpu[i] / b_n[i] if b_n[i] else None for i in range(nb)]
    r.tl_mem = [b_mem[i] / b_n[i] if b_n[i] else None for i in range(nb)]
    r.tl_cpu_max = [b_max[i] if b_n[i] else None for i in range(nb)]
    r.hod_cpu = [h_cpu[h] / h_n[h] if h_n[h] else None for h in range(24)]
    r.hod_net = [h_net[h] / h_n[h] * (3600 / SAMPLE_EVERY) if h_n[h] else None
                 for h in range(24)]
    r.heat = [[g_sum[d][h] / g_n[d][h] if g_n[d][h] else None for h in range(24)]
              for d in range(7)]
    known = [h for h in range(24) if r.hod_cpu[h] is not None]
    if known:
        r.peak_hour = max(known, key=lambda h: r.hod_cpu[h])
        r.quiet_hour = min(known, key=lambda h: r.hod_cpu[h])
        r.net_peak_hour = max(known, key=lambda h: r.hod_net[h] or 0)
    cells = [(r.heat[d][h], d, h) for d in range(7) for h in range(24)
             if r.heat[d][h] is not None]
    if cells:
        _, d, h = max(cells)
        r.heat_peak = (d, h)

    # ── services ──
    stats: dict[str, SvcStat] = {}
    agg: dict[str, list] = {}
    for name, up, mem, cpu in store.fetch_svc(t0, t1):
        a = agg.setdefault(name, [0, 0.0, 0, 0.0, 0.0, 0, 0.0])
        a[0] += 1
        a[1] += up or 0
        if mem:
            a[2] += 1
            a[3] += mem * 1024
            a[4] = max(a[4], mem * 1024)
        if cpu is not None:
            a[5] += 1
            a[6] += cpu / 10
    for name, a in agg.items():
        s = stats.setdefault(name, SvcStat(name))
        s.up = a[1] / a[0]
        s.mem_avg = a[3] / a[2] if a[2] else 0.0
        s.mem_max = a[4]
        s.cpu = a[6] / a[5] if a[5] else None
    for _ts, kind, who, _detail in store.fetch_events(t0, t1):
        if kind in ("crash", "restart", "down"):
            s = stats.setdefault(who, SvcStat(who))
            if kind == "crash":
                s.crashes += 1
            elif kind == "restart":
                s.restarts += 1
            else:
                s.downs += 1
    web_h = [0] * 24
    for name, ts, lines, errs, warns in store.fetch_logstat(t0, t1):
        if name == WEB_KEY:
            r.web_hits += lines or 0
            r.web_5xx += errs or 0
            web_h[((ts + off) // 3600) % 24] += lines or 0
            continue
        s = stats.setdefault(name, SvcStat(name))
        s.lines += lines or 0
        s.errs += errs or 0
        s.warns += warns or 0
    if r.web_hits:
        r.web_peak_hour = max(range(24), key=lambda h: web_h[h])
    order = {s.sid: i for i, s in enumerate(SERVICES)}
    r.services = sorted(stats.values(), key=lambda s: (order.get(s.name, 999), s.name))
    ups = [s.up for s in r.services if s.up is not None]
    r.up_avg = sum(ups) / len(ups) if ups else None
    return r


def _when(ts: int) -> str:
    dt = local(ts)
    return f"{day_name(dt.weekday())} {dt:%H:%M}"


def _hour_range(h: int) -> str:
    return f"{h:02d}:00–{(h + 1) % 24:02d}:00"


def report_text(r: Report, title: str) -> str:
    """The caption under the charts: short sections, the key figures in bold."""
    a, b = local(r.t0), local(r.t1)
    out = [
        f"📈 <b>{esc(title)}</b>",
        f"🗓 {a:%Y-%m-%d %H:%M} → {b:%Y-%m-%d %H:%M} · {esc(TZ_NAME)}",
        "",
        f"<b>{t('rp.h_res')}</b>",
        t("rp.cpu", avg=pct(r.cpu_avg, 1), peak=pct(r.cpu_max), when=_when(r.cpu_max_ts)),
        t("rp.ram", avg=pct(r.mem_avg), peak=pct(r.mem_max), when=_when(r.mem_max_ts)),
        t("rp.disk", now=pct(r.disk_last, 1), delta=f"{r.disk_last - r.disk_first:+.1f}"),
        t("rp.net", rx=human(r.rx), tx=human(r.tx)),
    ]
    rhythm = []
    if r.peak_hour is not None:
        rhythm.append(t("rp.peak", hours=_hour_range(r.peak_hour),
                        cpu=pct(r.hod_cpu[r.peak_hour], 1)))
        rhythm.append(t("rp.quiet", hours=_hour_range(r.quiet_hour)))
    if r.heat_peak is not None and r.t1 - r.t0 >= 3 * 86400:
        d, h = r.heat_peak
        rhythm.append(t("rp.busiest", day=day_name((d + r.week_start) % 7), hour=f"{h:02d}:00"))
    if r.net_peak_hour is not None:
        rhythm.append(t("rp.netpeak", hours=_hour_range(r.net_peak_hour)))
    if r.web_hits:
        line = t("rp.web", n=f"{r.web_hits:,}")
        if r.web_peak_hour is not None:
            line += " · " + t("rp.web_peak", hours=_hour_range(r.web_peak_hour))
        if r.web_5xx:
            line += " · " + t("rp.web_5xx", n=f"{r.web_5xx:,}")
        rhythm.append(line)
    if rhythm:
        out += ["", f"<b>{t('rp.h_rhythm')}</b>"] + rhythm

    def top(attr: str) -> str:
        items = sorted(((getattr(s, attr), s.name) for s in r.services if getattr(s, attr)),
                       reverse=True)[:3]
        return ", ".join(f"{esc(svc_plain(n))} ×{v}" for v, n in items)

    crashes = sum(s.crashes for s in r.services)
    restarts = sum(s.restarts for s in r.services)
    errs = sum(s.errs for s in r.services)
    out += ["", f"<b>{t('rp.h_svc')}</b>"]
    if r.up_avg is not None:
        out.append(t("rp.up", v=pct(r.up_avg, 2)))
    out.append(t("rp.crash", n=crashes) + (f" ({top('crashes')})" if crashes else "")
               + " · " + t("rp.restart", n=restarts))
    out.append(t("rp.errs", n=f"{errs:,}") + (f" ({top('errs')})" if errs else ""))
    out += ["", t("rp.cover", v=pct(r.coverage * 100), n=f"{r.n:,}")]
    return "\n".join(out)


def visible_len(text: str) -> int:
    """Length Telegram counts for a caption: without tags, in UTF-16 units."""
    plain = html.unescape(re.sub(r"<[^>]+>", "", text))
    return len(plain.encode("utf-16-le")) // 2


# ════════════════════════════════════════════════════════════════
#  Reports: the charts (Pillow, in memory — nothing touches the disk)
# ════════════════════════════════════════════════════════════════

C_PAGE = (13, 13, 13)
C_SURF = (26, 26, 25)
C_TEXT = (255, 255, 255)
C_TEXT2 = (195, 194, 183)
C_MUTED = (137, 135, 129)
C_GRID = (44, 44, 42)
C_AXIS = (56, 56, 53)
C_EMPTY = (36, 36, 34)
C_DIM = (78, 78, 73)
C_CPU = (57, 135, 229)
C_MEM = (217, 89, 38)
C_NET = (25, 158, 112)
C_GOOD = (12, 163, 12)
C_WARN = (250, 178, 25)
C_CRIT = (208, 59, 59)
# single-hue ramp for the heat map: low (dark, near the surface) → high (bright)
SEQ = [(13, 54, 107), (16, 66, 129), (24, 79, 149), (28, 92, 171), (37, 106, 191),
       (42, 120, 214), (57, 135, 229), (85, 152, 231), (109, 167, 236),
       (134, 182, 239), (158, 197, 244), (183, 211, 246), (205, 226, 251)]
EN_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
IMG_W = 1080
_FONT_DIRS = ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu-sans-fonts",
              "/usr/share/fonts/dejavu", "/usr/share/fonts/TTF", "/usr/share/fonts/truetype",
              "/usr/local/share/fonts")
_FONT_FILES = {False: "DejaVuSans.ttf", True: "DejaVuSans-Bold.ttf"}


@lru_cache(maxsize=64)
def _font(px: int, bold: bool):
    for d in _FONT_DIRS:
        with suppress(OSError):
            return ImageFont.truetype(os.path.join(d, _FONT_FILES[bold]), px)
    try:
        return ImageFont.load_default(size=px)   # Pillow's built-in font (10.1+)
    except TypeError:
        return ImageFont.load_default()


def seq_color(frac: float) -> tuple[int, int, int]:
    x = max(0.0, min(1.0, frac)) * (len(SEQ) - 1)
    i = min(len(SEQ) - 2, int(x))
    f = x - i
    return tuple(round(SEQ[i][k] + (SEQ[i + 1][k] - SEQ[i][k]) * f) for k in range(3))


def nice_scale(vmax: float, n: int = 4, cap: float | None = None) -> tuple[float, list[float]]:
    """Axis top and grid lines on round numbers."""
    if vmax <= 0:
        vmax = 1.0
    raw = vmax / n
    mag = 10 ** math.floor(math.log10(raw))
    step = mag
    for m in (1, 2, 2.5, 5, 10):
        step = m * mag
        if step >= raw:
            break
    top = math.ceil(vmax / step - 1e-9) * step
    if cap is not None and top > cap:
        top, step = cap, cap / n
    return top, [i * step for i in range(int(round(top / step)) + 1)]


def _num_label(v: float) -> str:
    return f"{v:.0f}" if abs(v - round(v)) < 0.05 else f"{v:.1f}"


class Canvas:
    """Draws at twice the size and shrinks at the end, so lines come out smooth."""

    S = 2

    def __init__(self, w: int, h: int) -> None:
        self.w, self.h = int(w), int(h)
        self.im = Image.new("RGB", (self.w * self.S, self.h * self.S), C_PAGE)
        self.d = ImageDraw.Draw(self.im, "RGBA")

    def _p(self, v: float) -> int:
        return int(round(v * self.S))

    def rect(self, x0, y0, x1, y1, fill, r: float = 0, top_only: bool = False) -> None:
        a, b, c, d = self._p(x0), self._p(y0), self._p(x1) - 1, self._p(y1) - 1
        if c < a or d < b:
            return
        rad = int(min(self._p(r), (c - a) // 2, (d - b) if top_only else (d - b) // 2))
        if rad <= 0:
            self.d.rectangle([a, b, c, d], fill=fill)
        else:
            corners = (True, True, False, False) if top_only else (True, True, True, True)
            self.d.rounded_rectangle([a, b, c, d], radius=rad, fill=fill, corners=corners)

    def outline(self, x0, y0, x1, y1, color, width: float = 2, r: float = 0) -> None:
        self.d.rounded_rectangle(
            [self._p(x0), self._p(y0), self._p(x1) - 1, self._p(y1) - 1],
            radius=self._p(r), outline=color, width=self._p(width))

    def line(self, pts, fill, width: float = 2) -> None:
        if len(pts) < 2:
            return
        w = max(1, self._p(width))
        xy = [(self._p(x), self._p(y)) for x, y in pts]
        self.d.line(xy, fill=fill, width=w, joint="curve")
        if w > 2:  # round caps
            for x, y in (xy[0], xy[-1]):
                self.d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill=fill)

    def poly(self, pts, fill) -> None:
        if len(pts) >= 3:
            self.d.polygon([(self._p(x), self._p(y)) for x, y in pts], fill=fill)

    def dot(self, x, y, r, fill, ring=None) -> None:
        cx, cy = self._p(x), self._p(y)
        if ring is not None:
            rr = self._p(r + 2)
            self.d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=ring)
        rr = self._p(r)
        self.d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=fill)

    def text(self, x, y, s, size, fill=C_TEXT, anchor: str = "la", bold: bool = False) -> None:
        self.d.text((self._p(x), self._p(y)), str(s), font=_font(self._p(size), bold),
                    fill=fill, anchor=anchor)

    def tw(self, s, size, bold: bool = False) -> float:
        return _font(self._p(size), bold).getlength(str(s)) / self.S

    def fit(self, s: str, size, maxw: float, bold: bool = False) -> str:
        s = str(s)
        if self.tw(s, size, bold) <= maxw:
            return s
        while len(s) > 1 and self.tw(s + "..", size, bold) > maxw:
            s = s[:-1]
        return s + ".."

    def png(self) -> bytes:
        lanczos = getattr(Image, "Resampling", Image).LANCZOS
        im = self.im.resize((self.w, self.h), lanczos)
        buf = io.BytesIO()
        im.save(buf, "PNG", optimize=True)
        return buf.getvalue()


def _ascii(s: str) -> str:
    """The chart font has no emoji or Persian glyphs; keep what it can draw."""
    out = "".join(ch if 32 <= ord(ch) < 0x250 else " " for ch in str(s))
    return re.sub(r"\s+", " ", out).strip() or "?"


def _card(c: Canvas, x, y, w, h, title: str, sub: str = "") -> None:
    c.rect(x, y, x + w, y + h, C_SURF, r=18)
    c.text(x + 24, y + 22, title, 26, C_TEXT, bold=True)
    if sub:
        c.text(x + 24, y + 60, c.fit(sub, 19, w - 48), 19, C_MUTED)


def _tile(c: Canvas, x, y, w, h, label: str, value: str, sub: str) -> None:
    c.rect(x, y, x + w, y + h, C_SURF, r=18)
    c.text(x + 20, y + 16, label, 20, C_MUTED)
    c.text(x + 20, y + 44, c.fit(value, 40, w - 40, True), 40, C_TEXT, bold=True)
    c.text(x + 20, y + h - 34, c.fit(sub, 19, w - 40), 19, C_TEXT2)


def _time_ticks(t0: int, t1: int, off: int) -> tuple[int, list[tuple[int, str]]]:
    """(step, [(moment, label)]) for the time axis. In charts of more than a day the
    midnights carry the date, so «12:00» is never ambiguous."""
    span = t1 - t0
    step = 7 * 86400
    for step in (3600, 3 * 3600, 6 * 3600, 12 * 3600, 86400, 2 * 86400, 5 * 86400, 7 * 86400):
        if span / step <= 8:
            break
    day_fmt = "%a %d" if span <= 10 * 86400 else "%d %b"
    ticks = []
    tick = ((t0 + off + step - 1) // step) * step - off
    while tick <= t1:
        midnight = (tick + off) % 86400 == 0
        fmt = day_fmt if step >= 86400 or (midnight and span > 30 * 3600) else "%H:%M"
        ticks.append((tick, local(tick).strftime(fmt)))
        tick += step
    return step, ticks


def _chart_line(c: Canvas, box, r: Report, vals: list, color, *, shade: list | None = None,
                mark: tuple[int, float, str] | None = None) -> None:
    """Average over time (+ the peak inside each bucket as a shade), percent axis."""
    x0, y0, x1, y1 = box
    peak = max([v for v in (shade or vals) if v is not None] + [1.0])
    if mark:
        peak = max(peak, mark[1])
    top, ticks = nice_scale(peak, cap=100.0)

    def X(tm: float) -> float:
        return x0 + (tm - r.t0) / max(1, r.t1 - r.t0) * (x1 - x0)

    def Y(v: float) -> float:
        return y1 - min(v, top) / top * (y1 - y0)

    for v in ticks:
        c.line([(x0, Y(v)), (x1, Y(v))], C_AXIS if v == 0 else C_GRID, 1)
        c.text(x0 - 12, Y(v), f"{_num_label(v)}%", 19, C_MUTED, anchor="rm")
    step, time_ticks = _time_ticks(r.t0, r.t1, r.off)
    if step == 86400:
        # day by day: the name sits under the middle of its day, a small mark at each midnight
        day = ((r.t0 + r.off) // 86400) * 86400 - r.off
        while day < r.t1:
            a, b = max(day, r.t0), min(day + 86400, r.t1)
            label = local(a).strftime("%a %d")
            if X(b) - X(a) >= c.tw(label, 19) + 10:
                c.text((X(a) + X(b)) / 2, y1 + 12, label, 19, C_MUTED, anchor="ma")
            if day > r.t0:
                c.line([(X(day), y1), (X(day), y1 + 7)], C_AXIS, 1)
            day += 86400
    else:
        for tm, label in time_ticks:
            x = X(tm)
            c.text(x, y1 + 12, label, 19, C_MUTED,
                   anchor="ra" if x > x1 - 34 else ("la" if x < x0 + 34 else "ma"))

    def runs(series: list) -> list[list[tuple[float, float]]]:
        out, cur = [], []
        for i, v in enumerate(series):
            if v is None:  # the line breaks where there is no data
                if cur:
                    out.append(cur)
                cur = []
            else:
                cur.append((X(min(r.t1, r.t0 + (i + 0.5) * r.bucket)), Y(v)))
        if cur:
            out.append(cur)
        return out

    if shade:
        for run in runs(shade):
            if len(run) > 1:
                c.poly([(run[0][0], y1)] + run + [(run[-1][0], y1)], color + (56,))
    for run in runs(vals):
        if len(run) > 1:
            c.line(run, color, 2.5)
        else:
            c.dot(run[0][0], run[0][1], 3, color)
    if mark:
        ts, v, label = mark
        x, y = X(max(r.t0, min(r.t1, ts))), Y(v)
        c.dot(x, y, 5, C_TEXT, ring=C_SURF)
        half = c.tw(label, 19) / 2
        c.text(max(x0 + half, min(x1 - half, x)), y - 12, label, 19, C_TEXT2, anchor="mb")


def _chart_hours(c: Canvas, box, vals: list, color, hi: int | None, fmt) -> None:
    """24 columns for the hours of the day; only the peak hour is coloured."""
    x0, y0, x1, y1 = box
    known = [v for v in vals if v is not None]
    top, ticks = nice_scale(max(known + [0.0]) or 1.0)
    slot = (x1 - x0) / 24
    bw = min(24.0, slot - 6)
    for v in ticks:
        y = y1 - v / top * (y1 - y0)
        c.line([(x0, y), (x1, y)], C_AXIS if v == 0 else C_GRID, 1)
        c.text(x0 - 12, y, fmt(v), 19, C_MUTED, anchor="rm")
    for h, v in enumerate(vals):
        xm = x0 + slot * (h + 0.5)
        if h % 3 == 0:
            c.text(xm, y1 + 12, f"{h:02d}", 19, C_MUTED, anchor="ma")
        if v is None:
            continue
        y = y1 - min(v, top) / top * (y1 - y0)
        c.rect(xm - bw / 2, min(y, y1 - 1), xm + bw / 2, y1, color if h == hi else C_DIM,
               r=4, top_only=True)
        if h == hi:
            label = fmt(v)
            half = c.tw(label, 19, True) / 2
            c.text(max(x0 + half, min(x1 - half, xm)), y - 8, label, 19, C_TEXT,
                   anchor="mb", bold=True)


def _chart_heat(c: Canvas, box, r: Report) -> None:
    """Weekday × hour; brighter = busier."""
    x0, y0, x1, y1 = box
    days = [EN_DAYS[(d + r.week_start) % 7] for d in range(7)]
    vals = [v for row in r.heat for v in row if v is not None]
    vmax = max(vals + [1.0])
    cw, ch = (x1 - x0) / 24, (y1 - y0) / 7
    for d in range(7):
        c.text(x0 - 14, y0 + ch * (d + 0.5), days[d], 19, C_MUTED, anchor="rm")
        for h in range(24):
            v = r.heat[d][h]
            fill = C_EMPTY if v is None else seq_color(v / vmax)
            c.rect(x0 + cw * h + 1, y0 + ch * d + 1, x0 + cw * (h + 1) - 1,
                   y0 + ch * (d + 1) - 1, fill, r=4)
    for h in range(0, 24, 3):
        c.text(x0 + cw * (h + 0.5), y1 + 10, f"{h:02d}", 19, C_MUTED, anchor="ma")
    if r.heat_peak:
        d, h = r.heat_peak
        c.outline(x0 + cw * h - 1, y0 + ch * d - 1, x0 + cw * (h + 1) + 1,
                  y0 + ch * (d + 1) + 1, C_TEXT, width=2, r=5)
    # colour key:  0% [ramp] max
    ly, lw = y1 + 54, 18.0
    hi = f"{vmax:.0f}%" if vmax >= 10 else f"{vmax:.1f}%"
    c.text(x1, ly + 8, hi, 19, C_MUTED, anchor="rm")
    lx0 = x1 - c.tw(hi, 19) - 10 - len(SEQ) * lw
    for i, col in enumerate(SEQ):
        c.rect(lx0 + i * lw, ly, lx0 + (i + 1) * lw, ly + 16, col)
    c.text(lx0 - 10, ly + 8, "0%", 19, C_MUTED, anchor="rm")
    if r.heat_peak:
        d, h = r.heat_peak
        c.text(x0, ly + 8, f"Busiest: {days[d]} {h:02d}:00-{(h + 1) % 24:02d}:00  (avg CPU {hi})",
               19, C_TEXT2, anchor="lm")


def _header(c: Canvas, title: str, sub: str) -> None:
    c.text(32, 30, title, 38, C_TEXT, bold=True)
    c.text(32, 84, c.fit(sub, 21, IMG_W - 64), 21, C_MUTED)


def _period(r: Report) -> str:
    a, b = local(r.t0), local(r.t1)
    host = _ascii(HOST.hostname) if HOST.hostname else ""
    return (f"{a:%a %d %b %H:%M} - {b:%a %d %b %H:%M}  ({TZ_NAME})"
            + (f"  ·  {host}" if host else ""))


def _legend(c: Canvas, x_right: float, y: float, items: list[tuple[str, tuple, str]]) -> None:
    x = x_right
    for label, color, kind in reversed(items):
        x -= c.tw(label, 19)
        c.text(x, y, label, 19, C_TEXT2, anchor="lm")
        x -= 12
        if kind == "line":
            c.line([(x - 26, y), (x, y)], color, 2.5)
        else:
            c.rect(x - 26, y - 8, x, y + 8, color + (90,), r=3)
        x -= 26 + 22


def render_report(r: Report, title_en: str) -> list[bytes]:
    """Three images: overview · peak hours · services"""
    M, W = 32, IMG_W
    cw = W - 2 * M
    out: list[bytes] = []
    long = r.t1 - r.t0 >= 3 * 86400
    per = "hourly" if r.bucket == 3600 else "5-minute"
    period = _period(r)

    # ── 1) overview ──
    c = Canvas(W, 1364)
    _header(c, title_en, period)
    tw_, th, gap = (cw - 32) / 3, 132, 16
    ph = r.peak_hour
    up = f"{r.up_avg:.2f}%" if r.up_avg is not None else "n/a"
    crashes = sum(s.crashes for s in r.services)
    cpu_peak = f"{r.cpu_max:.0f}%  {local(r.cpu_max_ts):%a %H:%M}"
    mem_peak = f"{r.mem_max:.0f}%  {local(r.mem_max_ts):%a %H:%M}"
    tiles = [
        ("CPU average", f"{r.cpu_avg:.1f}%", "peak " + cpu_peak),
        ("RAM average", f"{r.mem_avg:.0f}%", "peak " + mem_peak),
        ("Peak hour", f"{ph:02d}:00" if ph is not None else "n/a",
         f"avg CPU {r.hod_cpu[ph]:.1f}%" if ph is not None else ""),
        ("Network", human(r.rx + r.tx), f"in {human(r.rx)} / out {human(r.tx)}"),
        ("Services uptime", up, f"{crashes} crash(es) auto-restarted"),
        ("Disk used", f"{r.disk_last:.1f}%",
         f"{r.disk_last - r.disk_first:+.1f} pt in this period"),
    ]
    for i, (label, value, sub) in enumerate(tiles):
        _tile(c, M + (i % 3) * (tw_ + gap), 136 + (i // 3) * (th + gap), tw_, th,
              label, value, sub)
    y = 136 + 2 * th + gap + 20
    for name, vals, color, shade, mark in (
        ("CPU usage", r.tl_cpu, C_CPU, r.tl_cpu_max, (r.cpu_max_ts, r.cpu_max, cpu_peak)),
        ("RAM usage", r.tl_mem, C_MEM, None, (r.mem_max_ts, r.mem_max, mem_peak)),
    ):
        h = 440
        _card(c, M, y, cw, h, name,
              f"{per} average (line), peak inside each interval (shade)" if shade
              else f"{per} average, the dot marks the highest single sample")
        if shade:
            _legend(c, M + cw - 24, y + 38, [("average", color, "line"), ("peak", color, "area")])
        _chart_line(c, (M + 84, y + 124, M + cw - 30, y + h - 58), r, vals, color,
                    shade=shade, mark=mark)
        y += h + 16
    out.append(c.png())

    # ── 2) peak hours ──
    hh = 452
    c = Canvas(W, 136 + (hh + 16 if long else 0) + 2 * 400 + 16 + 32)
    _header(c, "Peak hours", period)
    y = 136
    if long:
        _card(c, M, y, cw, hh, "CPU by weekday and hour",
              "average CPU % - brighter means busier, the outlined cell is the busiest")
        _chart_heat(c, (M + 84, y + 104, M + cw - 30, y + 104 + 7 * 36), r)
        y += hh + 16
    _card(c, M, y, cw, 400, "CPU by hour of day",
          "average CPU % in each hour - the peak hour is highlighted")
    _chart_hours(c, (M + 84, y + 124, M + cw - 30, y + 400 - 58), r.hod_cpu, C_CPU,
                 r.peak_hour, lambda v: f"{_num_label(v)}%")
    y += 416
    net_top = max([v for v in r.hod_net if v is not None] + [0.0])
    unit, div = "B", 1
    for u, dv in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if net_top >= dv:
            unit, div = u, dv
            break
    _card(c, M, y, cw, 400, "Network traffic by hour of day",
          "average traffic per hour (in + out) - the busiest hour is highlighted")
    _chart_hours(c, (M + 110, y + 124, M + cw - 30, y + 400 - 58),
                 [v / div if v is not None else None for v in r.hod_net], C_NET,
                 r.net_peak_hour, lambda v: f"{_num_label(v)} {unit}")
    out.append(c.png())

    # ── 3) services ──
    rows = r.services[:40]
    rh = 58
    ch = 86 + max(1, len(rows)) * rh + 24
    c = Canvas(W, 136 + ch + 22 + 3 * 28 + 18)
    _header(c, "Services", period)
    c.rect(M, 136, M + cw, 136 + ch, C_SURF, r=18)
    x_name, x_up, x_cr, x_err, x_cpu, x_b0, x_b1, x_ram = 56, 448, 548, 648, 742, 772, 912, 1024
    for label, x, anchor in (("Service", x_name, "lm"), ("Uptime", x_up, "rm"),
                             ("Crashes", x_cr, "rm"), ("Errors", x_err, "rm"),
                             ("CPU", x_cpu, "rm"), ("RAM avg", x_ram, "rm")):
        c.text(x, 136 + 44, label, 19, C_MUTED, anchor=anchor)
    c.line([(56, 136 + 72), (M + cw - 24, 136 + 72)], C_AXIS, 1)
    mem_top = max([s.mem_avg for s in rows] + [1.0])
    if not rows:
        c.text(56, 136 + 86 + rh / 2, "No service data in this period yet.", 21, C_MUTED,
               anchor="lm")
    for i, s in enumerate(rows):
        y = 136 + 86 + i * rh + rh / 2
        if i:
            c.line([(56, y - rh / 2), (M + cw - 24, y - rh / 2)], C_GRID, 1)
        c.text(x_name, y, c.fit(_ascii(svc_plain(s.name)), 21, 270), 21, C_TEXT, anchor="lm")
        if s.up is None:
            c.text(x_up, y, "n/a", 21, C_MUTED, anchor="rm")
        else:
            label = f"{s.up:.1f}%" if s.up < 99.95 else "100%"
            col = C_GOOD if s.up >= 99.9 else (C_WARN if s.up >= 95 else C_CRIT)
            c.text(x_up, y, label, 21, C_TEXT, anchor="rm")
            c.dot(x_up - c.tw(label, 21) - 14, y, 5, col)
        c.text(x_cr, y, str(s.crashes), 21, C_TEXT if s.crashes else C_MUTED, anchor="rm")
        c.text(x_err, y, f"{s.errs:,}", 21, C_TEXT if s.errs else C_MUTED, anchor="rm")
        c.text(x_cpu, y, f"{s.cpu:.1f}%" if s.cpu is not None else "-", 21, C_TEXT2,
               anchor="rm")
        if s.mem_avg:
            c.rect(x_b0, y - 7, x_b0 + max(4, (x_b1 - x_b0) * s.mem_avg / mem_top), y + 7,
                   C_MEM, r=4)
            c.text(x_ram, y, human(s.mem_avg), 21, C_TEXT, anchor="rm")
        else:
            c.text(x_ram, y, "-", 21, C_MUTED, anchor="rm")
    for i, line in enumerate((
        "Uptime: share of 1-minute checks where the service was running.",
        "Crashes: automatic restarts.  CPU: percent of one core.",
        "Errors: log lines containing ERROR, CRITICAL or Traceback.",
    )):
        c.text(M + 4, 136 + ch + 22 + i * 28, line, 17, C_MUTED)
    out.append(c.png())
    return out


def render_disk(mounts: list[Mount], usage: "list[UsageRow] | None", partial: bool = False) -> bytes:
    """One image: every filesystem as a gauge, then what takes the space."""
    M, W = 32, IMG_W
    cw = W - 2 * M
    mounts = mounts[:8]
    rows = (usage or [])[:14]
    h1 = 96 + len(mounts) * 92 + 12
    h2 = (104 + len(rows) * 50 + 20) if rows else 0
    c = Canvas(W, 136 + h1 + (16 + h2 if rows else 0) + 60)
    host = _ascii(HOST.hostname) if HOST.hostname else "server"
    _header(c, "Disk usage", f"{host}  ·  {local(time.time()):%a %d %b %H:%M}  ({TZ_NAME})")
    y = 136
    _card(c, M, y, cw, h1, "Filesystems", "used space of every real filesystem")
    for i, m in enumerate(mounts):
        ry = y + 100 + i * 92
        col = C_CRIT if m.pct >= 90 else (C_WARN if m.pct >= 75 else C_CPU)
        c.text(M + 24, ry, c.fit(_ascii(m.path), 23, 520, True), 23, C_TEXT, bold=True)
        label = f"{human(m.used)} of {human(m.total)}  ·  {human(m.free)} free"
        c.text(M + cw - 24, ry + 2, label, 20, C_TEXT2, anchor="ra")
        bx0, bx1, by = M + 24, M + cw - 120, ry + 38
        c.rect(bx0, by, bx1, by + 22, C_EMPTY, r=11)
        c.rect(bx0, by, bx0 + max(22, (bx1 - bx0) * min(100.0, m.pct) / 100), by + 22, col, r=11)
        c.text(M + cw - 24, by + 11, f"{m.pct:.0f}%", 26, C_TEXT, anchor="rm", bold=True)
    y += h1 + 16
    if rows:
        _card(c, M, y, cw, h2, "What takes the space",
              "largest folders the bot looked at" + (" - time limit reached, '>' = at least" if partial else ""))
        top = max([r.size for r in rows] + [1])
        for i, r in enumerate(rows):
            ry = y + 112 + i * 50
            c.text(M + 24, ry, c.fit(_ascii(r.label), 21, 330), 21, C_TEXT, anchor="lm")
            bx0, bx1 = M + 370, M + cw - 170
            c.rect(bx0, ry - 9, bx0 + max(6, (bx1 - bx0) * r.size / top), ry + 9, C_MEM, r=5)
            c.text(M + cw - 24, ry, ("> " if r.partial else "") + human(r.size), 21, C_TEXT,
                   anchor="rm")
        y += h2 + 16
    c.text(M + 4, y + 8, "Read-only measurement. Nothing on the server was changed.", 17, C_MUTED)
    return c.png()


REPORT_LOCK = LazyLock()


async def send_report(bot, chat_id: int, t0: int, t1: int, title: str, title_en: str,
                      quiet: bool = False) -> None:
    """quiet=True for scheduled reports: when there is no data, nothing is sent."""
    if STORE is None:
        if not quiet:
            await bot.send_message(chat_id, t("rp.no_monitor"))
        return
    async with REPORT_LOCK:  # one report is built at a time
        rep = await asyncio.to_thread(build_report, STORE, t0, t1)
        if rep.n < 3:
            if not quiet:
                await bot.send_message(chat_id, t("rp.no_data"))
            return
        text = report_text(rep, title)
        if Image is None:
            await bot.send_message(chat_id, text + "\n\n" + t("rp.no_pillow"))
            return
        try:
            images = await asyncio.to_thread(render_report, rep, title_en)
        except Exception:
            log.exception("chart rendering failed")
            await bot.send_message(chat_id, text + "\n\n" + t("rp.chart_failed"))
            return
        caption = text if visible_len(text) <= 1000 else None
        media = [InputMediaPhoto(img, caption=caption if i == 0 else None,
                                 parse_mode=ParseMode.HTML, filename=f"report_{i + 1}.png")
                 for i, img in enumerate(images)]
        await bot.send_media_group(chat_id, media, read_timeout=120, write_timeout=300,
                                   connect_timeout=30, pool_timeout=60)
        if caption is None:
            await bot.send_message(chat_id, text)


# ════════════════════════════════════════════════════════════════
#  Heavy jobs: one at a time, with a cancel button
# ════════════════════════════════════════════════════════════════


class Cancelled(Exception):
    """The admin cancelled the job."""


class JobError(Exception):
    """An error worth showing to the admin (too big, disk nearly full, …)."""


class SinkFailure(Exception):
    """Sending or writing a part failed; the whole job stops."""


class BigUploadError(Exception):
    """The big-file channel is not usable right now."""


@dataclass
class Job:
    chat_id: int
    title: str
    cancel: threading.Event = field(default_factory=threading.Event)
    prefix: str = ""
    stage: str = ""
    total: int = 0
    done: int = 0
    parts: int = 0
    sent: int = 0
    cur: int = 0          # bytes of the upload in flight
    note: object = None


JOB: Job | None = None


def cancel_kb() -> InlineKeyboardMarkup:
    return kb([[btn(t("b.cancel"), "cx", "danger")]])


def progress_text(job: Job) -> str:
    text = f"⏳ <b>{esc(job.title)}</b>"
    if job.stage:
        text += f"\n{esc(job.prefix + job.stage)}"
    bits = []
    if job.total:
        bits.append(t("job.files", done=job.done, total=job.total))
    if job.sent or job.cur:
        bits.append(t("job.sent", size=human(job.sent + job.cur)))
    if bits:
        text += "\n" + " · ".join(bits)
    return aligned(text)


async def _progress_ticker(job: Job, app: Application) -> None:
    last = progress_text(job)
    tick = 0
    while True:
        await asyncio.sleep(2)
        tick += 1
        if not app.running:  # the bot is shutting down → let the job go
            job.cancel.set()
        if tick % 2:
            continue
        text = progress_text(job)
        if text != last and job.note is not None:
            last = text
            with suppress(Exception):
                await job.note.edit_text(text, reply_markup=cancel_kb())


async def run_job(q, context: ContextTypes.DEFAULT_TYPE, title: str, work) -> None:
    """Run work(job); refuses to start while another job is running."""
    global JOB
    if JOB is not None:
        with suppress(BadRequest):
            await q.answer(t("job.busy"), show_alert=True)
        return
    chat_id = q.message.chat_id if q.message else q.from_user.id
    job = JOB = Job(chat_id=chat_id, title=title)
    ticker = None
    try:
        with suppress(BadRequest):
            await q.answer(t("job.started"))
        job.note = await context.bot.send_message(chat_id, progress_text(job),
                                                  reply_markup=cancel_kb())
        ticker = asyncio.create_task(_progress_ticker(job, context.application))
        await work(job)
    except Exception as e:
        if isinstance(e, Cancelled) or isinstance(e.__cause__, Cancelled) or job.cancel.is_set():
            msg = t("job.cancelled")
        elif isinstance(e, JobError):
            msg = str(e)
        else:
            log.error("job «%s» failed", title, exc_info=e)
            msg = t("job.failed")
        with suppress(Exception):
            await context.bot.send_message(chat_id, msg)
    finally:
        if ticker is not None:
            ticker.cancel()
        clean_workdir()  # no temporary file is ever left behind
        JOB = None
        if job.note is not None:
            with suppress(Exception):
                await job.note.delete()


# ════════════════════════════════════════════════════════════════
#  Sending files (streamed — a file is never loaded into memory)
# ════════════════════════════════════════════════════════════════


class RangeReader:
    """A read-only window on part of a file; uploads straight from disk, no copy."""

    def __init__(self, path: str, offset: int, length: int, name: str = "") -> None:
        self._f = open(path, "rb")
        self._off, self._len, self._pos = offset, length, 0
        self._f.seek(offset)
        self.name = name or os.path.basename(path)

    def read(self, n: int = -1) -> bytes:
        left = self._len - self._pos
        if left <= 0:
            return b""
        data = self._f.read(left if n is None or n < 0 else min(n, left))
        self._pos += len(data)
        return data

    def seek(self, pos: int, whence: int = 0) -> int:
        if whence == 1:
            pos += self._pos
        elif whence == 2:
            pos += self._len
        self._pos = max(0, min(self._len, pos))
        self._f.seek(self._off + self._pos)
        return self._pos

    def tell(self) -> int:
        return self._pos

    def seekable(self) -> bool:
        return True

    def close(self) -> None:
        self._f.close()


def _mt_proxy() -> dict | None:
    """PROXY_URL in the shape Telethon expects (needs the python-socks package)."""
    m = re.match(r"^(socks5h?|socks4|http)://(?:([^:@/]+)(?::([^@/]*))?@)?([^:/@]+):(\d+)/?$",
                 PROXY_URL)
    if not m:
        return None
    scheme, user, password, host, port = m.groups()
    out = {"proxy_type": scheme.rstrip("h"), "addr": host, "port": int(port), "rdns": True}
    if user:
        out["username"], out["password"] = user, password or ""
    return out


class BigUploader:
    """Optional side-channel for files larger than one Bot API upload.

    With API_ID and API_HASH set (and Telethon installed) the same bot logs in over
    MTProto, where one file may be up to 2 GB. It connects only while a big file is
    being sent and disconnects shortly after. Without it, big files simply go out
    as numbered parts — nothing else changes."""

    def __init__(self) -> None:
        self.client = None
        self.lock = LazyLock()
        self.error = ""
        self.retry_at = 0.0
        self.idle: asyncio.Task | None = None
        self._lib: bool | None = None

    def configured(self) -> bool:
        return bool(API_ID and API_HASH)

    def installed(self) -> bool:
        if self._lib is None:
            try:
                self._lib = importlib.util.find_spec("telethon") is not None
            except Exception:
                self._lib = False
        return self._lib

    def ready(self) -> bool:
        return self.configured() and self.installed() and time.time() >= self.retry_at

    def status(self) -> str:
        if not self.configured():
            return "off"
        if not self.installed():
            return "missing"
        return "paused" if time.time() < self.retry_at else "on"

    async def _connect(self):
        if self.client is None:
            from telethon import TelegramClient
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            self.client = TelegramClient(
                str(DATA_DIR / "mtproto"), API_ID, API_HASH, receive_updates=False,
                flood_sleep_threshold=120, connection_retries=3, request_retries=4,
                proxy=_mt_proxy())
        if not self.client.is_connected():
            await self.client.connect()
        if not await self.client.is_user_authorized():
            await self.client.sign_in(bot_token=BOT_TOKEN)
        return self.client

    async def _close_later(self, delay: float = 90.0) -> None:
        await asyncio.sleep(delay)
        await self.close()

    async def close(self) -> None:
        async with self.lock:
            if self.client is not None:
                with suppress(Exception):
                    await self.client.disconnect()

    def _fail(self, e: BaseException) -> str:
        wait = getattr(e, "seconds", None)
        self.retry_at = time.time() + (float(wait) + 5 if isinstance(wait, (int, float)) else 600)
        self.error = f"{type(e).__name__}: {e}"[:200]
        log.warning("big-file channel unavailable (%s) — falling back to parts", self.error)
        return self.error

    async def send(self, chat_id: int, source, name: str, caption: str | None, size: int,
                   job: Job | None = None) -> None:
        """Upload one file (a path or a file-like object) of up to 2 GB."""
        if not self.ready():
            raise BigUploadError(self.error or "big-file mode is not configured")
        async with self.lock:
            if self.idle is not None:
                self.idle.cancel()
                self.idle = None
            try:
                try:
                    client = await asyncio.wait_for(self._connect(), 90)
                except Exception as e:
                    raise BigUploadError(self._fail(e)) from e

                def progress(sent, total) -> None:
                    if job is not None:
                        job.cur = int(sent)

                async def work() -> None:
                    handle = await client.upload_file(source, file_size=size, file_name=name,
                                                      progress_callback=progress)
                    await client.send_file(chat_id, handle, caption=caption,
                                           force_document=True, parse_mode="html")

                task = asyncio.ensure_future(work())
                try:
                    while not task.done():
                        await asyncio.wait({task}, timeout=1.0)
                        if job is not None and job.cancel.is_set() and not task.done():
                            raise Cancelled
                    try:
                        task.result()
                    except asyncio.CancelledError:
                        raise Cancelled from None
                    except Exception as e:
                        raise BigUploadError(self._fail(e)) from e
                    self.error = ""
                finally:
                    if not task.done():  # cancelled by the admin or by shutdown
                        task.cancel()
                        with suppress(BaseException):
                            await task
                    if job is not None:
                        job.cur = 0
            finally:
                self.idle = spawn(self._close_later())


BIG = BigUploader()
STREAM_UPLOAD = True  # switched off automatically if streamed uploads do not work here


def one_file_cap() -> int:
    """The largest single file we can send right now."""
    return MT_MAX_BYTES if BIG.ready() else TG_PART_SIZE


def part_cap() -> int:
    """Size of the pieces when something has to be split."""
    return MT_PART_SIZE if BIG.ready() else TG_PART_SIZE


async def send_doc(bot, chat_id: int, path: str, filename: str, caption: str | None = None,
                   offset: int = 0, length: int | None = None, job: Job | None = None) -> None:
    """Send one file (or a slice of it). Small → Bot API, streamed with retries;
    large → the big-file channel. Raises BigUploadError when that channel fails."""
    global STREAM_UPLOAD
    size = length if length is not None else os.path.getsize(path) - offset
    if size > TG_PART_SIZE:
        whole = offset == 0 and size == os.path.getsize(path)
        source = path if whole else RangeReader(path, offset, size, filename)
        try:
            await BIG.send(chat_id, source, filename, caption, size, job)
        finally:
            if not whole:
                source.close()
        return
    for attempt in range(1, 5):
        fh = RangeReader(path, offset, size)
        # the first two attempts stream; after that the piece (≤ one upload) goes from memory
        stream = STREAM_UPLOAD and attempt <= 2
        try:
            doc = None
            if stream:
                try:
                    doc = InputFile(fh, filename=filename, read_file_handle=False)
                except TypeError:  # an older library without this option
                    STREAM_UPLOAD = stream = False
            if doc is None:
                doc = InputFile(await asyncio.to_thread(fh.read), filename=filename)
            await bot.send_document(chat_id, doc, caption=caption, read_timeout=300,
                                    write_timeout=900, connect_timeout=30, pool_timeout=60)
            return
        except RetryAfter as e:
            wait = e.retry_after
            wait = wait.total_seconds() if hasattr(wait, "total_seconds") else float(wait)
            await asyncio.sleep(min(120.0, wait + 1))
        except BadRequest:
            raise
        except (TimedOut, NetworkError) as e:
            if attempt == 4:
                raise
            log.warning("send %s failed (%s) — retry %d", filename, type(e).__name__, attempt)
            await asyncio.sleep(3 * attempt)
        except TelegramError:
            raise
        except Exception as e:
            if not stream:
                raise
            STREAM_UPLOAD = False  # this environment cannot stream uploads
            log.warning("streaming upload not usable (%s: %s) — sending from memory",
                        type(e).__name__, e)
        finally:
            fh.close()
    raise NetworkError(f"sending {filename} failed after retries")


async def send_pieces(bot, chat_id: int, path: str, name: str, step: int,
                      job: Job | None = None, tail: str = "") -> int:
    """Send a file as name.001, name.002, … straight from disk. Returns the part count."""
    size = os.path.getsize(path)
    parts = math.ceil(size / step)
    for i in range(parts):
        if job is not None and job.cancel.is_set():
            raise Cancelled
        length = min(step, size - i * step)
        cap = t("send.part", name=esc(name), i=i + 1, n=parts, size=human(size))
        if i == 0:
            cap += t("send.join")
        if i == parts - 1 and tail:
            cap += "\n\n" + tail
        await send_doc(bot, chat_id, path, f"{name}.{i + 1:03d}", cap,
                       offset=i * step, length=length, job=job)
        if job is not None:
            job.sent += length
        await asyncio.sleep(SEND_PAUSE)
    return parts


async def send_file(bot, chat_id: int, path: str, job: Job | None = None,
                    caption: str | None = None) -> None:
    """Send a file straight from disk; a file that is too large for one message is
    split without making a temporary copy."""
    size = os.path.getsize(path)
    name = os.path.basename(path)
    if size == 0:
        await bot.send_message(chat_id, t("send.empty", name=esc(name)))
        return
    if size > MAX_JOB_BYTES:
        raise JobError(t("send.too_big", name=esc(name), size=human(size),
                         cap=human(MAX_JOB_BYTES)))
    if size <= one_file_cap():
        try:
            await send_doc(bot, chat_id, path, name, caption, job=job)
            if job is not None:
                job.sent += size
            return
        except BigUploadError as e:
            await bot.send_message(chat_id, t("send.big_fallback", why=esc(e)))
    try:
        await send_pieces(bot, chat_id, path, name, part_cap(), job)
    except BigUploadError as e:
        raise JobError(t("send.big_failed", why=esc(e))) from e


# ════════════════════════════════════════════════════════════════
#  Building zips (reading only; the output is streamed in parts)
# ════════════════════════════════════════════════════════════════

Pairs = list  # of (source path, name inside the zip)


def pairs_tree(root: str, prefix: str, flat: bool = False) -> Pairs:
    """Every file below `root` worth backing up. Skips virtualenvs, caches, VCS data,
    symlinks and this bot's own temporary folder."""
    out: Pairs = []
    if flat:
        return [(os.path.join(root, n), os.path.join(prefix, n))
                for n in loose_files(root, 2000) if not skip_file(os.path.join(root, n), n)]
    own_tmp = str(WORK_DIR)
    for dp, dns, fns in os.walk(root, followlinks=False):
        keep = []
        for d in sorted(dns):
            full = os.path.join(dp, d)
            if (d in SKIP_DIRS or os.path.islink(full) or full == own_tmp
                    or os.path.isfile(os.path.join(full, "pyvenv.cfg"))):
                continue
            keep.append(d)
        dns[:] = keep
        for f in sorted(fns):
            fp = os.path.join(dp, f)
            if skip_file(fp, f) or os.path.islink(fp) or not os.path.isfile(fp):
                continue
            out.append((fp, os.path.join(prefix, os.path.relpath(fp, root))))
    return out


def pairs_files(p: Project, rels: list[str], prefix: str) -> Pairs:
    return [
        (os.path.join(p.root, r), os.path.join(prefix, r))
        for r in rels
        if allowed(p, os.path.join(p.root, r))
    ]


def uniq_pairs(pairs: Pairs) -> Pairs:
    seen: set[str] = set()
    out: Pairs = []
    for src, arc in pairs:
        if arc not in seen:
            seen.add(arc)
            out.append((src, arc))
    return out


def measure(pairs: Pairs) -> tuple[int, int]:
    """(total source size, largest SQLite database) — for the disk check before starting."""
    total = big_db = 0
    for src, _ in pairs:
        try:
            sz = os.path.getsize(src)
        except OSError:
            continue
        total += sz
        if src.lower().endswith(SQLITE_SUFFIXES):
            big_db = max(big_db, sz)
    return total, big_db


class PartSink:
    """Writes the zip stream in parts of at most part_size. Each part is sent and
    deleted as soon as it is full, so only one part is ever on disk."""

    def __init__(self, workdir: str, part_size: int, on_part, cancel: threading.Event) -> None:
        self.dir, self.size, self.on_part, self.cancel = workdir, part_size, on_part, cancel
        self.idx = 0
        self.cur = 0
        self.total = 0
        self.fh = None
        self.path = ""
        self.failed = False

    def _open(self) -> None:
        self.idx += 1
        self.path = os.path.join(self.dir, f"part{self.idx:03d}")
        self.fh = open(self.path, "wb")
        self.cur = 0

    def _ship(self, final: bool) -> None:
        self.fh.close()
        self.fh = None
        try:
            self.on_part(self.path, self.idx, final)
        finally:
            with suppress(OSError):
                os.remove(self.path)

    def write(self, data) -> int:
        if self.failed:
            raise SinkFailure("sink already failed")
        mv = memoryview(data)
        n = len(mv)
        try:
            if self.cancel.is_set():
                raise Cancelled
            while len(mv):
                if self.fh is None:
                    self._open()
                elif self.cur >= self.size:
                    self._ship(False)
                    self._open()
                k = min(len(mv), self.size - self.cur)
                self.fh.write(mv[:k])
                self.cur += k
                self.total += k
                mv = mv[k:]
        except Exception as e:
            self.failed = True
            raise SinkFailure(str(e) or type(e).__name__) from e
        return n

    def flush(self) -> None:
        if self.fh is not None:
            self.fh.flush()

    def close(self) -> None:
        if self.fh is not None and not self.failed:
            try:
                self._ship(True)
            except Exception as e:
                self.failed = True
                raise SinkFailure(str(e) or type(e).__name__) from e

    def abort(self) -> None:
        self.failed = True
        if self.fh is not None:
            with suppress(OSError):
                self.fh.close()
            self.fh = None
        if self.path:
            with suppress(OSError):
                os.remove(self.path)


_SQLITE_MAGIC = b"SQLite format 3\x00"


def _sqlite_kind(path: str) -> str:
    """"" (not a SQLite database), "wal" or "journal" — read from the file's own header,
    so the name of the file does not matter."""
    try:
        with open(path, "rb") as f:
            head = f.read(20)
    except OSError:
        return ""
    if len(head) < 20 or head[:16] != _SQLITE_MAGIC:
        return ""
    return "wal" if 2 in (head[18], head[19]) else "journal"


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _snapshot(src: str, snap: str, kind: str) -> None:
    """One consistent copy of a database, made by SQLite's own backup API. The source is
    opened read-only and with a read-only shared-memory file: reading it never creates a
    -shm file. (The caller has checked that a WAL database already has its -wal and -shm,
    which is the one case where SQLite would otherwise create them.)"""
    uri = Path(src).as_uri() + "?mode=ro&readonly_shm=1"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as s, \
            closing(sqlite3.connect(snap)) as d:
        if kind == "wal" or _size(src) <= SNAP_ONE_GO:
            s.backup(d)     # a reader never blocks a WAL writer; a small file takes a moment
            return
        # A large database in rollback-journal mode: while it is being read, the program it
        # belongs to cannot commit. So it is copied in short steps and the program writes
        # in between (SQLite starts the copy over when that happens — within a time limit).
        deadline = time.monotonic() + SNAP_SECONDS

        def progress(_status: int, remaining: int, _total: int) -> None:
            if remaining and time.monotonic() > deadline:
                raise sqlite3.OperationalError("written too often for a snapshot")

        s.backup(d, pages=2048, progress=progress, sleep=0.02)


def _add_file(zf: zipfile.ZipFile, src: str, arc: str, tmpdir: str) -> bool:
    """Add one file to the zip. A SQLite database goes in as one consistent snapshot.
    Returns False when that was not possible and the database went in as it was found,
    with its -wal / -journal file beside it (SQLite completes the job when it is opened)."""
    comp = zipfile.ZIP_STORED if arc.lower().endswith(STORE_SUFFIXES) else zipfile.ZIP_DEFLATED
    kind = _sqlite_kind(src)
    if not kind:
        zf.write(src, arc, compress_type=comp)
        return True
    if kind == "wal" and not (os.path.exists(src + "-shm") and os.path.exists(src + "-wal")):
        # No program has it open. To read it, SQLite would create -wal and -shm files next
        # to it — and this bot does not create files in other people's folders. Without a
        # write-ahead log the file itself is the whole truth; a log left behind by a crash
        # goes in beside it.
        if not _size(src + "-wal"):
            zf.write(src, arc, compress_type=comp)
            return True
        why = "a write-ahead log, but no running program"
    else:
        snap = os.path.join(tmpdir, f"snap_{uuid.uuid4().hex}.db")
        try:
            if shutil.disk_usage(tmpdir).free < _size(src) + MIN_FREE_DISK:
                raise sqlite3.OperationalError("not enough free disk for a snapshot")
            _snapshot(src, snap, kind)
            zf.write(snap, arc, compress_type=comp)
            return True
        except sqlite3.Error as e:
            why = str(e)
        finally:
            for suffix in ("",) + SIDE_SUFFIXES:
                with suppress(OSError):
                    os.remove(snap + suffix)
    log.warning("no consistent snapshot of %s (%s) — copied as found", src, why)
    zf.write(src, arc, compress_type=comp)
    for suffix in ("-wal", "-journal"):
        if _size(src + suffix):
            with suppress(OSError):
                zf.write(src + suffix, arc + suffix, compress_type=comp)
    return False


def write_zip_stream(pairs: Pairs, workdir: str, part_size: int, on_part, job: Job,
                     stats: dict) -> None:
    sink = PartSink(workdir, part_size, on_part, job.cancel)
    try:
        with zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED, compresslevel=6,
                             strict_timestamps=False) as zf:
            for src, arc in pairs:
                if job.cancel.is_set():
                    raise Cancelled
                try:
                    if not _add_file(zf, src, arc, workdir):
                        stats["as_found"] = stats.get("as_found", 0) + 1
                    stats["added"] += 1
                except (OSError, ValueError, sqlite3.Error) as e:
                    stats["skipped"] += 1
                    log.warning("skip %s: %s", src, e)
                job.done += 1
        stats["size"] = sink.total
        sink.close()
        stats["parts"] = sink.idx
    except BaseException:
        sink.abort()
        raise


async def zip_and_send(job: Job, bot, chat_id: int, *, title: str, label: str, slug: str,
                       pairs: Pairs) -> None:
    """Build the zip and send it while it is being built.
    Temporary space: at most one part plus one database snapshot."""
    pairs = uniq_pairs(pairs)
    if not pairs:
        raise JobError(t("zip.nothing", title=esc(title)))
    if not os.access(WORK_DIR, os.W_OK | os.X_OK):
        raise JobError(t("zip.no_workdir", path=esc(WORK_DIR)))
    est, big_db = await asyncio.to_thread(measure, pairs)
    if est > MAX_JOB_BYTES:
        raise JobError(t("zip.too_big", title=esc(title), size=human(est),
                         cap=human(MAX_JOB_BYTES)))
    part = part_cap()
    room = free_bytes() - MIN_FREE_DISK - big_db
    if part > TG_PART_SIZE:  # big-file mode: never use more temporary space than is free
        part = max(TG_PART_SIZE, min(part, room))
    if room < min(est, part):
        raise JobError(t("zip.no_space", free=human(free_bytes())))
    job.stage, job.total, job.done = f"{title} · {label}", len(pairs), 0
    loop = asyncio.get_running_loop()
    lang = LANG.get()
    made = datetime.now(TZ)
    when = made.strftime("%Y-%m-%d_%H-%M")
    name = f"{slug}_{when}.zip"
    stats = {"added": 0, "skipped": 0, "size": 0, "parts": 0, "sliced": 0}
    t_start = time.time()

    def summary() -> str:
        text = t("zip.summary", title=esc(title), label=esc(label), n=stats["added"],
                 size=human(stats["size"]), secs=f"{time.time() - t_start:.1f}",
                 when=made.strftime("%Y-%m-%d %H:%M"))
        if stats["skipped"]:
            text += "\n" + t("zip.skipped", n=stats["skipped"])
        if stats.get("as_found"):
            text += "\n" + t("zip.as_found", n=stats["as_found"])
        return text

    async def ship(path: str, idx: int, final: bool) -> None:
        LANG.set(lang)
        size = os.path.getsize(path)
        single = final and idx == 1
        try:
            if single:
                await send_doc(bot, chat_id, path, name, summary(), job=job)
            else:
                cap = t("zip.part", name=esc(name), i=idx) + (t("zip.last") if final else "")
                if idx == 1:
                    cap += t("send.join")
                await send_doc(bot, chat_id, path, f"{name}.{idx:03d}", cap, job=job)
                await asyncio.sleep(SEND_PAUSE)
        except BigUploadError as e:
            if not single:
                raise
            # the big-file channel failed: the finished zip goes out as ordinary parts
            await bot.send_message(chat_id, t("send.big_fallback", why=esc(e)))
            stats["sliced"] = await send_pieces(bot, chat_id, path, name, TG_PART_SIZE, job)
            job.parts = idx
            return
        job.parts = idx
        job.sent += size

    def on_part(path: str, idx: int, final: bool) -> None:  # runs in the zip thread
        if job.cancel.is_set():
            raise Cancelled
        wait = max(PART_SEND_TIMEOUT, os.path.getsize(path) // (64 * 1024))
        asyncio.run_coroutine_threadsafe(ship(path, idx, final), loop).result(wait)

    await asyncio.to_thread(write_zip_stream, pairs, str(WORK_DIR), part, on_part, job, stats)
    parts = stats["sliced"] or stats["parts"]
    if parts > 1:
        await bot.send_message(chat_id, summary() + "\n" + t("zip.parts", n=parts))


async def send_many(job: Job, bot, chat_id: int, items: list[tuple[str, object]]) -> None:
    """Several things in a row: ("file", path) or ("zip", arguments of zip_and_send)."""
    ok = 0
    for i, (kind, arg) in enumerate(items, 1):
        if job.cancel.is_set():
            raise Cancelled
        job.prefix = f"[{i}/{len(items)}] " if len(items) > 1 else ""
        try:
            if kind == "file":
                job.stage, job.total, job.done = os.path.basename(arg), 0, 0
                await send_file(bot, chat_id, arg, job)
            else:
                await zip_and_send(job, bot, chat_id, **arg)
            ok += 1
        except JobError as e:  # this one failed; the rest carry on
            await bot.send_message(chat_id, str(e))
        except OSError as e:
            await bot.send_message(chat_id, f"⚠️ <code>{esc(e)}</code>")
        await asyncio.sleep(SEND_PAUSE)
    job.prefix = ""
    if len(items) > 1:
        await bot.send_message(chat_id, t("send.many_done", ok=ok, n=len(items)))


# ════════════════════════════════════════════════════════════════
#  Essentials: the files that matter when a project has to be rebuilt
# ════════════════════════════════════════════════════════════════

_ESS_CACHE: dict[str, tuple[float, list[str]]] = {}


def is_essential_name(name: str) -> bool:
    low = name.lower()
    if low.endswith((".log", ".min.js", ".min.css", ".map", ".pyc")):
        return False
    return (low.endswith(ESSENTIAL_SUFFIXES) or name in ESSENTIAL_NAMES
            or low.startswith((".env", "docker-compose", "compose.")))


def essentials(p: Project, fresh: bool = False) -> list[str]:
    """Relative paths: source, configuration and small data files of a project
    (up to ESSENTIAL_DEPTH levels deep), plus anything the admin pinned."""
    hit = _ESS_CACHE.get(p.root)
    if hit and not fresh and time.time() - hit[0] < 30:
        return hit[1]
    found: list[tuple[int, str]] = []
    # configuration folders: every small file counts (site files have no extension),
    # except private keys — those only leave the server in a deliberate "whole" backup
    everything = p.kind == "cfg"
    if p.flat:
        for n in loose_files(p.root, 600):
            with suppress(OSError):
                if is_essential_name(n) and os.path.getsize(os.path.join(p.root, n)) <= ESSENTIAL_MAX_FILE:
                    found.append((0, n))
    else:
        stack = [(p.root, 0)]
        listings = 0
        while stack and listings < 500 and len(found) < ESSENTIAL_MAX_COUNT * 3:
            d, depth = stack.pop()
            listings += 1
            try:
                entries = sorted(os.scandir(d), key=lambda e: e.name)
            except OSError:
                continue
            for e in entries:
                try:
                    if e.is_symlink():
                        continue
                    if e.is_dir(follow_symlinks=False):
                        if (depth + 1 < ESSENTIAL_DEPTH + everything
                                and e.name not in ESSENTIAL_SKIP_DIRS
                                and not e.name.startswith(".")
                                and not os.path.isfile(os.path.join(e.path, "pyvenv.cfg"))):
                            stack.append((e.path, depth + 1))
                    elif not e.is_file(follow_symlinks=False):
                        continue
                    elif everything:
                        low = e.name.lower()
                        if (not low.endswith(".key") and "privkey" not in low
                                and e.stat(follow_symlinks=False).st_size <= 2 * MIB):
                            found.append((depth, os.path.relpath(e.path, p.root)))
                    elif (is_essential_name(e.name)
                          and e.stat(follow_symlinks=False).st_size <= ESSENTIAL_MAX_FILE):
                        found.append((depth, os.path.relpath(e.path, p.root)))
                except OSError:
                    continue
    found.sort(key=lambda x: (x[0], x[1].lower()))
    rels = [r for _, r in found[:ESSENTIAL_MAX_COUNT]]
    pins = STORE.pins(p.root) if STORE is not None else []
    out = [r for r in pins if os.path.isfile(os.path.join(p.root, r))]
    out += [r for r in rels if r not in out]
    _ESS_CACHE[p.root] = (time.time(), out)
    if len(_ESS_CACHE) > 200:
        _ESS_CACHE.pop(next(iter(_ESS_CACHE)))
    return out


# ════════════════════════════════════════════════════════════════
#  Search: file names (typo-tolerant) and file contents
# ════════════════════════════════════════════════════════════════

_WORD_RE = re.compile(r"[a-z0-9؀-ۿ]+")
_GREP_SKIP = STORE_SUFFIXES + SQLITE_SUFFIXES + (
    ".pyc", ".so", ".o", ".a", ".bin", ".exe", ".dll", ".woff", ".woff2", ".ttf", ".otf",
    ".ico", ".session", ".tar", ".iso", ".img", ".wav", ".avi", ".psd", ".svg", ".lock")


@dataclass
class Hit:
    score: int
    pkey: str
    path: str
    is_dir: bool
    size: int = 0
    line: int = 0          # content search: line number
    text: str = ""         # content search: the matching line


def _norm(s: str) -> str:
    return s.lower().replace("ي", "ی").replace("ك", "ک").replace("‌", "")


def _osa(a: str, b: str, k: int) -> int:
    """Edit distance (insert, delete, replace, swap of neighbours); gives up above k."""
    la, lb = len(a), len(b)
    if abs(la - lb) > k:
        return k + 1
    prev2: list[int] | None = None
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        ai = a[i - 1]
        for j in range(1, lb + 1):
            v = prev[j - 1] + (ai != b[j - 1])
            if prev[j] + 1 < v:
                v = prev[j] + 1
            if cur[j - 1] + 1 < v:
                v = cur[j - 1] + 1
            if prev2 is not None and j > 1 and ai == b[j - 2] and a[i - 2] == b[j - 1]:
                if prev2[j - 2] + 1 < v:
                    v = prev2[j - 2] + 1
            cur[j] = v
        if min(cur) > k:
            return k + 1
        prev2, prev = prev, cur
    return prev[lb]


def _slack(n: int) -> int:
    """How many typos a word of this length may contain."""
    return 0 if n <= 3 else (1 if n <= 5 else (2 if n <= 9 else 3))


def _fuzzy_word(tok: str, name: str) -> int:
    """How well one query word matches one (normalised) file name: 0–70."""
    if tok in name:
        return 70
    k = _slack(len(tok))
    best = 0
    if k:
        chars = set(tok)
        words = _WORD_RE.findall(name)
        for i, w in enumerate(words):
            if len(w) + k < len(tok) or len(chars - set(w)) > k:
                continue
            head = w if len(w) <= len(tok) + k else w[:len(tok)]   # also "confg" ~ "config…"
            d = _osa(tok, head, k)
            if d <= k:
                # the first word of a name counts a little more than its extension:
                # "confg" should offer config.json before nginx.conf
                place = 2 if i == 0 else (-2 if i == len(words) - 1 and "." in name else 0)
                best = max(best, 62 - 8 * d + place)
    if not best and len(tok) >= 4:  # letters in order, close together: "rqmts" ~ requirements
        pos = -1
        first = -1
        for ch in tok:
            pos = name.find(ch, pos + 1)
            if pos < 0:
                break
            if first < 0:
                first = pos
        else:
            if pos - first + 1 <= len(tok) * 2.5:
                best = 44
    return best


def _walk(projects: tuple[Project, ...], deadline: float, stop: threading.Event | None,
          max_entries: int):
    """Yield (project, entry) for every file and folder; honours the time limit."""
    seen = 0
    for p in projects:
        if p.flat:
            for n in loose_files(p.root, 2000):
                yield p, None, os.path.join(p.root, n), n, False
            continue
        stack = [p.root]
        while stack:
            d = stack.pop()
            try:
                it = os.scandir(d)
            except OSError:
                continue
            with it:
                for e in it:
                    seen += 1
                    if seen % 1024 == 0 and (time.monotonic() > deadline or seen > max_entries
                                             or (stop is not None and stop.is_set())):
                        yield None, None, "", "", False   # signal: cut short
                        return
                    try:
                        if e.is_symlink():
                            continue
                        is_dir = e.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    if is_dir:
                        if e.name in SKIP_DIRS:
                            continue
                        stack.append(e.path)
                    elif skip_file(e.path, e.name):
                        continue
                    yield p, e, e.path, e.name, is_dir


def search_names(query: str, projects: tuple[Project, ...],
                 stop: threading.Event | None = None) -> dict:
    """Find files and folders by name. Exact and partial matches first; when there are
    few of them, near-misses (typos, missing letters) are added."""
    started = time.monotonic()
    qn = _norm(query.strip())
    tokens = [x for x in re.split(r"[\s/\\]+", qn) if x][:6]
    result = {"hits": [], "scanned": 0, "cut": False, "fuzzy": False, "secs": 0.0}
    if not tokens:
        return result
    strong: list[Hit] = []
    pool: dict[str, list] = {}
    for p, e, path, name, is_dir in _walk(projects, started + SEARCH_SECONDS, stop,
                                          SEARCH_MAX_ENTRIES):
        if p is None:
            result["cut"] = True
            break
        result["scanned"] += 1
        nn = _norm(name)
        score = 0
        if nn == qn:
            score = 100
        elif nn.rsplit(".", 1)[0] == qn:
            score = 96
        elif nn.startswith(qn):
            score = 90
        elif qn in nn:
            score = 82
        elif len(tokens) > 1:
            if all(tok in nn for tok in tokens):
                score = 76
            elif any(tok in nn for tok in tokens):
                rel = _norm(os.path.join(os.path.basename(p.root), os.path.relpath(path, p.root)))
                if all(tok in rel for tok in tokens):
                    score = 64
        if score:
            if len(strong) < 2000:
                strong.append(Hit(score, p.key, path, is_dir))
        elif len(pool) < 150_000:
            slot = pool.setdefault(nn, [])
            if len(slot) < 3:
                slot.append((p.key, path, is_dir))
    hits = strong
    if len(strong) < 6:  # few direct matches → look for near-misses among the names seen
        result["fuzzy"] = True
        for nn, places in pool.items():
            score = min(_fuzzy_word(tok, nn) for tok in tokens)
            if score >= 40:
                hits.extend(Hit(score, k, path, is_dir) for k, path, is_dir in places)
            if time.monotonic() > started + SEARCH_SECONDS * 1.5:
                result["cut"] = True
                break
    hits.sort(key=lambda h: (-h.score, h.path.count(os.sep), len(h.path)))
    hits = hits[:SEARCH_MAX_HITS]
    for h in hits:
        if not h.is_dir:
            with suppress(OSError):
                h.size = os.path.getsize(h.path)
    result["hits"] = hits
    result["secs"] = time.monotonic() - started
    return result


def search_content(query: str, projects: tuple[Project, ...],
                   stop: threading.Event | None = None) -> dict:
    """Find text files that contain the query (case-insensitive). Bounded by time,
    file count and bytes read; binary and large files are skipped."""
    started = time.monotonic()
    needle = query.strip().lower().encode("utf-8", "replace")
    result = {"hits": [], "scanned": 0, "cut": False, "secs": 0.0}
    if len(needle) < 2:
        return result
    hits: list[Hit] = []
    read = 0
    for p, e, path, name, is_dir in _walk(projects, started + GREP_SECONDS, stop,
                                          SEARCH_MAX_ENTRIES):
        if p is None:
            result["cut"] = True
            break
        if is_dir or name.lower().endswith(_GREP_SKIP):
            continue
        try:
            size = os.path.getsize(path)
            if size == 0 or size > GREP_MAX_FILE:
                continue
            with open(path, "rb") as f:
                data = f.read(GREP_MAX_FILE)
        except OSError:
            continue
        result["scanned"] += 1
        read += len(data)
        if b"\0" not in data[:2048]:
            low = data.lower()
            pos = low.find(needle)
            if pos >= 0:
                start = data.rfind(b"\n", 0, pos) + 1
                end = data.find(b"\n", pos)
                line = data[start:end if end >= 0 else len(data)]
                text = line.decode("utf-8", "replace").strip()
                if name.startswith(".env"):  # never echo the values of an .env file
                    text = re.sub(r"=.*", "=••••••", text)
                hits.append(Hit(low.count(needle), p.key, path, False, size,
                                data.count(b"\n", 0, pos) + 1, text[:160]))
                if len(hits) >= SEARCH_MAX_HITS:
                    result["cut"] = True
                    break
        if result["scanned"] >= GREP_MAX_FILES or read >= GREP_MAX_BYTES:
            result["cut"] = True
            break
    hits.sort(key=lambda h: (-h.score, len(h.path)))
    result["hits"] = hits
    result["secs"] = time.monotonic() - started
    return result


# ════════════════════════════════════════════════════════════════
#  Disk: what takes the space  (measured on demand, with a time limit)
# ════════════════════════════════════════════════════════════════

USAGE_PLACES = (
    ("/var/log", "logs (/var/log)"), ("/var/lib/docker", "docker data"),
    ("/var/lib/containers", "podman data"), ("/var/lib/mysql", "mysql data"),
    ("/var/lib/postgresql", "postgresql data"), ("/var/lib/mongodb", "mongodb data"),
    ("/var/lib/redis", "redis data"), ("/var/cache", "package cache (/var/cache)"),
    ("/var/lib/snapd", "snap data"), ("/tmp", "/tmp"), ("/var/backups", "/var/backups"),
)


@dataclass
class UsageRow:
    label: str
    path: str
    size: int
    partial: bool = False


@dataclass
class Usage:
    rows: list
    files: list           # (size, path) of the largest single files in projects
    partial: bool
    at: float


_USAGE: Usage | None = None
USAGE_LOCK = LazyLock()


def _du(root: str, deadline: float, big: list, want_files: bool) -> tuple[int, bool]:
    """Allocated bytes below a folder, staying on its filesystem."""
    total, partial = 0, False
    try:
        dev = os.stat(root).st_dev
    except OSError:
        return 0, False
    stack = [root]
    n = 0
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                n += 1
                if n % 2048 == 0 and time.monotonic() > deadline:
                    return total, True
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                total += st.st_blocks * 512
                if e.is_dir(follow_symlinks=False):
                    if st.st_dev == dev:
                        stack.append(e.path)
                elif want_files and st.st_size >= 20 * MIB:
                    if len(big) < 8:
                        heapq.heappush(big, (st.st_size, e.path))
                    elif st.st_size > big[0][0]:
                        heapq.heapreplace(big, (st.st_size, e.path))
    return total, partial


def measure_usage(projects: tuple[Project, ...]) -> Usage:
    targets: list[tuple[str, str, bool]] = []
    for p in projects:
        if p.kind in ("cfg", "flat"):
            continue
        targets.append((p.title, p.root, True))
    roots = {r for _, r, _ in targets}
    for path, label in USAGE_PLACES:
        if os.path.isdir(path) and path not in roots:
            targets.append((label, path, False))
    rows: list[UsageRow] = []
    big: list = []
    end = time.monotonic() + USAGE_SECONDS
    for i, (label, path, is_project) in enumerate(targets):
        left = end - time.monotonic()
        if left <= 0.05:
            rows.append(UsageRow(label, path, 0, True))
            continue
        share = max(0.4, left / (len(targets) - i))
        size, partial = _du(path, time.monotonic() + share, big, is_project)
        rows.append(UsageRow(label, path, size, partial))
    rows = [r for r in rows if r.size > 0 or r.partial]
    rows.sort(key=lambda r: -r.size)
    return Usage(rows, sorted(big, reverse=True), any(r.partial for r in rows), time.time())


# ════════════════════════════════════════════════════════════════
#  Processes: who uses the CPU and the memory right now
# ════════════════════════════════════════════════════════════════


def proc_title(pr: Proc) -> str:
    """`python3 bot.py` — the command, shortened to its meaningful words."""
    words = []
    for a in pr.cmd[:4]:
        if a.startswith("-") and words:
            continue
        words.append(os.path.basename(a) if "/" in a else a)
        if len(words) == 2:
            break
    return short(" ".join(words) or pr.comm, 34)


def proc_owner(pr: Proc) -> str:
    """Which service or project a process belongs to, when that can be told."""
    if pr.boxed:
        return "docker"
    if pr.unit and not _HOST_UNITS.match(pr.unit):
        return pr.unit
    paths = [pr.cwd] + [a for a in pr.cmd[:5] if a.startswith("/")]
    for p in PROJECTS:
        if not p.flat and any(x == p.root or x.startswith(p.root + "/") for x in paths if x):
            return os.path.basename(p.root)
    return ""


async def top_procs(n: int = 6) -> tuple[list, list, int]:
    """(top by CPU over one second, top by memory, process count)."""
    a = await asyncio.to_thread(scan_procs)
    t0 = time.monotonic()
    await asyncio.sleep(1.0)
    b = await asyncio.to_thread(scan_procs)
    dt = max(0.2, time.monotonic() - t0)
    before = {(p.pid, p.start): p.ticks for p in a}
    cpu = []
    for p in b:
        was = before.get((p.pid, p.start))
        if was is not None and p.ticks >= was:
            share = (p.ticks - was) / CLK_TCK / dt * 100
            if share >= 0.5:
                cpu.append((share, p))
    cpu.sort(key=lambda x: -x[0])
    mem = sorted(b, key=lambda p: -p.rss)[:n]
    return cpu[:n], mem, len(b)


# ════════════════════════════════════════════════════════════════
#  Screens: home, files, basket, backup
# ════════════════════════════════════════════════════════════════

View = tuple  # (text, InlineKeyboardMarkup)


def home_row() -> list[InlineKeyboardButton]:
    return [btn(t("b.home"), "m")]


def basket_of(ud: dict) -> "OrderedDict[str, str]":
    """Each admin's basket: path → project key (in memory only)."""
    return ud.setdefault("basket", OrderedDict())


def menu_projects() -> list[Project]:
    return [p for p in PROJECTS if p.kind not in ("cfg", "extra")]


def group_projects(group: str) -> list[Project]:
    return [p for p in PROJECTS if p.kind == group]


def states_now() -> dict[str, UnitState]:
    """The most recent states the monitor has (no new reading)."""
    return MON.view if MON is not None else {}


async def fresh_states(max_age: float = 75.0) -> dict[str, UnitState]:
    """States that are at most a minute old (the monitor's own sample, usually)."""
    return await MON.snapshot(max_age) if MON is not None else {}


def pager(prefix: str, page: int, pages: int) -> list[InlineKeyboardButton]:
    if pages <= 1:
        return []
    row = []
    if page > 0:
        row.append(btn("◀️", f"{prefix}:{page - 1}"))
    row.append(btn(f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        row.append(btn("▶️", f"{prefix}:{page + 1}"))
    return row


def home_view(ud: dict) -> View:
    n = len(basket_of(ud))
    lines = [f"🧰 <b>{APP_NAME}</b> · <code>{esc(HOST.hostname or 'server')}</code>",
             f"<i>{t('home.sub')}</i>"]
    facts = []
    states = states_now()
    if SERVICES and states:
        up = sum(1 for s in SERVICES if state_icon(s.sid, states.get(s.sid)) == "🟢")
        bad = sum(1 for s in SERVICES if state_icon(s.sid, states.get(s.sid)) in ("🔴", "🟠"))
        facts.append(t("home.svc", up=up, n=len(SERVICES))
                     + (" · " + t("home.bad", n=bad) if bad else ""))
    last = MON.last if MON is not None else {}
    if last:
        facts.append(t("home.res", cpu=pct(last["cpu"]), mem=pct(last["mem"]),
                       disk=pct(last["disk"])))
    if facts:
        lines += [""] + facts
    lines += ["", t("home.pick")]
    return "\n".join(lines), kb([
        [btn(t("b.files"), "fl", "primary"), btn(t("b.search"), "sq", "primary")],
        [btn(t("b.backup"), "bk", "success"), btn(t("b.basket", n=n), "bq")],
        [btn(t("b.status"), "st"), btn(t("b.services"), "sl")],
        [btn(t("b.disk"), "dk"), btn(t("b.procs"), "tp")],
        [btn(t("b.reports"), "rp"), btn(t("b.events"), "ev")],
        [btn(t("b.settings"), "se"), btn(t("b.help"), "hp")],
    ])


def files_view(page: int = 0, group: str = "") -> View:
    """The list of everything that can be browsed."""
    items = group_projects(group) if group else menu_projects()
    pages = max(1, math.ceil(len(items) / PROJECTS_PER_PAGE))
    page = max(0, min(page, pages - 1))
    chunk = items[page * PROJECTS_PER_PAGE:(page + 1) * PROJECTS_PER_PAGE]
    states = states_now()
    if group:
        head = t("fl.cfg_title") if group == "cfg" else t("fl.extra_title")
        text = f"<b>{head}</b>\n<i>{t('fl.count', n=len(items))}</i>\n"
    else:
        text = f"📁 <b>{t('fl.title')}</b>\n<i>{t('fl.count_auto', n=len(items))}</i>\n"
    for p in chunk:
        line = f"\n{esc(p.title)}\n{project_info(p, states)}"
        if not os.path.isdir(p.root):
            line += " · " + t("fl.missing")
        if len(text) + len(line) > 3500:
            break
        text += line
    if not items:
        text += "\n" + t("fl.none")
    rows = grid([btn(short(p.title, 30), f"d:{REG.put(p.key, p.root)}:0") for p in chunk])
    rows.append(pager(f"fg:{group}" if group else "fl", page, pages))
    if not group:
        extra = []
        n_cfg, n_extra = len(group_projects("cfg")), len(group_projects("extra"))
        if n_cfg:
            extra.append(btn(t("b.configs", n=n_cfg), "fg:cfg:0"))
        if n_extra:
            extra.append(btn(t("b.places", n=n_extra), "fg:extra:0"))
        rows.append(extra)
        rows.append([btn(t("b.search"), "sq", "primary"), btn(t("b.rescan"), "rs")])
        if CATALOG_AT:
            text += "\n\n" + t("fl.scanned", when=local(CATALOG_AT).strftime("%H:%M:%S"))
        rows.append(home_row())
    else:
        rows.append([btn(t("b.back"), "fl"), btn(t("b.home"), "m")])
    return text, kb(rows)


def list_dir(p: Project, path: str) -> list[os.DirEntry]:
    entries = list(os.scandir(path))
    if p.flat:
        entries = [e for e in entries if flat_ok(e.name) and e.is_file(follow_symlinks=False)]
    dirs = sorted((e for e in entries if e.is_dir(follow_symlinks=False)),
                  key=lambda e: e.name.lower())
    files = sorted((e for e in entries if not e.is_dir(follow_symlinks=False)),
                   key=lambda e: e.name.lower())
    return dirs + files


def selectable(path: str) -> bool:
    return not os.path.islink(path) and (os.path.isfile(path) or os.path.isdir(path))


async def open_dir(p: Project, path: str, page: int, ud: dict) -> View:
    """A folder screen. The listing itself runs in a worker thread, so a folder with a
    hundred thousand files does not hold up the rest of the bot."""
    try:
        items = await asyncio.to_thread(list_dir, p, path)
    except OSError as e:
        return t("dir.error", err=esc(e)), kb([home_row()])
    return dir_view(p, path, page, ud, items)


def dir_view(p: Project, path: str, page: int, ud: dict, items: list) -> View:
    n_dirs = sum(1 for e in items if e.is_dir(follow_symlinks=False))
    pages = max(1, math.ceil(len(items) / PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    chunk = items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    basket = basket_of(ud)
    sel = bool(ud.get("selmode"))
    at_root = os.path.realpath(path) == os.path.realpath(p.root)

    rel = os.path.relpath(path, p.root)
    rel = "/" if rel == "." else "/" + rel
    text = (f"{esc(p.title)}\n<code>{esc(short(rel, 200))}</code>\n"
            + t("dir.count", dirs=n_dirs, files=len(items) - n_dirs))
    if at_root:
        text += f"\n📍 {project_info(p, states_now())}"
    if not items:
        text += "\n\n" + t("dir.empty")
    if sel:
        text += "\n\n" + t("dir.selmode", n=len(basket))

    h_self = REG.put(p.key, path)
    rows: list[list[InlineKeyboardButton]] = []
    for e in chunk:
        full = os.path.join(path, e.name)
        h = REG.put(p.key, full)
        is_dir = e.is_dir(follow_symlinks=False)
        if e.is_symlink():  # shown, never followed
            rows.append([btn(f"🔗 {short(e.name, 40)}", "noop")])
            continue
        if is_dir:
            label = f"📁 {short(e.name, 26 if sel else 38)}"
        else:
            try:
                sz = human(e.stat(follow_symlinks=False).st_size)
            except OSError:
                sz = "?"
            label = f"📄 {short(e.name, 34)} · {sz}"
        if sel:
            mark = "✅" if full in basket else "⬜"
            row = [btn(f"{mark} {label}", f"t:{h}:{h_self}:{page}")]
            if is_dir:
                row.append(btn(t("b.open"), f"d:{h}:0"))
            rows.append(row)
        else:
            rows.append([btn(label, f"d:{h}:0" if is_dir else f"o:{h}")])

    rows.append(pager(f"d:{h_self}", page, pages))
    if sel:
        rows.append([btn(t("b.sel_page"), f"ta:{h_self}:{page}"),
                     btn(t("b.sel_none"), f"tn:{h_self}:{page}")])
        rows.append([btn(t("b.send_basket", n=len(basket)), "bq", "success")])
    bottom = []
    if not at_root:
        bottom.append(btn(t("b.up"), f"d:{REG.put(p.key, os.path.dirname(path))}:0"))
    if sel:
        bottom.append(btn(t("b.sel_done"), f"sx:{h_self}:{page}", "primary"))
    else:
        bottom.append(btn(t("b.zip_dir"), f"bd:{h_self}", "success"))
    rows.append(bottom)
    if not sel:
        rows.append([btn(t("b.multi"), f"sx:{h_self}:{page}"),
                     btn(t("b.basket", n=len(basket)), "bq")])
        if at_root and p.kind not in ("cfg", "extra"):
            rows.append([btn(t("b.project"), f"pj:{p.key}")])
    back = "fl" if p.kind not in ("cfg", "extra") else f"fg:{p.kind}:0"
    rows.append([btn(t("b.projects"), back), btn(t("b.home"), "m")])
    return text, kb(rows)


def file_view(p: Project, path: str, ud: dict) -> View:
    try:
        st = os.stat(path)
    except OSError as e:
        return f"❌ <code>{esc(e)}</code>", kb([home_row()])
    h = REG.put(p.key, path)
    parent = REG.put(p.key, os.path.dirname(path))
    rel = os.path.relpath(path, p.root)
    pinned = STORE is not None and rel in STORE.pins(p.root)
    text = (f"📄 <b>{esc(short(os.path.basename(path), 120))}</b>\n"
            f"💾 {human(st.st_size)} · 🕒 {stamp(st.st_mtime)}\n"
            f"{esc(p.title)} · <code>{esc(short(rel, 300))}</code>")
    if pinned:
        text += "\n" + t("file.pinned")
    if st.st_size > one_file_cap():
        text += "\n" + t("file.split", n=math.ceil(st.st_size / part_cap()),
                         size=human(part_cap()))
    in_basket = path in basket_of(ud)
    return text, kb([
        [btn(t("b.download"), f"dl:{h}", "success"), btn(t("b.preview"), f"v:{h}")],
        [btn(t("b.basket_del") if in_basket else t("b.basket_add"), f"af:{h}"),
         btn(t("b.unpin") if pinned else t("b.pin"), f"pin:{h}")],
        [btn(t("b.back"), f"d:{parent}:0"), btn(t("b.home"), "m")],
    ])


def project_view(p: Project, states: dict[str, UnitState], n_ess: int) -> View:
    kinds = {"app": t("pj.k_app"), "site": t("pj.k_site"), "dir": t("pj.k_dir"),
             "flat": t("pj.k_flat")}
    lines = [f"<b>{esc(p.title)}</b>", f"📍 <code>{esc(p.root)}</code>",
             f"🏷 {kinds.get(p.kind, p.kind)}"]
    if p.domains:
        lines.append("🌍 " + ", ".join(esc(d) for d in p.domains[:4]))
    if p.services:
        lines.append("")
        for sid in p.services:
            s = SMAP.get(sid)
            if s is not None:
                lines.append(service_line(s, states.get(sid)))
    else:
        lines += ["", t("pj.no_svc")]
    lines += ["", t("pj.ess", n=n_ess)]
    rows = [[btn(t("b.files"), f"d:{REG.put(p.key, p.root)}:0", "primary"),
             btn(t("b.backup"), f"b:{p.key}", "success")]]
    rows += grid([btn(f"📜 {short(SMAP[sid].label, 22)}", f"sv:{svc_token(sid)}")
                  for sid in p.services[:6] if sid in SMAP])
    manage = [btn(t("b.rename"), f"pr:{p.key}"), btn(t("b.hide"), f"ph:{p.key}", "danger")]
    rows.append(manage)
    if p.custom:
        rows.append([btn(t("b.autoname"), f"pa:{p.key}")])
    rows.append([btn(t("b.projects"), "fl"), btn(t("b.home"), "m")])
    return "\n".join(lines), kb(rows)


def basket_items(ud: dict) -> list[tuple[Project, str]]:
    out = []
    for path, pkey in list(basket_of(ud).items()):
        p = PMAP.get(pkey)
        if p and allowed(p, path) and selectable(path):
            out.append((p, path))
    return out


def _arc(p: Project, path: str) -> str:
    rel = os.path.relpath(path, p.root)
    return p.key if rel == "." else os.path.join(p.key, rel)


def basket_view(ud: dict) -> View:
    items = basket_items(ud)
    if not items:
        return (f"🧺 <b>{t('bq.empty')}</b>\n\n{t('bq.how')}",
                kb([[btn(t("b.files"), "fl", "primary"), btn(t("b.home"), "m")]]))
    lines, size, n_dirs = [], 0, 0
    for p, path in items:
        if os.path.isdir(path):
            n_dirs += 1
            lines.append(f"📁 <code>{esc(short(_arc(p, path), 90))}/</code>")
        else:
            with suppress(OSError):
                sz = os.path.getsize(path)
                size += sz
                lines.append(f"📄 <code>{esc(short(_arc(p, path), 90))}</code> · {human(sz)}")
    shown = lines[:25]
    if len(lines) > 25:
        shown.append(t("more", n=len(lines) - 25))
    text = (f"🧺 <b>{t('bq.title')}</b> — {t('bq.count', n=len(items))}\n"
            + t("bq.size", size=human(size)) + (" · " + t("bq.dirs", n=n_dirs) if n_dirs else "")
            + "\n\n" + "\n".join(shown) + "\n\n" + t("bq.hint"))
    return text, kb([
        [btn(t("b.one_zip"), "bqz", "success"), btn(t("b.one_by_one"), "bqs", "success")],
        [btn(t("b.clear"), "bqc", "danger"), btn(t("b.add_more"), "fl")],
        home_row(),
    ])


def backup_menu_view(page: int = 0) -> View:
    items = list(PROJECTS)
    pages = max(1, math.ceil(len(items) / PROJECTS_PER_PAGE))
    page = max(0, min(page, pages - 1))
    chunk = items[page * PROJECTS_PER_PAGE:(page + 1) * PROJECTS_PER_PAGE]
    rows = grid([btn(short(p.title, 30), f"b:{p.key}") for p in chunk])
    rows.append(pager("bk", page, pages))
    rows.append([btn(t("b.several"), "mp")])
    rows.append([btn(t("b.all_ess"), "ba", "success")])
    rows.append(home_row())
    text = f"📦 <b>{t('bk.title')}</b>\n{t('bk.sub')}\n\n<i>{t('bk.note')}</i>"
    if not items:
        text += "\n\n" + t("fl.none")
    return text, kb(rows)


def backup_project_view(p: Project, n_ess: int) -> View:
    text = f"📦 <b>{esc(p.title)}</b>\n{project_info(p)}\n\n"
    text += t("bp.ess", n=n_ess) if n_ess else t("bp.no_ess")
    text += "\n\n" + t("bp.legend")
    rows = [[btn(t("b.full"), f"bf:{p.key}", "success")]]
    if n_ess:
        rows.append([btn(t("b.ess", n=n_ess), f"bi:{p.key}", "success")])
        rows.append([btn(t("b.pick"), f"bs:{p.key}")])
    rows.append([btn(t("b.back"), "bk"), btn(t("b.home"), "m")])
    return text, kb(rows)


def select_view(p: Project, files: list[str], selected: set[int]) -> View:
    imp = files[:MAX_SELECT_FILES]
    selected = {i for i in selected if i < len(imp)}
    text = (f"☑️ <b>{esc(p.title)}</b>\n{t('sel.how')}\n"
            + t("sel.count", n=len(selected), total=len(imp)))
    if len(files) > len(imp):
        text += "\n" + t("sel.capped", n=len(imp), total=len(files))
    rows = []
    for i, rel in enumerate(imp):
        mark = "✅" if i in selected else "⬜"
        label = rel if len(rel) <= 40 else "…" + rel[-39:]
        rows.append([btn(f"{mark} {label}", f"bt:{p.key}:{i}")])
    rows.append([btn(t("b.all"), f"bsa:{p.key}"), btn(t("b.none"), f"bsn:{p.key}")])
    rows.append([btn(t("b.zip_n", n=len(selected)), f"bg:{p.key}", "success"),
                 btn(t("b.each_n", n=len(selected)), f"bgs:{p.key}", "success")])
    rows.append([btn(t("b.back"), f"b:{p.key}"), btn(t("b.home"), "m")])
    return text, kb(rows)


def multi_project_view(ud: dict, page: int = 0) -> View:
    sel: set[str] = ud.setdefault("msel", set())
    sel.intersection_update(PMAP)
    items = list(PROJECTS)
    per = 14
    pages = max(1, math.ceil(len(items) / per))
    page = max(0, min(page, pages - 1))
    chunk = items[page * per:(page + 1) * per]
    text = (f"☑️ <b>{t('mp.title')}</b>\n{t('mp.how')}\n"
            + t("sel.count", n=len(sel), total=len(items)) + "\n\n" + t("mp.legend"))
    rows = grid([btn(("✅ " if p.key in sel else "⬜ ") + short(p.title, 26), f"mt:{p.key}:{page}")
                 for p in chunk])
    rows.append(pager("mp", page, pages))
    rows.append([btn(t("b.all"), f"ma:{page}"), btn(t("b.none"), f"mn:{page}")])
    rows.append([btn(t("b.m_full_zip"), "mg:f:z", "success"),
                 btn(t("b.m_full_each"), "mg:f:s", "success")])
    rows.append([btn(t("b.m_ess_zip"), "mg:i:z", "success"),
                 btn(t("b.m_ess_each"), "mg:i:s", "success")])
    rows.append([btn(t("b.back"), "bk"), btn(t("b.home"), "m")])
    return text, kb(rows)


def read_preview(path: str) -> str | None:
    with open(path, "rb") as f:
        head = f.read(64 * 1024)
    if b"\0" in head:
        return None
    text = head.decode("utf-8", "replace")
    name = os.path.basename(path)
    # values in .env files and Environment= lines are hidden in previews (downloads are complete)
    if name.startswith(".env"):
        text = re.sub(r"^(\s*[^#=\s][^=\n]*=).*$", r"\1••••••", text, flags=re.M)
    elif name.endswith((".service", ".conf")):
        text = re.sub(r"^(\s*Environment=\s*\"?[A-Za-z_][A-Za-z0-9_]*=).*$", r"\1••••••", text,
                      flags=re.M)
    return text


# ── search ──
def search_prompt_view() -> View:
    return (f"🔎 <b>{t('sq.title')}</b>\n\n{t('sq.how')}",
            kb([[btn(t("b.projects"), "fl"), btn(t("b.home"), "m")]]))


def search_view(ud: dict, page: int = 0) -> View:
    state = ud.get("search") or {}
    hits: list[Hit] = state.get("hits") or []
    query = state.get("q", "")
    content = state.get("mode") == "content"
    info = state.get("info") or {}
    head = t("sr.title_c") if content else t("sr.title")
    text = f"🔎 <b>{head}</b> · <code>{esc(short(query, 60))}</code>\n"
    bits = [t("sr.found", n=len(hits)), t("sr.scanned", n=f"{info.get('scanned', 0):,}"),
            f"{info.get('secs', 0):.1f}s"]
    text += "<i>" + " · ".join(bits) + "</i>\n"
    if info.get("fuzzy") and hits and not content:
        text += t("sr.fuzzy") + "\n"
    if info.get("cut"):
        text += t("sr.cut") + "\n"
    per = PAGE_SIZE
    pages = max(1, math.ceil(len(hits) / per))
    page = max(0, min(page, pages - 1))
    rows: list[list[InlineKeyboardButton]] = []
    for h in hits[page * per:(page + 1) * per]:
        p = PMAP.get(h.pkey)
        if p is None:
            continue
        rel = short(os.path.relpath(h.path, p.root), 110)
        where = short(os.path.dirname(os.path.relpath(h.path, p.root)), 110)
        name = short(os.path.basename(h.path), 80)
        token = REG.put(p.key, h.path)
        if content:
            text += (f"\n📄 <b>{esc(name)}</b> · {esc(p.title)}\n"
                     f"<code>{esc(rel)}:{h.line}</code>" + (f" ×{h.score}" if h.score > 1 else "")
                     + f"\n<code>{esc(h.text)}</code>\n")
        else:
            icon = "📁" if h.is_dir else "📄"
            text += (f"\n{icon} <b>{esc(name)}</b>" + ("" if h.is_dir else f" · {human(h.size)}")
                     + f"\n{esc(p.title)} · <code>{esc('/' + where if where else '/')}</code>\n")
        label = f"{'📁' if h.is_dir else '📄'} {short(name, 40)}"
        rows.append([btn(label, f"d:{token}:0" if h.is_dir else f"o:{token}")])
    if not hits:
        text += "\n" + (t("sr.none_c") if content else t("sr.none"))
    rows.append(pager("sr", page, pages))
    tools = [btn(t("b.new_search"), "sq", "primary")]
    if query and not content:
        tools.append(btn(t("b.grep"), "sc"))
    elif query:
        tools.append(btn(t("b.by_name"), "sn"))
    rows.append(tools)
    rows.append(home_row())
    return text, kb(rows)


# ════════════════════════════════════════════════════════════════
#  Screens: status, services, logs, disk, processes, reports, settings
# ════════════════════════════════════════════════════════════════

_FAILED: tuple[float, list[str]] = (0.0, [])


def state_word(sid: str, st: UnitState | None) -> str:
    if st is None:
        return t("sv.unknown")
    if is_up(st):
        return t("sv.unhealthy") if st.health == "unhealthy" else t("sv.running")
    if st.state in _BUSY_STATES:
        return t("sv.busy", state=esc(st.sub or st.state))
    if st.state == "failed":
        return t("sv.failed")
    return t("sv.stopped") if state_icon(sid, st) == "🔴" else t("sv.off")


def service_line(s: Svc, st: UnitState | None) -> str:
    """`🟢 ⚙️ name · 3d 4h · 84 MB`  or  `🔴 ⚙️ name — failed (exit-code)`"""
    line = f"{state_icon(s.sid, st)} {svc_icon(s)} <code>{esc(s.label)}</code>"
    if st is not None and is_up(st):
        bits = []
        if st.since:
            bits.append(dur(time.time() - st.since))
        if st.mem:
            bits.append(human(st.mem))
        if st.health == "unhealthy":
            bits.append(t("sv.unhealthy"))
        return line + ("".join(" · " + b for b in bits))
    line += " — " + state_word(s.sid, st)
    if st is not None and st.result and st.result != "success":
        line += f" ({esc(st.result)})"
    return line


async def failed_units() -> list[str]:
    """Units systemd itself marks as failed, beyond the ones the bot already lists."""
    global _FAILED
    if not HOST.systemd:
        return []
    if time.time() - _FAILED[0] > 60:
        out = await run_cmd("systemctl", "list-units", "--state=failed", "--plain",
                            "--no-legend", "--no-pager", timeout=10)
        names = []
        for line in out.splitlines():
            parts = line.replace("●", " ").replace("×", " ").replace("*", " ").split()
            if parts and "." in parts[0] and not parts[0].startswith("("):
                names.append(parts[0])
        _FAILED = (time.time(), names[:20])
    known = {s.sid + ".service" for s in SERVICES if s.kind == "unit"}
    return [n for n in _FAILED[1] if n not in known]


def self_usage() -> tuple[int, float]:
    """(memory, CPU %) of the bot itself."""
    rss = 0
    with suppress(Exception):
        rss = int(read_text("/proc/self/statm", 128).split()[1]) * PAGE_BYTES
    return rss, (MON.self_pct if MON is not None else 0.0)


async def status_view() -> View:
    h = HOST
    now = time.time()
    try:
        mu, mt, su, st = read_mem()
    except (OSError, ValueError):
        mu = mt = su = st = 0
    mounts = await MON.get_mounts() if MON is not None else read_mounts()
    last = MON.last if MON is not None else {}
    if "cpu" in last:
        cpu_now = last["cpu"]
    else:  # the first minute after a start: take a quick reading
        a = read_cpu()
        await asyncio.sleep(0.3)
        b = read_cpu()
        cpu_now = 100.0 * (b[0] - a[0]) / (b[1] - a[1]) if b[1] > a[1] else 0.0
    mem_pct = 100.0 * mu / mt if mt else 0.0
    table = [f"CPU  {bar(cpu_now)} {cpu_now:5.1f}%", f"RAM  {bar(mem_pct)} {mem_pct:5.1f}%"]
    if st:
        table.append(f"Swap {bar(100.0 * su / st)} {100.0 * su / st:5.1f}%")
    for m in mounts[:4]:
        table.append(f"Disk {bar(m.pct)} {m.pct:5.1f}%  {short(m.path, 12)}")
    load = os.getloadavg()
    lines = [
        f"📊 <b>{t('st.title')}</b>",
        f"<code>{esc(h.hostname)}</code> · {esc(h.os)}",
        esc(host_line(h)),
        t("st.up", up=dur(now - boot_time()), now=local(now).strftime("%H:%M"), tz=esc(TZ_NAME)),
        "<pre>" + esc("\n".join(table)) + "</pre>",
        t("st.load", a=f"{load[0]:.2f}", b=f"{load[1]:.2f}", c=f"{load[2]:.2f}", cores=h.cores),
        t("st.mem", used=human(mu), total=human(mt))
        + (" · " + t("st.swap", used=human(su), total=human(st)) if st else ""),
    ]
    for m in mounts[:4]:
        lines.append(t("st.disk", path=esc(m.path), used=human(m.used), total=human(m.total),
                       free=human(m.free)))
    if "rx" in last:
        lines.append(t("st.net", rx=human(last["rx"]), tx=human(last["tx"])))

    states = await MON.snapshot() if MON is not None else await poll_states(SERVICES)
    if SERVICES:
        icons = [state_icon(s.sid, states.get(s.sid)) for s in SERVICES]
        n_up = icons.count("🟢")
        n_bad = icons.count("🔴") + icons.count("🟠")
        n_other = len(icons) - n_up - n_bad
        summary = t("st.svc", up=n_up, n=len(icons))
        if n_bad:
            summary += " · " + t("st.svc_bad", n=n_bad)
        if n_other:
            summary += " · " + t("st.svc_off", n=n_other)
        lines += ["", summary]
        shown = 0
        for s, icon in zip(SERVICES, icons):
            if icon in ("🔴", "🟠", "🟡") and shown < 8:
                lines.append(service_line(s, states.get(s.sid)))
                shown += 1
    else:
        lines += ["", t("st.no_svc")]
    extra = await failed_units()
    if extra:
        lines.append(t("st.failed", names=", ".join(f"<code>{esc(n)}</code>" for n in extra[:6])))
    if SITES:
        doms = sum(len(s.domains) for s in SITES)
        lines.append(t("st.sites", n=len(SITES), d=doms))
    if MON is not None and MON.certs:
        label, _path, exp = MON.certs[0]
        days = math.floor((exp - now) / 86400)
        mark = "🔴" if days < 7 else ("🟡" if days < 21 else "🟢")
        lines.append(t("st.ssl", mark=mark, name=esc(short(label, 40)), days=days,
                       n=len(MON.certs)))
    rss, cpu_self = self_usage()
    lines += ["", t("st.self", mem=human(rss), cpu=f"{cpu_self:.1f}",
                    data=human(data_dir_bytes()), cap=human(DATA_MAX_BYTES))]
    return "\n".join(lines), kb([
        [btn(t("b.refresh"), "st", "primary"), btn(t("b.services"), "sl")],
        [btn(t("b.disk"), "dk"), btn(t("b.procs"), "tp")],
        [btn(t("b.reports"), "rp"), btn(t("b.home"), "m")],
    ])


async def services_view(page: int = 0) -> View:
    states = await MON.snapshot() if MON is not None else await poll_states(SERVICES)
    svcs = list(SERVICES)
    icons = {s.sid: state_icon(s.sid, states.get(s.sid)) for s in svcs}
    vals = list(icons.values())
    head = (f"⚙️ <b>{t('sl.title')}</b> — 🟢 {vals.count('🟢')} · "
            f"🔴 {vals.count('🔴') + vals.count('🟠')} · "
            f"⚪️ {len(vals) - vals.count('🟢') - vals.count('🔴') - vals.count('🟠')}")
    pages = max(1, math.ceil(len(svcs) / SERVICES_PER_PAGE))
    page = max(0, min(page, pages - 1))
    chunk = svcs[page * SERVICES_PER_PAGE:(page + 1) * SERVICES_PER_PAGE]
    lines = [head]
    group = ""
    names = {"app": t("sl.g_app"), "sys": t("sl.g_sys"), "docker": t("sl.g_docker"),
             "proc": t("sl.g_proc")}
    for s in chunk:
        if s.group != group:
            group = s.group
            lines += ["", f"<b>{names[group]}</b>"]
        lines.append(service_line(s, states.get(s.sid)))
    if not svcs:
        lines += ["", t("sl.none") if HOST.systemd or HOST.docker_sock else t("sl.no_systemd")]
    else:
        lines += ["", f"<i>{t('sl.hint')}</i>"]
    rows = grid([btn(f"{icons[s.sid]} {short(s.label, 24)}", f"sv:{svc_token(s.sid)}")
                 for s in chunk])
    rows.append(pager("sl", page, pages))
    rows.append([btn(t("b.refresh"), f"sl:{page}", "primary"), btn(t("b.home"), "m")])
    return "\n".join(lines), kb(rows)


async def service_view(s: Svc) -> View:
    states = await MON.snapshot(max_age=10) if MON is not None else await poll_states((s,))
    st = states.get(s.sid)
    kind = {"unit": t("sv.k_unit"), "docker": t("sv.k_docker"), "proc": t("sv.k_proc")}[s.kind]
    lines = [f"{svc_icon(s)} <b>{esc(s.label)}</b> — {state_icon(s.sid, st)} {state_word(s.sid, st)}",
             f"🏷 {kind}" + (f" · {esc(st.enabled)}" if st is not None and st.enabled else "")]
    if st is not None:
        if st.since:
            lines.append(t("sv.since", when=stamp(st.since), ago=dur(time.time() - st.since)))
        if st.mem:
            lines.append(t("sv.mem", mem=human(st.mem)))
        if st.nrest:
            lines.append(t("sv.nrest", n=st.nrest))
        if st.result and st.result != "success":
            lines.append(t("sv.result", why=esc(st.result)))
        if not is_up(st) and st.sub and st.sub != st.state:
            lines.append(t("sv.sub", sub=esc(st.sub)))
    proj = next((p for p in PROJECTS if p.root == s.root), None) if s.root else None
    if proj is not None:
        lines.append(f"📍 {esc(proj.title)} · <code>{esc(proj.root)}</code>")
    elif s.root:
        lines.append(f"📍 <code>{esc(s.root)}</code>")
    muted = s.sid in SET["muted"]
    if muted:
        lines.append(t("sv.muted"))
    tok = svc_token(s.sid)
    rows = []
    if s.kind in ("unit", "docker"):
        rows.append([btn(t("b.logs"), f"lv:{tok}", "primary"), btn(t("b.errors"), f"le:{tok}")])
        second = [btn(t("b.logfile", n=LOG_FILE_LINES), f"lf:{tok}", "success")]
        if s.kind == "unit":
            second.append(btn(t("b.unitfile"), f"uf:{tok}"))
        rows.append(second)
    else:
        lines += ["", t("sv.proc_note")]
    third = []
    if proj is not None:
        third.append(btn(t("b.project"), f"pj:{proj.key}"))
    third.append(btn(t("b.unmute") if muted else t("b.mute"), f"mu:{tok}",
                     None if muted else "danger"))
    rows.append(third)
    rows.append([btn(t("b.services"), "sl"), btn(t("b.home"), "m")])
    return "\n".join(lines), kb(rows)


async def fetch_log(s: Svc, lines: int, iso: bool = False) -> str:
    if s.kind == "unit" and HOST.journal:
        return await run_cmd("journalctl", "-u", s.sid, "-n", str(lines), "--no-pager",
                             "-o", "short-iso" if iso else "short", timeout=30, limit=8_000_000)
    if s.kind == "docker":
        return await docker_logs(s.label, lines)
    return ""


async def log_view(s: Svc, errors_only: bool = False) -> View:
    tok = svc_token(s.sid)
    name = f"{svc_icon(s)} <b>{esc(s.label)}</b>"
    if errors_only:
        raw = await fetch_log(s, LOG_FILE_LINES)
        hits = []
        for ln in raw.splitlines():
            b = ln.encode("utf-8", "replace")
            if ERR_RE.search(b) or WARN_RE.search(b):
                hits.append(ln)
        out = "\n".join(hits[-LOG_LINES:])
        head = f"⚠️ {name} — " + t("lg.errors", n=len(hits), lines=LOG_FILE_LINES)
    else:
        out = await fetch_log(s, LOG_LINES)
        head = f"📜 {name} — " + t("lg.last", n=LOG_LINES)
    out = out[-3300:] or t("lg.empty")
    text = f"{head}\n<pre>{esc(out)}</pre>"
    if errors_only:
        top = [btn(t("b.refresh"), f"le:{tok}", "primary"), btn(t("b.all_lines"), f"lv:{tok}")]
    else:
        top = [btn(t("b.refresh"), f"lv:{tok}", "primary"), btn(t("b.errors"), f"le:{tok}")]
    return text, kb([
        top,
        [btn(t("b.logfile", n=LOG_FILE_LINES), f"lf:{tok}", "success")],
        [btn(t("b.back"), f"sv:{tok}"), btn(t("b.home"), "m")],
    ])


# ── disk ──
async def disk_view() -> View:
    mounts = await MON.get_mounts(max_age=5) if MON is not None else read_mounts()
    lines = [f"💽 <b>{t('dk.title')}</b>"]
    for m in mounts[:8]:
        meta = " · ".join(x for x in (m.fstype, m.device) if x)
        lines += ["", f"{level(m.pct)} <b>{esc(m.path)}</b>" + (f"  <i>{esc(meta)}</i>" if meta else ""),
                  f"<code>{bar(m.pct, 16)} {m.pct:.0f}%</code>",
                  t("dk.line", used=human(m.used), total=human(m.total), free=human(m.free))]
        if m.inodes_pct is not None and m.inodes_pct >= 80:
            lines.append(t("dk.inodes", pct=f"{m.inodes_pct:.0f}"))
    for path, fstype in SILENT_MOUNTS[:4]:
        lines += ["", t("dk.silent", path=esc(path), fs=esc(fstype))]
    lines += ["", f"<i>{t('dk.hint')}</i>"]
    return "\n".join(lines), kb([
        [btn(t("b.usage"), "du", "primary"), btn(t("b.chart"), "di")],
        [btn(t("b.refresh"), "dk"), btn(t("b.home"), "m")],
    ])


def usage_view(u: Usage) -> View:
    lines = [f"🔍 <b>{t('du.title')}</b>",
             f"<i>{t('du.when', when=local(u.at).strftime('%H:%M'))}</i>", ""]
    top = max([r.size for r in u.rows] + [1])
    for r in u.rows[:16]:
        mark = "≥ " if r.partial else ""
        lines.append(f"<code>{bar(100.0 * r.size / top, 8)}</code> <b>{mark}{human(r.size)}</b>"
                     f"  {esc(short(r.label, 34))}")
    if not u.rows:
        lines.append(t("du.none"))
    if u.files:
        lines += ["", f"<b>{t('du.files')}</b>"]
        for size, path in u.files[:6]:
            lines.append(f"<b>{human(size)}</b>  <code>{esc(short(path, 70))}</code>")
    if u.partial:
        lines += ["", f"<i>{t('du.partial', secs=int(USAGE_SECONDS))}</i>"]
    return "\n".join(lines), kb([
        [btn(t("b.remeasure"), "dur", "primary"), btn(t("b.chart"), "di")],
        [btn(t("b.back"), "dk"), btn(t("b.home"), "m")],
    ])


async def procs_view() -> View:
    cpu, mem, total = await top_procs()
    lines = [f"🧮 <b>{t('tp.title')}</b> — {t('tp.count', n=total)}", "", f"<b>{t('tp.cpu')}</b>"]
    if not cpu:
        lines.append(t("tp.idle"))
    for share, pr in cpu:
        owner = proc_owner(pr)
        lines.append(f"<code>{share:5.1f}%</code> {esc(proc_title(pr))}"
                     + (f" · <i>{esc(owner)}</i>" if owner else ""))
    lines += ["", f"<b>{t('tp.mem')}</b>"]
    for pr in mem:
        owner = proc_owner(pr)
        lines.append(f"<code>{human(pr.rss):>9}</code> {esc(proc_title(pr))}"
                     + (f" · <i>{esc(owner)}</i>" if owner else ""))
    lines += ["", f"<i>{t('tp.note', cores=HOST.cores)}</i>"]
    return "\n".join(lines), kb([
        [btn(t("b.refresh"), "tp", "primary"), btn(t("b.status"), "st")],
        home_row(),
    ])


# ── reports and events ──
def schedule_text() -> str:
    mode = SET["report"]
    if mode == "daily":
        return t("rp.s_daily", hour=f"{int(SET['report_hour']):02d}:00")
    if mode == "weekly":
        return t("rp.s_weekly", day=day_name(int(SET["report_wd"])),
                 hour=f"{int(SET['report_hour']):02d}:00")
    return t("rp.s_off")


def report_menu_view() -> View:
    lines = [f"📈 <b>{t('rp.title')}</b>", ""]
    lo, _hi, n = STORE.span() if STORE else (None, None, 0)
    if n and lo:
        lines.append(t("rp.since", when=stamp(lo), n=f"{n:,}"))
    else:
        lines.append(t("rp.nothing"))
    lines.append(t("rp.size", size=human(data_dir_bytes()), cap=human(DATA_MAX_BYTES),
                   days=RETENTION_DAYS))
    lines.append("🗓 " + schedule_text())
    if Image is None:
        lines += ["", t("rp.no_pillow")]
    lines += ["", t("rp.pick")]
    return "\n".join(lines), kb([
        [btn(t("b.r1"), "rg:1", "primary"), btn(t("b.r7"), "rg:7", "primary"),
         btn(t("b.r30"), "rg:30", "primary")],
        [btn(t("b.schedule"), "se"), btn(t("b.home"), "m")],
    ])


EVENT_ICONS = {"crash": "♻️", "restart": "🔁", "down": "🔴", "up": "🟢", "admin": "👤",
               "alert": "⚠️", "new": "🆕", "gone": "🗑"}


def events_view() -> View:
    rows = STORE.fetch_events(limit=25) if STORE else []
    lines = []
    for ts, kind, who, detail in rows:
        when = local(ts).strftime("%m-%d %H:%M")
        icon = EVENT_ICONS.get(kind, "•")
        if kind == "admin":
            body = f"<code>{esc(who)}</code> {esc(short(detail or '', 90))}"
        else:
            body = f"<code>{esc(svc_plain(who))}</code> {t('ev.' + kind) if kind in EVENT_ICONS else esc(kind)}"
            if detail and kind in ("down", "alert", "new", "gone") and detail != "service":
                body += f" ({esc(short(detail, 60))})"
        lines.append(f"<code>{when}</code> {icon} {body}")
    text = f"🧾 <b>{t('ev.title')}</b>\n<i>{t('ev.sub')}</i>\n\n"
    text += "\n".join(lines) if lines else t("ev.none")
    return text, kb([[btn(t("b.refresh"), "ev", "primary"), btn(t("b.home"), "m")]])


# ── settings ──
DISK_STEPS = (80, 85, 90, 95)
MEM_STEPS = (85, 90, 95, 98)
REPORT_STEPS = ("off", "daily", "weekly")
WEEK_STARTS = (0, 5, 6)   # Monday, Saturday, Sunday


def settings_view(uid: int) -> View:
    lang = LANG.get()
    big = BIG.status()
    big_text = {"on": t("se.big_on", size=human(MT_PART_SIZE)), "off": t("se.big_off"),
                "missing": t("se.big_missing"), "paused": t("se.big_paused")}[big]
    lines = [
        f"🛠 <b>{t('se.title')}</b>", "",
        t("se.lang", v="فارسی" if lang == "fa" else "English"),
        t("se.alerts", v=t("on") if SET["alerts"] else t("off")),
        t("se.disk", v=SET["disk_pct"]),
        t("se.mem", v=SET["mem_pct"], n=MEM_ALERT_SAMPLES),
        "🗓 " + schedule_text(),
        t("se.week", v=day_name(int(SET["week_start"]))),
        t("se.hidden", n=len(HIDDEN)),
        "", big_text,
    ]
    if BIG.error and big != "on":
        lines.append(f"<code>{esc(BIG.error)}</code>")
    lines += [
        t("se.limits", one=human(one_file_cap()), job=human(MAX_JOB_BYTES)),
        "",
        f"<i>{APP_NAME} {__version__} · Python {sys.version_info.major}.{sys.version_info.minor}"
        f" · {esc(TZ_NAME)}</i>",
    ]
    hour = int(SET["report_hour"])
    rows = [
        [btn("🌐 English" if lang == "fa" else "🌐 فارسی", "set:lang", "primary"),
         btn(t("b.alerts_off") if SET["alerts"] else t("b.alerts_on"), "set:alerts",
             "danger" if SET["alerts"] else "success")],
        [btn(t("b.disk_at", v=SET["disk_pct"]), "set:disk"),
         btn(t("b.mem_at", v=SET["mem_pct"]), "set:mem")],
        [btn(t("b.report_mode", v=t("rp.m_" + SET["report"])), "set:report")],
    ]
    if SET["report"] != "off":
        timing = [btn("➖", "set:hour:-1"), btn(f"🕘 {hour:02d}:00", "noop"),
                  btn("➕", "set:hour:1")]
        if SET["report"] == "weekly":
            timing.insert(0, btn(f"📆 {day_name(int(SET['report_wd']))}", "set:wd"))
        rows.append(timing)
    rows.append([btn(t("b.week_start", v=day_name(int(SET["week_start"]))), "set:week")])
    if HIDDEN:
        rows.append([btn(t("b.hidden", n=len(HIDDEN)), "hd")])
    rows.append(home_row())
    return "\n".join(lines), kb(rows)


def hidden_view() -> View:
    lines = [f"🙈 <b>{t('hd.title')}</b>", t("hd.sub"), ""]
    rows = []
    for i, p in enumerate(HIDDEN[:30]):
        lines.append(f"{esc(p.title)} · <code>{esc(p.root)}</code>")
        rows.append([btn(f"👁 {short(p.title, 34)}", f"uh:{i}", "success")])
    if not HIDDEN:
        lines.append(t("hd.none"))
    rows.append([btn(t("b.settings"), "se"), btn(t("b.home"), "m")])
    return "\n".join(lines), kb(rows)


def help_view() -> View:
    text = (f"❔ <b>{APP_NAME}</b> — {t('hp.tag')}\n\n{t('hp.body')}\n\n"
            f"<b>{t('hp.cmds')}</b>\n{t('hp.cmdlist')}\n\n<i>{t('hp.safe')}</i>")
    return text, kb([[btn(t("b.files"), "fl", "primary"), btn(t("b.status"), "st")], home_row()])


# ════════════════════════════════════════════════════════════════
#  Handlers
# ════════════════════════════════════════════════════════════════

SEARCH_LOCK = LazyLock()
COMMANDS = ("start", "status", "services", "disk", "top", "report", "logs", "find",
            "backup", "events", "settings", "help")


async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admins only, private chat only. Also picks the language for this update."""
    user = update.effective_user
    chat = update.effective_chat
    private = bool(chat and chat.type == "private")
    code = ((user.language_code if user else "") or "").lower()
    guess = "fa" if code.startswith("fa") else "en"
    if user and user.id in ADMIN_IDS and private:
        lang = SET["langs"].get(str(user.id))
        if lang not in ("fa", "en"):
            lang = DEFAULT_LANG if DEFAULT_LANG in ("fa", "en") else guess
            SET.set("langs", {**SET["langs"], str(user.id): lang})
        LANG.set(lang)
        return
    LANG.set(DEFAULT_LANG if DEFAULT_LANG in ("fa", "en") else guess)
    log.warning("access denied: user=%s chat=%s", user.id if user else None,
                chat.type if chat else None)
    if private and user:
        # telling someone their own id is harmless and is exactly what setup needs
        text = t("setup.id" if not ADMIN_IDS else "gate.denied", uid=user.id)
        with suppress(Exception):
            if update.callback_query:
                await update.callback_query.answer(t("gate.short"), show_alert=True)
            elif update.message:
                await update.message.reply_text(text)
    raise ApplicationHandlerStop


async def edit(q, view: View) -> None:
    text, markup = aligned(view[0]), view[1]
    try:
        await q.edit_message_text(text, reply_markup=markup)
    except BadRequest as e:
        msg = str(e).lower()
        if "not modified" in msg:
            return
        if "no text in the message" in msg or "message can't be edited" in msg:
            await q.get_bot().send_message(q.message.chat_id, text, reply_markup=markup)
            return
        raise


async def reply(update: Update, view: View) -> None:
    await update.message.reply_text(aligned(view[0]), reply_markup=view[1])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("await", None)
    await fresh_states()
    await reply(update, home_view(context.user_data))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, help_view())


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await sync_catalog(context.application, max_age=FRESH_AGE)
    await reply(update, await status_view())


async def cmd_services(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await sync_catalog(context.application, max_age=FRESH_AGE)
    await reply(update, await services_view())


async def cmd_disk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, await disk_view())


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, await procs_view())


async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await sync_catalog(context.application, max_age=FRESH_AGE)
    await reply(update, backup_menu_view())


async def cmd_events(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, events_view())


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, settings_view(update.effective_user.id))


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(t("id.yours", uid=update.effective_user.id))


def _report_titles(days: int) -> tuple[str, str]:
    known = {1: ("rp.t_1", "Last 24 hours"), 7: ("rp.t_7", "Last 7 days"),
             30: ("rp.t_30", "Last 30 days")}
    if days in known:
        return t(known[days][0]), known[days][1]
    return t("rp.t_n", n=days), f"Last {days} days"


async def run_report(update: Update, bot, chat_id: int, days: int) -> None:
    days = max(1, min(RETENTION_DAYS, days))
    t1 = int(time.time()) // 300 * 300
    title, title_en = _report_titles(days)
    audit(update, "report", f"{days}d")
    try:
        await send_report(bot, chat_id, t1 - days * 86400, t1, title, title_en)
    except Exception:
        log.exception("report failed")
        await bot.send_message(chat_id, t("rp.failed"))


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    arg = (context.args[0] if context.args else "").lower().rstrip("dh")
    if arg.isdigit():
        days = 1 if arg == "24" else int(arg)
        await run_report(update, context.bot, update.effective_chat.id, days)
    else:
        await reply(update, report_menu_view())


async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await sync_catalog(context.application, max_age=FRESH_AGE)
    want = " ".join(context.args or []).strip().lower()
    if want:
        match = ([s for s in SERVICES if s.label.lower() == want or s.sid.lower() == want]
                 or [s for s in SERVICES if want in s.label.lower()])
        if match:
            await reply(update, await log_view(match[0]))
            return
        await update.message.reply_text(t("lg.unknown", name=esc(want)))
    await reply(update, await services_view())


async def do_search(message, context: ContextTypes.DEFAULT_TYPE, query: str,
                    content: bool = False, note=None) -> None:
    """Run a search and show the first page of results."""
    ud = context.user_data
    query = " ".join(query.split())[:80]
    if len(query) < 2:
        await message.reply_text(t("sq.short"))
        return
    if SEARCH_LOCK.locked():
        await message.reply_text(t("sq.busy"))
        return
    async with SEARCH_LOCK:
        await sync_catalog(context.application, max_age=FRESH_AGE)
        if note is None:
            note = await message.reply_text(t("sq.wait_c" if content else "sq.wait",
                                              q=esc(query)))
        fn = search_content if content else search_names
        try:
            res = await asyncio.to_thread(fn, query, PROJECTS)
        except Exception:
            log.exception("search failed")
            res = {"hits": [], "scanned": 0, "cut": False, "secs": 0.0}
    ud["search"] = {"q": query, "hits": res["hits"], "mode": "content" if content else "name",
                    "info": {k: v for k, v in res.items() if k != "hits"}}
    text, markup = search_view(ud)
    text = aligned(text)
    try:
        await note.edit_text(text, reply_markup=markup)
    except BadRequest:
        await message.reply_text(text, reply_markup=markup)


async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args or [])
    if not query.strip():
        await reply(update, search_prompt_view())
        return
    audit(update, "search", query[:60])
    await do_search(update.message, context, query)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Plain text = a search, unless the bot just asked for a new project name."""
    ud = context.user_data
    text = (update.message.text or "").strip()
    waiting = ud.pop("await", None)
    if waiting and waiting[0] == "rename":
        root = waiting[1]
        title = " ".join(text.split())[:48]
        if not title:
            return
        SET.set("titles", {**SET["titles"], root: title})
        await sync_catalog(context.application, force=True)
        p = next((x for x in PROJECTS if x.root == root), None)
        rows = [[btn(t("b.project"), f"pj:{p.key}", "primary")]] if p else []
        rows.append(home_row())
        await update.message.reply_text(t("pj.renamed", title=esc(title)), reply_markup=kb(rows))
        return
    audit(update, "search", text[:60])
    await do_search(update.message, context, text)


async def router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _USAGE
    q = update.callback_query
    parts = (q.data or "").split(":")
    cmd, args = parts[0], parts[1:]
    chat_id = update.effective_chat.id
    uid = update.effective_user.id
    ud = context.user_data
    bot = context.bot
    app = context.application

    async def expired() -> None:
        await q.answer(t("expired"), show_alert=True)

    def resolved(i: int = 0, *, want_dir: bool | None = None):
        res = REG.resolve(args[i]) if len(args) > i else None
        if not res:
            return None
        if want_dir is True and not os.path.isdir(res[1]):
            return None
        if want_dir is False and not os.path.isfile(res[1]):
            return None
        return res

    def page_arg(i: int) -> int:
        return int(args[i]) if len(args) > i and args[i].isdigit() else 0

    def project_arg() -> Project | None:
        return PMAP.get(args[0]) if args else None

    def service_arg() -> Svc | None:
        return svc_from_token(args[0]) if args else None

    if cmd == "noop":
        await q.answer()
        return
    if cmd != "pr":
        ud.pop("await", None)   # any other button cancels a pending "send me a name"

    # ── home and navigation ──
    if cmd == "m":
        await q.answer()
        await fresh_states()
        await edit(q, home_view(ud))

    elif cmd == "hp":
        await q.answer()
        await edit(q, help_view())

    elif cmd == "fl":
        await q.answer()
        await sync_catalog(app, max_age=FRESH_AGE)
        await fresh_states()
        await edit(q, files_view(page_arg(0)))

    elif cmd == "fg":
        if not args or args[0] not in ("cfg", "extra"):
            return await expired()
        await q.answer()
        await edit(q, files_view(page_arg(1), args[0]))

    elif cmd == "rs":  # scan the server again, on request
        before = {p.root for p in PROJECTS}
        if not await sync_catalog(app, force=True):
            return await q.answer(t("rs.failed"), show_alert=True)
        added = len({p.root for p in PROJECTS} - before)
        await q.answer(t("rs.done", p=len(PROJECTS), s=len(SERVICES))
                       + " · " + (t("rs.new", n=added) if added else t("rs.same")))
        await fresh_states(5)
        await edit(q, files_view())

    elif cmd == "d":
        res = resolved(want_dir=True)
        if not res:
            return await expired()
        await q.answer()
        await edit(q, await open_dir(res[0], res[1], page_arg(1), ud))

    elif cmd == "o":
        res = resolved(want_dir=False)
        if not res:
            return await expired()
        await q.answer()
        await edit(q, file_view(res[0], res[1], ud))

    # ── one project ──
    elif cmd == "pj":
        p = project_arg()
        if not p:
            return await expired()
        await q.answer()
        states = await MON.snapshot() if MON is not None else {}
        files = await asyncio.to_thread(essentials, p)
        await edit(q, project_view(p, states, len(files)))

    elif cmd == "pr":
        p = project_arg()
        if not p:
            return await expired()
        ud["await"] = ("rename", p.root)
        await q.answer()
        await edit(q, (t("pj.rename_how", title=esc(p.title)),
                       kb([[btn(t("b.cancel"), f"pj:{p.key}", "danger")]])))

    elif cmd in ("ph", "pa"):
        p = project_arg()
        if not p:
            return await expired()
        if cmd == "ph":
            SET.set("hidden", sorted(set(SET["hidden"]) | {p.root}))
            await q.answer(t("pj.hidden"), show_alert=True)
        else:
            SET.set("titles", {k: v for k, v in SET["titles"].items() if k != p.root})
            await q.answer(t("pj.auto"))
        await sync_catalog(app, force=True)
        await edit(q, files_view())

    elif cmd == "hd":
        await q.answer()
        await edit(q, hidden_view())

    elif cmd == "uh":
        i = page_arg(0)
        if i >= len(HIDDEN):
            return await expired()
        SET.set("hidden", [r for r in SET["hidden"] if r != HIDDEN[i].root])
        await q.answer(t("hd.shown"))
        await sync_catalog(app, force=True)
        await edit(q, hidden_view() if HIDDEN else settings_view(uid))

    # ── multi-select (the basket) ──
    elif cmd == "sx":
        res = resolved(want_dir=True)
        if not res:
            return await expired()
        ud["selmode"] = not ud.get("selmode")
        await q.answer(t("sel.on") if ud["selmode"] else t("sel.off"))
        await edit(q, await open_dir(res[0], res[1], page_arg(1), ud))

    elif cmd == "t":
        item, folder = resolved(0), resolved(1, want_dir=True)
        if not item or not folder:
            return await expired()
        basket = basket_of(ud)
        path = item[1]
        if path in basket:
            del basket[path]
            await q.answer(t("bq.removed"))
        elif not selectable(path):
            return await q.answer(t("bq.cannot"), show_alert=True)
        elif len(basket) >= MAX_BASKET:
            return await q.answer(t("bq.full", n=MAX_BASKET), show_alert=True)
        else:
            basket[path] = item[0].key
            await q.answer(t("bq.added", n=len(basket)))
        await edit(q, await open_dir(folder[0], folder[1], page_arg(2), ud))

    elif cmd in ("ta", "tn"):
        folder = resolved(want_dir=True)
        if not folder:
            return await expired()
        p, path = folder
        page = page_arg(1)
        basket = basket_of(ud)
        try:
            listing = await asyncio.to_thread(list_dir, p, path)
            chunk = listing[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        except OSError:
            return await expired()
        for e in chunk:
            full = os.path.join(path, e.name)
            if cmd == "tn":
                basket.pop(full, None)
            elif e.name in SKIP_DIRS or os.path.isfile(os.path.join(full, "pyvenv.cfg")):
                continue  # "this page" leaves out virtualenvs and caches; tick them by hand
            elif selectable(full) and len(basket) < MAX_BASKET:
                basket[full] = p.key
        await q.answer(t("bq.count", n=len(basket)))
        await edit(q, await open_dir(p, path, page, ud))

    elif cmd == "af":
        res = resolved(want_dir=False)
        if not res:
            return await expired()
        basket = basket_of(ud)
        if res[1] in basket:
            del basket[res[1]]
            await q.answer(t("bq.removed"))
        elif len(basket) >= MAX_BASKET or not selectable(res[1]):
            return await q.answer(t("bq.cannot"), show_alert=True)
        else:
            basket[res[1]] = res[0].key
            await q.answer(t("bq.added", n=len(basket)))
        await edit(q, file_view(res[0], res[1], ud))

    elif cmd == "pin":
        res = resolved(want_dir=False)
        if not res or STORE is None:
            return await expired()
        p, path = res
        on = await asyncio.to_thread(STORE.toggle_pin, p.root, os.path.relpath(path, p.root))
        _ESS_CACHE.pop(p.root, None)
        await q.answer(t("file.pin_on") if on else t("file.pin_off"))
        await edit(q, file_view(p, path, ud))

    elif cmd == "bq":
        await q.answer()
        await edit(q, basket_view(ud))

    elif cmd == "bqc":
        basket_of(ud).clear()
        await q.answer(t("bq.cleared"))
        await edit(q, basket_view(ud))

    elif cmd in ("bqz", "bqs"):
        items = basket_items(ud)
        if not items:
            return await q.answer(t("bq.empty"), show_alert=True)
        audit(update, "basket-zip" if cmd == "bqz" else "basket-send", f"{len(items)} items")

        async def work(job: Job) -> None:
            if cmd == "bqz":
                pairs: Pairs = []
                for p, path in items:
                    if os.path.isdir(path):
                        pairs += await asyncio.to_thread(pairs_tree, path, _arc(p, path), p.flat)
                    else:
                        pairs.append((path, _arc(p, path)))
                await zip_and_send(job, bot, chat_id, title=t("bq.title"),
                                   label=t("bq.count", n=len(items)), slug="selection",
                                   pairs=pairs)
            else:
                todo: list[tuple[str, object]] = []
                for p, path in items:
                    if os.path.isdir(path):
                        name = os.path.basename(path.rstrip("/")) or p.key
                        tree = await asyncio.to_thread(pairs_tree, path, name, p.flat)
                        todo.append(("zip", dict(
                            title=f"{p.title} / {name}", label=t("zip.l_dir"),
                            slug=re.sub(r"\W+", "_", f"{p.key}_{name}"), pairs=tree)))
                    else:
                        todo.append(("file", path))
                await send_many(job, bot, chat_id, todo)
            basket_of(ud).clear()  # only once everything went through

        await run_job(q, context, t("job.basket"), work)

    # ── download / preview ──
    elif cmd == "dl":
        res = resolved(want_dir=False)
        if not res:
            return await expired()
        path = res[1]
        name = os.path.basename(path)
        try:
            big = os.path.getsize(path) > TG_PART_SIZE
        except OSError:
            return await expired()
        audit(update, "download", path)
        if big:
            async def work(job: Job) -> None:
                job.stage = name
                await send_file(bot, chat_id, path, job)

            await run_job(q, context, t("job.download", name=name), work)
        else:
            await q.answer(t("sending"))
            try:
                await send_file(bot, chat_id, path)
            except Exception:
                log.exception("download failed")
                await bot.send_message(chat_id, t("send.failed"))

    elif cmd == "v":
        res = resolved(want_dir=False)
        if not res:
            return await expired()
        await q.answer()
        audit(update, "view", res[1])
        try:
            preview = await asyncio.to_thread(read_preview, res[1])
        except OSError as e:
            await bot.send_message(chat_id, f"❌ <code>{esc(e)}</code>")
            return
        if preview is None:
            await bot.send_message(chat_id, t("file.binary"))
            return
        cut = len(preview) > PREVIEW_CHARS
        body = preview[:PREVIEW_CHARS] + ("\n…" if cut else "")
        await bot.send_message(
            chat_id, f"👁 <b>{esc(short(os.path.basename(res[1]), 80))}</b>\n<pre>{esc(body)}</pre>")

    # ── zip a folder from the file browser ──
    elif cmd == "bd":
        res = resolved(want_dir=True)
        if not res:
            return await expired()
        p, path = res
        name = os.path.basename(path.rstrip("/")) or p.key
        audit(update, "zip-dir", path)

        async def work(job: Job) -> None:
            pairs = await asyncio.to_thread(pairs_tree, path, name, p.flat)
            await zip_and_send(job, bot, chat_id, title=f"{p.title} / {name}",
                               label=t("zip.l_dir"),
                               slug=re.sub(r"\W+", "_", f"{p.key}_{name}"), pairs=pairs)

        await run_job(q, context, t("job.zip_dir", name=name), work)

    # ── backup ──
    elif cmd == "bk":
        await q.answer()
        await sync_catalog(app, max_age=FRESH_AGE)
        await edit(q, backup_menu_view(page_arg(0)))

    elif cmd == "b":
        p = project_arg()
        if not p:
            return await expired()
        await q.answer()
        files = await asyncio.to_thread(essentials, p, True)
        await edit(q, backup_project_view(p, len(files)))

    elif cmd in ("bf", "bi"):
        p = project_arg()
        if not p:
            return await expired()
        full = cmd == "bf"
        audit(update, "backup-full" if full else "backup-essentials", p.key)

        async def work(job: Job) -> None:
            if full:
                pairs = await asyncio.to_thread(pairs_tree, p.root, p.key, p.flat)
            else:
                pairs = pairs_files(p, await asyncio.to_thread(essentials, p, True), p.key)
            await zip_and_send(job, bot, chat_id, title=p.title,
                               label=t("zip.l_full") if full else t("zip.l_ess"),
                               slug=f"{p.key}_{'full' if full else 'essentials'}", pairs=pairs)

        await run_job(q, context, t("job.backup", title=p.title), work)

    elif cmd == "ba":
        audit(update, "backup-essentials", "all")
        chosen = list(PROJECTS)

        async def work(job: Job) -> None:
            pairs: Pairs = []
            for p in chosen:
                pairs += pairs_files(p, await asyncio.to_thread(essentials, p, True), p.key)
            await zip_and_send(job, bot, chat_id, title=t("zip.t_all"), label=t("zip.l_ess"),
                               slug="all_essentials", pairs=pairs)

        await run_job(q, context, t("job.backup_all"), work)

    elif cmd in ("bs", "bt", "bsa", "bsn"):
        p = project_arg()
        if not p:
            return await expired()
        files = await asyncio.to_thread(essentials, p)
        sel = ud.setdefault("sel", {}).setdefault(p.key, set())
        count = min(len(files), MAX_SELECT_FILES)
        if cmd == "bt":
            sel.symmetric_difference_update({page_arg(1)})
        elif cmd == "bsa":
            sel.update(range(count))
        elif cmd == "bsn":
            sel.clear()
        await q.answer()
        await edit(q, select_view(p, files, sel))

    elif cmd in ("bg", "bgs"):
        p = project_arg()
        if not p:
            return await expired()
        imp = (await asyncio.to_thread(essentials, p))[:MAX_SELECT_FILES]
        sel = ud.get("sel", {}).get(p.key, set())
        rels = [imp[i] for i in sorted(sel) if i < len(imp)]
        if not rels:
            return await q.answer(t("sel.nothing"), show_alert=True)
        audit(update, "backup-custom", f"{p.key} ({len(rels)} files)")

        async def work(job: Job) -> None:
            pairs = pairs_files(p, rels, p.key)
            if cmd == "bg":
                await zip_and_send(job, bot, chat_id, title=p.title,
                                   label=t("zip.l_pick", n=len(rels)),
                                   slug=f"{p.key}_custom", pairs=pairs)
            else:
                await send_many(job, bot, chat_id, [("file", src) for src, _ in pairs])

        await run_job(q, context, t("job.pick", title=p.title), work)

    # ── several projects at once ──
    elif cmd in ("mp", "mt", "ma", "mn"):
        sel = ud.setdefault("msel", set())
        page = page_arg(0)
        if cmd == "mt":
            page = page_arg(1)
            if args and args[0] in PMAP:
                sel.symmetric_difference_update({args[0]})
        elif cmd == "ma":
            sel.update(PMAP)
        elif cmd == "mn":
            sel.clear()
        await q.answer()
        await edit(q, multi_project_view(ud, page))

    elif cmd == "mg":
        chosen = [p for p in PROJECTS if p.key in ud.get("msel", set())]
        if not chosen or len(args) < 2:
            return await q.answer(t("mp.nothing"), show_alert=True)
        full, one_zip = args[0] == "f", args[1] == "z"
        kind = "full" if full else "essentials"
        label = t("zip.l_full") if full else t("zip.l_ess")
        audit(update, "backup-multi", ",".join(p.key for p in chosen) + " " + kind)

        async def work(job: Job) -> None:
            per: list[tuple[Project, Pairs]] = []
            for p in chosen:
                if full:
                    per.append((p, await asyncio.to_thread(pairs_tree, p.root, p.key, p.flat)))
                else:
                    per.append((p, pairs_files(
                        p, await asyncio.to_thread(essentials, p, True), p.key)))
            if one_zip:
                await zip_and_send(job, bot, chat_id, title=t("zip.t_n", n=len(chosen)),
                                   label=label, slug=f"projects_{kind}",
                                   pairs=[x for _, pairs in per for x in pairs])
            else:
                await send_many(job, bot, chat_id, [
                    ("zip", dict(title=p.title, label=label, slug=f"{p.key}_{kind}", pairs=pairs))
                    for p, pairs in per])

        await run_job(q, context, t("job.backup_n", n=len(chosen)), work)

    # ── cancel a job ──
    elif cmd == "cx":
        if JOB is None:
            await q.answer(t("job.none"))
        else:
            JOB.cancel.set()
            await q.answer(t("job.cancelling"))

    # ── status, services, logs ──
    elif cmd == "st":
        await q.answer()
        await sync_catalog(app, max_age=FRESH_AGE)
        await edit(q, await status_view())

    elif cmd == "sl":
        await q.answer()
        await sync_catalog(app, max_age=FRESH_AGE)
        await edit(q, await services_view(page_arg(0)))

    elif cmd == "sv":
        s = service_arg()
        if not s:
            return await expired()
        await q.answer()
        await edit(q, await service_view(s))

    elif cmd == "mu":
        s = service_arg()
        if not s:
            return await expired()
        muted = set(SET["muted"])
        muted.symmetric_difference_update({s.sid})
        SET.set("muted", sorted(muted))
        await q.answer(t("sv.mute_on") if s.sid in muted else t("sv.mute_off"))
        await edit(q, await service_view(s))

    elif cmd == "uf":
        s = service_arg()
        if not s or s.kind != "unit":
            return await expired()
        await q.answer()
        info = (await read_units((s.sid,)) or {}).get(s.sid, {})
        path = info.get("FragmentPath", "")
        if not path or not os.path.isfile(path):
            await bot.send_message(chat_id, t("sv.no_unitfile"))
            return
        audit(update, "unit-file", path)
        body = (await asyncio.to_thread(read_preview, path) or "")[:PREVIEW_CHARS]
        await bot.send_message(chat_id, f"📄 <code>{esc(path)}</code>\n<pre>{esc(body)}</pre>")

    elif cmd in ("lv", "le", "lf"):
        s = service_arg()
        if not s:
            return await expired()
        if cmd != "lf":
            await q.answer()
            await edit(q, await log_view(s, errors_only=cmd == "le"))
            return
        await q.answer(t("preparing"))
        audit(update, "log-file", s.sid)
        raw = await fetch_log(s, LOG_FILE_LINES, iso=True)
        if not raw:
            await bot.send_message(chat_id, t("lg.none"))
            return
        when = datetime.now(TZ).strftime("%Y-%m-%d_%H-%M")
        fname = re.sub(r"[^\w.@-]+", "_", s.label) + f"_{when}.log.txt"
        await bot.send_document(  # straight from memory; nothing is written to disk
            chat_id, InputFile(raw.encode("utf-8", "replace"), filename=fname),
            caption=aligned(f"📜 <b>{esc(s.label)}</b> — " + t("lg.file", n=raw.count("\n") + 1)),
            read_timeout=120, write_timeout=300)

    # ── disk and processes ──
    elif cmd == "dk":
        await q.answer()
        await edit(q, await disk_view())

    elif cmd in ("du", "dur"):
        fresh = _USAGE is not None and time.time() - _USAGE.at < USAGE_CACHE
        if cmd == "du" and fresh:
            await q.answer()
            return await edit(q, usage_view(_USAGE))
        if USAGE_LOCK.locked():
            return await q.answer(t("du.busy"), show_alert=True)
        await q.answer(t("du.wait", secs=int(USAGE_SECONDS)))
        audit(update, "disk-usage")
        async with USAGE_LOCK:
            await sync_catalog(app, max_age=FRESH_AGE)
            with suppress(BadRequest):
                await q.edit_message_text(t("du.working", secs=int(USAGE_SECONDS)))
            _USAGE = await asyncio.to_thread(measure_usage, PROJECTS)
        await edit(q, usage_view(_USAGE))

    elif cmd == "di":
        if Image is None:
            return await q.answer(t("rp.no_pillow_short"), show_alert=True)
        await q.answer(t("preparing"))
        mounts = await MON.get_mounts(max_age=5) if MON is not None else read_mounts()
        usage = _USAGE if _USAGE is not None and time.time() - _USAGE.at < USAGE_CACHE else None
        try:
            png = await asyncio.to_thread(render_disk, mounts, usage.rows if usage else None,
                                          bool(usage and usage.partial))
        except Exception:
            log.exception("disk chart failed")
            return await bot.send_message(chat_id, t("rp.chart_failed"))
        root = mounts[0]
        await bot.send_photo(chat_id, InputFile(png, filename="disk.png"),
                             caption=t("dk.caption", path=esc(root.path), pct=pct(root.pct),
                                       free=human(root.free)),
                             read_timeout=120, write_timeout=300)

    elif cmd == "tp":
        await q.answer(t("tp.wait"))
        await edit(q, await procs_view())

    # ── reports, events ──
    elif cmd == "rp":
        await q.answer()
        await edit(q, report_menu_view())

    elif cmd == "rg":
        await q.answer(t("rp.building"))
        await run_report(update, bot, chat_id, page_arg(0) or 7)

    elif cmd == "ev":
        await q.answer()
        await edit(q, events_view())

    # ── settings ──
    elif cmd == "se":
        await q.answer()
        await edit(q, settings_view(uid))

    elif cmd == "set":
        what = args[0] if args else ""
        if what == "lang":
            new = "en" if LANG.get() == "fa" else "fa"
            SET.set("langs", {**SET["langs"], str(uid): new})
            LANG.set(new)
        elif what == "alerts":
            SET.set("alerts", not SET["alerts"])
        elif what == "disk":
            SET.set("disk_pct", _next(DISK_STEPS, SET["disk_pct"]))
        elif what == "mem":
            SET.set("mem_pct", _next(MEM_STEPS, SET["mem_pct"]))
        elif what == "report":
            SET.set("report", _next(REPORT_STEPS, SET["report"]))
            if MON is not None:
                MON.report_last = None
                await asyncio.to_thread(STORE.set_meta, "report_last", "0")
        elif what == "wd":
            SET.set("report_wd", (int(SET["report_wd"]) + 1) % 7)
        elif what == "hour":
            step = -1 if len(args) > 1 and args[1] == "-1" else 1
            SET.set("report_hour", (int(SET["report_hour"]) + step) % 24)
        elif what == "week":
            SET.set("week_start", _next(WEEK_STARTS, SET["week_start"]))
        await q.answer(t("saved"))
        await edit(q, settings_view(uid))

    # ── search ──
    elif cmd == "sq":
        await q.answer()
        await edit(q, search_prompt_view())

    elif cmd == "sr":
        if not ud.get("search"):
            return await expired()
        await q.answer()
        await edit(q, search_view(ud, page_arg(0)))

    elif cmd in ("sc", "sn"):
        query = (ud.get("search") or {}).get("q", "")
        if not query:
            return await expired()
        await q.answer(t("sq.wait_short"))
        audit(update, "search-content" if cmd == "sc" else "search", query[:60])
        await do_search(q.message, context, query, content=cmd == "sc")

    else:
        await q.answer()


def _next(steps: tuple, current):
    """The value after `current` in a cycle (the first one when it is not in the list)."""
    return steps[(steps.index(current) + 1) % len(steps)] if current in steps else steps[0]


_TROUBLE_AT: dict[str, float] = {}


def _first_in(kind: str, seconds: float) -> bool:
    """True at most once per `seconds` for each kind of trouble: the log stays readable."""
    now = time.monotonic()
    if kind in _TROUBLE_AT and now - _TROUBLE_AT[kind] < seconds:
        return False
    _TROUBLE_AT[kind] = now
    return True


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, Conflict):
        # somebody else reads this bot's updates: a second copy of it, or another server
        if _first_in("conflict", 600):
            log.error("another program is using this bot token (a second copy of this bot, or "
                      "a bot on another server). Every server needs a token of its own.")
        if ADMIN_IDS and _first_in("conflict-told", 6 * 3600):
            with suppress(Exception):
                await notify_admins(context.bot, [("al.conflict", {})])
        return
    if update is None and isinstance(err, (NetworkError, RetryAfter)):
        # a hiccup while waiting for updates; the library tries again by itself
        if _first_in("network", 300):
            log.warning("connection to Telegram interrupted (%s: %s) — retrying",
                        type(err).__name__, err)
        return
    log.error("unhandled error", exc_info=err)
    if isinstance(update, Update):      # the admin pressed something: do not leave them waiting
        with suppress(Exception):
            if update.callback_query:
                await update.callback_query.answer(t("err.generic"), show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text(t("err.generic"))


async def post_init(app: Application) -> None:
    global STORE, MON, HOST
    HOST = detect_host()
    await refine_virt(HOST)
    with suppress(OSError):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        os.chmod(DATA_DIR, 0o700)
    clean_workdir()  # anything left over from a previous run is removed
    for folder, loss in ((DATA_DIR, "the monitoring history is kept in memory only"),
                         (WORK_DIR, "zip backups cannot be built")):
        if not os.access(folder, os.W_OK | os.X_OK):
            log.warning("%s cannot be written: %s (running install.sh again repairs it)",
                        folder, loss)
    STORE = await asyncio.to_thread(Store, DB_PATH)
    SET.load()
    await asyncio.to_thread(STORE.prune, time.time())
    for lang, code in (("en", None), ("fa", "fa")):
        token = LANG.set(lang)
        with suppress(Exception):
            await app.bot.set_my_commands(
                [BotCommand(c, t("cmd." + c)) for c in COMMANDS], language_code=code)
        LANG.reset(token)
    await sync_catalog(app, force=True)  # what is on this server right now
    MON = Monitor(app, STORE)
    with suppress(Exception):
        await MON.snapshot()             # first reading, so the menus have states at once
    app.bot_data["monitor_task"] = asyncio.create_task(MON.run())
    if CERTS:
        spawn(MON.certs_task())
    me = await app.bot.get_me()
    log.info("%s %s started as @%s · %s · %s · admins=%d · projects=%d services=%d · "
             "charts=%s · big files=%s · data=%s/%s",
             APP_NAME, __version__, me.username, HOST.os, host_line(HOST), len(ADMIN_IDS),
             len(PROJECTS), len(SERVICES), "on" if Image else "off (install pillow)",
             BIG.status(), human(data_dir_bytes()), human(DATA_MAX_BYTES))
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS is empty: the bot only tells people their own Telegram id. "
                    "Put your id in .env and restart.")
    if not HOST.systemd:
        log.warning("systemd was not found: services are limited to containers and processes")


async def post_stop(app: Application) -> None:
    task = app.bot_data.get("monitor_task")
    if task:
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task
    with suppress(Exception):
        await BIG.close()
    if STORE is not None:
        await asyncio.to_thread(STORE.close)
    clean_workdir()


def make_builder():
    builder = (
        Application.builder()
        .token(BOT_TOKEN)
        .defaults(Defaults(parse_mode=ParseMode.HTML,
                           link_preview_options=LinkPreviewOptions(is_disabled=True)))
        .concurrent_updates(True)
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(600)
        .media_write_timeout(900)
        .pool_timeout(30)
        .post_init(post_init)
        .post_stop(post_stop)
    )
    if BOT_API_URL:  # your own Bot API server (it can also raise the 50 MB limit)
        builder = builder.base_url(f"{BOT_API_URL}/bot").base_file_url(f"{BOT_API_URL}/file/bot")
    if PROXY_URL:
        builder = builder.proxy(PROXY_URL).get_updates_proxy(PROXY_URL)
    return builder


def register(app: Application) -> None:
    app.add_handler(TypeHandler(Update, gate), group=-1)
    app.add_handler(CommandHandler(["start", "menu"], cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("services", cmd_services))
    app.add_handler(CommandHandler("disk", cmd_disk))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler(["find", "search"], cmd_find))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CommandHandler("events", cmd_events))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(router))
    app.add_error_handler(on_error)


def main() -> None:
    if not BOT_TOKEN:
        log.error("BOT_TOKEN is missing. Copy .env.example to .env, fill it in and start again "
                  "(see README.md).")
        sys.exit(EX_CONFIG)
    os.umask(0o077)  # every file the bot creates is readable by its own user only
    be_gentle()
    try:
        app = make_builder().build()
        register(app)
        app.run_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query"])
    except InvalidToken:
        # (the library's own message would print the token into the log)
        log.error("Telegram does not accept BOT_TOKEN. Copy it again from @BotFather into .env "
                  "and start the bot again.")
        sys.exit(EX_CONFIG)
    except NetworkError as e:
        log.error("Telegram cannot be reached (%s: %s). If this server needs a proxy, set "
                  "PROXY_URL in .env. Trying again shortly.", type(e).__name__, e)
        sys.exit(1)


if __name__ == "__main__":
    main()

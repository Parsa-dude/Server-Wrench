#!/usr/bin/env python3
"""Regenerate the sample images used in the README.

The pictures are drawn by the bot's own chart code (bot.build_report, bot.render_report,
bot.render_disk) from made-up monitoring data for an imaginary "demo-server" — no real
server was involved, and nothing here talks to Telegram.

    python docs/make_samples.py          # writes docs/report-1..3.png and docs/disk.png

It also prints the caption the bot would send under the report, in English and Persian.
"""
import math
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DOCS = Path(__file__).resolve().parent
os.environ["TIMEZONE"] = "UTC"
os.environ["WRENCH_ENV_FILE"] = os.devnull      # never read a real configuration
sys.path.insert(0, str(DOCS.parent))
import bot  # noqa: E402

if bot.Image is None:
    sys.exit("The charts need Pillow:  pip install pillow")

random.seed(11)
DAYS = 7
T1 = int(datetime(2026, 10, 5, tzinfo=timezone.utc).timestamp())    # a Monday, 00:00
T0 = T1 - DAYS * 86400
real_time = time.time
time.time = lambda: T1 + 9 * 3600      # the "now" printed in the image headers: Monday 09:00

bot.HOST.hostname = "demo-server"
bot.STORE = bot.Store(":memory:")

SERVICES = [
    # id, kind, label, group, average RAM (KiB), CPU (% of one core)
    ("shop-api", "unit", "shop-api", "app", 214_000, 6.4),
    ("telegram-bot", "unit", "telegram-bot", "app", 86_000, 0.8),
    ("worker", "unit", "worker", "app", 143_000, 3.1),
    ("nginx", "unit", "nginx", "sys", 18_500, 0.6),
    ("postgresql", "unit", "postgresql", "sys", 388_000, 2.2),
    ("redis-server", "unit", "redis-server", "sys", 22_400, 0.3),
    ("d/shop-web", "docker", "shop-web", "docker", 161_000, 4.0),
    ("p/scraper", "proc", "scraper", "proc", 61_300, 1.5),
]
bot.SERVICES = tuple(bot.Svc(sid, kind, label, group) for sid, kind, label, group, _, _ in SERVICES)
bot.SMAP = {s.sid: s for s in bot.SERVICES}


def minute(day: int, hh: int, mm: int) -> int:
    """Minutes from the start of the report to that moment of October 2026 (UTC)."""
    return (int(datetime(2026, 10, day, hh, mm, tzinfo=timezone.utc).timestamp()) - T0) // 60


def busy(hour: float) -> float:
    """How busy the imaginary server is at an hour of the day: quiet nights, a lunch bump,
    a long evening peak."""
    evening = math.exp(-((hour - 20.5) ** 2) / 9) + math.exp(-((hour + 3.5) ** 2) / 9)
    return evening + 0.45 * math.exp(-((hour - 12.5) ** 2) / 6)


INCIDENT = minute(2, 21, 14)           # Friday evening: a traffic spike takes two services down
CRASHES = {"worker": [minute(1, 14, 20), INCIDENT - 4], "shop-api": [INCIDENT]}

sys_rows, svc_rows = [], []
for i in range(DAYS * 1440):
    ts = T0 + i * 60
    dt = datetime.fromtimestamp(ts, timezone.utc)
    load = busy(dt.hour + dt.minute / 60) * (1.25 if dt.weekday() >= 5 else 1.0)
    cpu = 5.5 + 31 * load + random.gauss(0, 2.2)
    if random.random() < 0.004:
        cpu += random.uniform(15, 35)                       # short bursts
    if i == INCIDENT:
        cpu = 96.0
    mem = 57.5 + 4.5 * i / (DAYS * 1440) + 9 * load + random.gauss(0, 0.8)
    if INCIDENT - 14 <= i <= INCIDENT + 11:
        mem += 13
    disk = 61.2 + 1.4 * i / (DAYS * 1440)
    rx = int((0.10 + 1.15 * load) * bot.MIB * random.uniform(0.85, 1.15))
    tx = int((0.22 + 2.90 * load) * bot.MIB * random.uniform(0.85, 1.15))
    sys_rows.append((ts, round(max(1.0, min(100.0, cpu)) * 10), round((0.2 + 1.6 * load) * 100),
                     round(max(20.0, min(99.0, mem)) * 10), round((3.0 + 2.0 * load) * 10),
                     round(disk * 10), rx, tx))
    if i % 5 == 0:
        for sid, _kind, _label, _group, kib, cpu_pct in SERVICES:
            down = any(c <= i < c + 10 for c in CRASHES.get(sid, ()))
            svc_rows.append((ts, sid, 0 if down else 100,
                             None if down else int(kib * (0.9 + 0.25 * load) * random.uniform(0.97, 1.03)),
                             None if down else round(cpu_pct * (0.5 + 1.3 * load) * 10)))

with bot.STORE.db as db:
    db.executemany("INSERT OR REPLACE INTO sys VALUES(?,?,?,?,?,?,?,?)", sys_rows)
    for ts, name, up, mem, cpu in svc_rows:
        db.execute("INSERT OR REPLACE INTO svc VALUES(?,?,?,?,?)",
                   (ts, bot.STORE._sid(db, name), up, mem, cpu))
    for sid, lines, errs, warns in (("shop-api", 412_000, 37, 210), ("worker", 96_000, 112, 64),
                                    ("telegram-bot", 58_000, 4, 19), ("d/shop-web", 230_000, 9, 41)):
        db.execute("INSERT OR REPLACE INTO logstat VALUES(?,?,?,?,?)",
                   (T1 - 7200, bot.STORE._sid(db, sid), lines, errs, warns))
    for h in range(DAYS * 24):                              # website requests, hour by hour
        ts = T0 + h * 3600
        db.execute("INSERT OR REPLACE INTO logstat VALUES(?,?,?,?,?)",
                   (ts, bot.STORE._sid(db, bot.WEB_KEY), int(300 + 2100 * busy((ts // 3600) % 24)),
                    1 if h % 4 == 0 else 0, 0))
for sid, minutes in CRASHES.items():
    for n, m in enumerate(minutes, 1):
        bot.STORE.add_event("crash", sid, f"auto-restart #{n}", T0 + m * 60)
bot.STORE.add_event("restart", "nginx", "manual", T0 + minute(1, 11, 0) * 60)

report = bot.build_report(bot.STORE, T0, T1)
for n, png in enumerate(bot.render_report(report, "Weekly server report"), 1):
    (DOCS / f"report-{n}.png").write_bytes(png)

GIB = 1024 ** 3
mounts = [
    bot.Mount("/", "/dev/vda1", "ext4", 80 * GIB, int(47.3 * GIB), int(28.6 * GIB)),
    bot.Mount("/mnt/data", "/dev/vdb", "xfs", 200 * GIB, int(171.4 * GIB), int(28.6 * GIB)),
]
usage = [
    bot.UsageRow("shop", "/srv/shop", int(22.6 * GIB)),
    bot.UsageRow("docker data", "/var/lib/docker", int(9.8 * GIB)),
    bot.UsageRow("postgresql data", "/var/lib/postgresql", int(6.1 * GIB)),
    bot.UsageRow("logs (/var/log)", "/var/log", int(3.4 * GIB)),
    bot.UsageRow("scraper", "/root/scraper", int(1.9 * GIB)),
    bot.UsageRow("telegram-bot", "/root/telegram-bot", int(0.62 * GIB)),
    bot.UsageRow("package cache (/var/cache)", "/var/cache", int(0.48 * GIB)),
    bot.UsageRow("blog", "/var/www/blog", int(0.21 * GIB)),
]
(DOCS / "disk.png").write_bytes(bot.render_disk(mounts, usage))
time.time = real_time

for lang in ("en", "fa"):
    bot.LANG.set(lang)
    caption = bot.report_text(report, bot.t("rp.t_week"))
    print(f"──── caption ({lang}) · {bot.visible_len(caption)} characters ────\n{caption}\n")
print(f"written: {DOCS}/report-1.png, report-2.png, report-3.png, disk.png")

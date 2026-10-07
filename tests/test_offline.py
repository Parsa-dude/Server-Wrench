#!/usr/bin/env python3
"""Offline test-suite for Server Wrench.

No Telegram account, no network and no root are needed. A small fake server is built
in a temporary folder — project folders, systemd unit files, a /proc tree, web-server
configuration, Docker API answers — and the real bot code runs against it through
python-telegram-bot with a fake HTTP layer. Every message the bot would send is
checked: valid Telegram HTML, within the length limits, buttons within 64 bytes.

    python tests/test_offline.py
"""
import asyncio
import atexit
import io
import json
import logging
import os
import re
import shutil
import signal
import socket
import sqlite3
import string
import subprocess
import sys
import tempfile
import threading
import time
import types
import urllib.parse
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# a made-up identity, and no settings file: a real .env next to bot.py is never read
os.environ.update(BOT_TOKEN="123456:TEST-TOKEN-not-a-real-one-000000000", ADMIN_IDS="1001, 1002",
                  WRENCH_ENV_FILE=os.devnull, LANGUAGE="auto", API_ID="", API_HASH="",
                  PROXY_URL="", BOT_API_URL="", EXTRA_PATHS="", SCAN_PATHS="", IGNORE_DIRS="",
                  IGNORE_SERVICES="")
sys.path.insert(0, str(REPO))
import bot  # noqa: E402
from telegram import Update  # noqa: E402
from telegram.constants import ParseMode  # noqa: E402
from telegram.error import Conflict, NetworkError  # noqa: E402
from telegram.ext import Application, Defaults  # noqa: E402
from telegram.request import BaseRequest  # noqa: E402

T = tempfile.mkdtemp(prefix="wrench_srv_")
atexit.register(shutil.rmtree, T, ignore_errors=True)     # the fake server never outlives the run
NOW = time.time()
BTIME = int(NOW) - 5 * 86400
PASSED = 0
ADMIN, ADMIN2, STRANGER = 1001, 1002, 4242


PROBLEMS = []   # anything that went wrong inside the bot: a bad message, a logged error


class _Catch(logging.Handler):
    def emit(self, record):
        if record.levelno >= 40 or "failed" in record.getMessage():
            PROBLEMS.append(f"log: {record.getMessage()} {record.exc_info[1] if record.exc_info else ''}")


def check(cond, label):
    global PASSED
    if PROBLEMS:
        raise AssertionError(f"before «{label}»: {PROBLEMS[0]}")
    if not cond:
        raise AssertionError(label)
    PASSED += 1
    print("  ok  ", label)


def section(title):
    print(f"\n{title}")


# ════════════════════════════════════════════════════════════════
#  the fake server
# ════════════════════════════════════════════════════════════════
def mk(path, text="x"):
    p = Path(T + path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text.replace("{T}", T), encoding="utf-8")
    return str(p)


def mkd(path):
    Path(T + path).mkdir(parents=True, exist_ok=True)


def link(path, target):
    p = Path(T + path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.is_symlink():
        p.unlink()
    os.symlink(target, p)


def unit(name, body):
    mk(f"/etc/systemd/system/{name}.service", body)


PROCS = {}
JUST_STARTED = -3600


def mkproc(pid, comm, cmd, cwd="/", unit_name="", age=7200, rss_mb=50, ticks=300, box="",
           exe=None, session=True):
    """One fake process. `age` is in seconds; a negative age is a start time in the future,
    which keeps a process "just started" for the whole run, however slow the machine is."""
    base = f"/proc/{pid}"
    start = int((NOW - age - BTIME) * bot.CLK_TCK)
    fields = ["S", "1", str(pid), str(pid), "0", "-1", "4194560", "0", "0", "0", "0",
              str(ticks // 2), str(ticks - ticks // 2), "0", "0", "20", "0", "1", "0",
              str(start), "100000000", str(rss_mb * bot.MIB // bot.PAGE_BYTES)] + ["0"] * 30
    mk(base + "/stat", f"{pid} ({comm}) " + " ".join(fields) + "\n")
    Path(T + base + "/cmdline").write_bytes(
        b"\0".join(c.replace("{T}", T).encode() for c in cmd) + b"\0")
    if box:
        cg = f"0::/system.slice/docker-{box}.scope\n"
    elif unit_name:
        cg = f"0::/system.slice/{unit_name}.service\n"
    elif session:
        cg = "0::/user.slice/user-0.slice/session-7.scope\n"
    else:
        cg = "0::/init.scope\n"
    mk(base + "/cgroup", cg)
    link(base + "/cwd", cwd.replace("{T}", T))
    link(base + "/exe", (exe or cmd[0]).replace("{T}", T))
    link(base + "/root", "/")
    link(base + "/ns/pid", "pid:[4026532999]" if box else "pid:[4026531836]")
    PROCS[pid] = comm


def rmproc(pid):
    shutil.rmtree(T + f"/proc/{pid}", ignore_errors=True)
    PROCS.pop(pid, None)


def build_server():
    # ── /root: the classic "bots in /root" VPS ──
    mk("/root/atlas_bot/atlas_bot.py", "print('hello')\n" * 40)
    mk("/root/atlas_bot/.env", "API_HASH=supersecretvalue\nTOKEN=12345:abcdef\n")
    mk("/root/atlas_bot/config.json", '{"model": "x"}')
    mk("/root/atlas_bot/requirements.txt", "python-telegram-bot\n")
    mk("/root/atlas_bot/data/state.json", '{"n": 1}')
    mk("/root/atlas_bot/data/deep/a/b/c/too_deep.py", "x")
    mk("/root/atlas_bot/my_account.session", "s")
    mk("/root/atlas_bot/logs/app.log", "line\n" * 100)
    mk("/root/atlas_bot/media/cover.jpg", "j" * 3000)
    mk("/root/atlas_bot/venv/pyvenv.cfg", "home = /usr/bin\n")
    mk("/root/atlas_bot/venv/lib/site.py", "v" * 9000)
    link("/root/atlas_bot/venv/bin/python", "/usr/bin/python3")
    mk("/root/atlas_bot/__pycache__/atlas_bot.cpython-312.pyc", "c")
    db = sqlite3.connect(T + "/root/atlas_bot/data/users.sqlite3")
    db.execute("create table u(id integer, name text)")
    db.executemany("insert into u values(?, ?)", [(i, f"user{i}") for i in range(200)])
    db.commit()
    db.close()

    mk("/root/shop/docker-compose.yml", "services:\n  web:\n    image: nginx\n")
    mk("/root/shop/backend/app.py", "import flask\n")
    mk("/root/shop/frontend/package.json", "{}")
    mk("/root/bots/alpha/bot.py", "# alpha\nCHANNEL_ID = -100123\n")
    mk("/root/bots/beta/main.js", "console.log('beta')\n")
    mk("/root/bots/beta/package.json", "{}")
    mk("/root/bots/gamma/bot.py", "# gamma\n")
    mk("/root/scraper/scraper.py", "# scraper\nCHANNEL_ID = -100123\n")
    mk("/root/scraper/requirements.txt", "requests\n")
    mk("/root/notes/todo.txt", "buy milk")
    mk("/root/archive/old_site/index.html", "<h1>old</h1>")
    mk("/root/cronjob/run.py", "# job\n")
    mk("/root/my new<bot>&co/main.py", "# odd name\n")
    mk("/root/bot.py", "# loose bot\n")
    mk("/root/backup.sh", "#!/bin/sh\n")
    mk("/root/.bash_history", "secret history")
    mk("/root/.ssh/id_ed25519", "PRIVATE KEY")
    mk("/root/.cache/pip/x.py")
    mk("/root/.local/bin/uv")
    mk("/root/snap/x.py")
    mk("/root/venv/pyvenv.cfg", "home = /usr/bin\n")
    mk("/root/venv/x.py")
    # ── other homes, /opt, /usr/local, /srv, /data ──
    mk("/home/ali/api/app.py", "# api\n")
    mk("/home/ali/public_html/index.php", "<?php ?>")
    mk("/opt/tool/bin/tool", "ELF")
    mk("/opt/tool/config.yaml", "a: 1\n")
    mk("/opt/vendor_thing/package.json", "{}")
    mkd("/opt/containerd/bin")
    mk("/usr/local/x-ui/x-ui", "ELF")
    mk("/usr/local/bin/proxy", "ELF")
    mk("/mnt/big/blob.bin", "data")
    mk("/srv/app1/Dockerfile", "FROM scratch\n")
    mk("/srv/app1/main.go", "package main\n")
    mk("/srv/caddysite/index.html", "<h1>caddy</h1>")
    mk("/data/warehouse/etl/job.py", "# etl\n")
    mk("/var/www/example.com/index.html", "<h1>hi</h1>")
    mk("/var/www/example.com/style.css", "body{}")
    mk("/var/www/blog/public/index.php", "<?php ?>")
    mk("/var/www/html/index.nginx-debian.html", "default")
    mkd("/var/lib/mysql")
    mkd("/var/spool/cron")
    mk("/var/log/nginx/access.log", "")
    mk("/var/log/syslog", "s" * 5000)

    # ── units created on the server ──
    unit("atlas-bot", "[Unit]\nDescription=Assistant\n\n[Service]\nUser=root\n"
         "WorkingDirectory={T}/root/atlas_bot\nEnvironment=SECRET_KEY=topsecret\n"
         "ExecStart={T}/root/atlas_bot/venv/bin/python atlas_bot.py\nRestart=always\n\n"
         "[Install]\nWantedBy=multi-user.target\n")
    unit("alpha", "[Service]\nExecStart=/usr/bin/python3 \\\n   {T}/root/bots/alpha/bot.py --fast\n")
    unit("rootbot", "[Service]\nWorkingDirectory={T}/root\nExecStart=/usr/bin/python3 {T}/root/bot.py\n")
    unit("uv", "[Service]\nExecStart={T}/root/.local/bin/uv run {T}/home/ali/api/app.py\n")
    unit("tool", "[Service]\nType=simple\nExecStart={T}/opt/tool/bin/tool -c {T}/opt/tool/config.yaml\n")
    unit("x-ui", "[Service]\nWorkingDirectory={T}/usr/local/x-ui/\nExecStart={T}/usr/local/x-ui/x-ui\n")
    unit("proxy", "[Service]\nExecStart=-{T}/usr/local/bin/proxy -c {T}/etc/proxy.json --data {T}/mnt/big\n")
    unit("cron-job", "[Service]\nType=oneshot\nWorkingDirectory={T}/root/cronjob\n"
         "ExecStart=/usr/bin/python3 run.py\n")
    unit("snap.lxd.daemon", "[Service]\nExecStart=/usr/bin/snap run lxd\n")
    unit("tmpl@", "[Service]\nExecStart=/bin/foo %i\n")
    link("/etc/systemd/system/sshd.service", "/lib/systemd/system/ssh.service")
    link("/etc/systemd/system/multi-user.target.wants/nginx.service", "/lib/systemd/system/nginx.service")
    link("/etc/systemd/system/multi-user.target.wants/wg-quick@wg0.service",
         "/lib/systemd/system/wg-quick@.service")
    for name in ("nginx", "mysql", "docker", "ssh", "cron", "fail2ban", "apache2", "postgresql",
                 "redis-server", "systemd-journald", "getty@", "unknown-daemon", "pm2-root"):
        mk(f"/usr/lib/systemd/system/{name}.service", "[Service]\nExecStart=/usr/sbin/x\n")

    # ── web servers ──
    mk("/etc/nginx/nginx.conf", "http { include sites-enabled/*; }\n")
    mk("/etc/nginx/sites-enabled/example",
       "server {\n listen 80;\n server_name example.com www.example.com;\n root {T}/var/www/example.com;\n}\n"
       "server {\n listen 443 ssl; # tls\n server_name example.com www.example.com;\n"
       " root {T}/var/www/example.com;\n ssl_certificate {T}/etc/letsencrypt/live/example.com/fullchain.pem;\n}\n")
    mk("/etc/nginx/sites-enabled/blog",
       "upstream u { server 127.0.0.1:9; }\nserver {\n server_name blog.example.com;\n"
       " root \"{T}/var/www/blog/public\";\n}\n"
       "server {\n server_name api.example.com;\n location / { proxy_pass http://127.0.0.1:3000; }\n}\n"
       "server {\n server_name _;\n root {T}/var/www/html;\n}\n")
    mk("/etc/apache2/sites-enabled/ali.conf",
       "# comment\n<VirtualHost *:80>\n  ServerName ali.example.net\n  ServerAlias www.ali.example.net\n"
       "  DocumentRoot \"{T}/home/ali/public_html\"\n</VirtualHost>\n")
    mk("/etc/caddy/Caddyfile",
       "{\n  email a@b.c\n}\n\ncaddy.example.org, www.caddy.example.org {\n  root * {T}/srv/caddysite\n"
       "  file_server\n  handle /api/* {\n    reverse_proxy localhost:1\n  }\n}\n")

    # ── the machine itself ──
    mk("/etc/os-release", 'NAME="Ubuntu"\nPRETTY_NAME="Ubuntu 24.04.1 LTS"\nID=ubuntu\nVERSION_ID="24.04"\n')
    mk("/sys/class/dmi/id/sys_vendor", "Hetzner\n")
    mk("/sys/class/dmi/id/product_name", "vServer\n")
    mk("/sys/class/dmi/id/bios_vendor", "Hetzner\n")
    mk("/proc/stat", "cpu  1000 0 500 8000 100 0 0 0 0 0\ncpu0 1 1 1 1\n" + f"btime {BTIME}\n")
    mk("/proc/meminfo", "MemTotal:        4000000 kB\nMemFree:          500000 kB\n"
       "MemAvailable:    2500000 kB\nCached: 1000 kB\nSwapTotal:       2000000 kB\nSwapFree:        1900000 kB\n")
    mk("/proc/net/dev", "Inter-|   Receive\n face |bytes\n    lo: 500 0 0 0 0 0 0 0 500 0 0 0 0 0 0 0\n"
       "  eth0: 1000000 10 0 0 0 0 0 0 2000000 20 0 0 0 0 0 0\ndocker0: 9 0 0 0 0 0 0 0 9 0 0 0 0 0 0 0\n")
    mk("/proc/cpuinfo", "processor : 0\nmodel name : Intel Xeon (Skylake)\nflags : fpu hypervisor\n")
    mk("/proc/uptime", "432000.00 1.0\n")
    mk("/proc/mounts", "/dev/vda1 / ext4 rw 0 0\nproc /proc proc rw 0 0\ntmpfs /run tmpfs rw 0 0\n")
    link("/proc/1/ns/pid", "pid:[4026531836]")

    # ── processes ──
    mkproc(1, "systemd", ["/sbin/init"], session=False, age=5 * 86400)
    mkproc(300, "nginx", ["nginx: master process /usr/sbin/nginx"], unit_name="nginx", exe="/usr/sbin/nginx")
    mkproc(400, "python", ["{T}/root/atlas_bot/venv/bin/python", "atlas_bot.py"],
           "{T}/root/atlas_bot", "atlas-bot", rss_mb=84, ticks=5000)
    mkproc(410, "python3", ["/usr/bin/python3", "{T}/root/bots/alpha/bot.py", "--fast"], "/", "alpha")
    mkproc(415, "python3", ["/usr/bin/python3", "{T}/root/bot.py"], "{T}/root", "rootbot")
    mkproc(500, "PM2 v5.3.0: God", ["PM2 v5.3.0: God Daemon (/root/.pm2)"], "{T}/root/bots/beta",
           "pm2-root", exe="/usr/bin/node")
    mkproc(510, "node", ["node", "{T}/root/bots/beta/main.js"], "{T}/root/bots/beta", "pm2-root",
           exe="/usr/bin/node", rss_mb=120)
    mkproc(600, "python3", ["python3", "scraper.py"], "{T}/root/scraper", age=2 * 86400,
           exe="/usr/bin/python3.12", rss_mb=60, ticks=90000)
    mkproc(610, "bash", ["-bash"], "{T}/root/scraper", exe="/usr/bin/bash")
    mkproc(620, "python3", ["python3", "tmp.py"], "{T}/root/notes", age=JUST_STARTED, exe="/usr/bin/python3")
    mkproc(700, "python", ["python", "app.py"], "/app", box="c" * 64, exe="/usr/local/bin/python", rss_mb=70)
    mkproc(800, "mysqld", ["/usr/sbin/mysqld"], "{T}/var/lib/mysql", "mysql")
    mkproc(820, "x-ui", ["{T}/usr/local/x-ui/x-ui"], "{T}/usr/local/x-ui", "x-ui")
    mkproc(830, "cron", ["/usr/sbin/cron", "-f"], "{T}/var/spool/cron", "cron")
    mkproc(831, "python3", ["python3", "{T}/root/cronjob/run.py"], "{T}/root", "cron", age=600,
           exe="/usr/bin/python3")
    mkproc(832, "python3", ["/usr/bin/python3", "{T}/root/bots/gamma/bot.py"], "{T}/root", "cron",
           age=2 * 86400)
    mkproc(840, "python3", ["/usr/bin/python3", "app.py"], "{T}/home/ali/api", "uv")


def retarget():
    """Point every path the bot reads at the fake server."""
    pre = lambda p: T + p  # noqa: E731
    real_system = tuple(p for p in bot.SYSTEM_PREFIXES if not (T + "/").startswith(p + "/"))
    for name in ("HOME_BASES", "APP_BASES", "WEB_BASES", "SYSTEM_PREFIXES", "PKG_UNIT_DIRS",
                 "NGINX_GLOBS", "APACHE_GLOBS", "CADDY_FILES", "WEB_LOG_GLOBS", "OS_RELEASE"):
        setattr(bot, name, tuple(pre(p) for p in getattr(bot, name)))
    bot.SYSTEM_PREFIXES += real_system   # the real /usr/bin etc. stay "system" too
    bot.STOP_DIRS = tuple(T if p == "/" else pre(p) for p in bot.STOP_DIRS)
    bot.CONFIG_PLACES = tuple((pre(p), label) for p, label in bot.CONFIG_PLACES)
    bot.USAGE_PLACES = tuple((pre(p), label) for p, label in bot.USAGE_PLACES)
    for name in ("PROC", "SYSTEMD_RUN", "DMI_DIR", "UNIT_DIR", "CGROUP_ROOT", "LETSENCRYPT_LIVE"):
        setattr(bot, name, pre(getattr(bot, name)))
    bot.VIRT_FILES = {k: pre(v) for k, v in bot.VIRT_FILES.items()}
    bot.BASE_DIR = Path(T + "/opt/server-wrench")
    bot.DATA_DIR = bot.BASE_DIR / "data"
    bot.WORK_DIR = bot.BASE_DIR / ".work"
    bot.DB_PATH = bot.DATA_DIR / "monitor.sqlite3"
    bot.SEND_PAUSE = 0


# ── fake systemd ──
def U(active="active", sub="running", enabled="enabled", typ="simple", result="success",
      mem="50000000", nrest="0", enter="1000000", load="loaded"):
    return {"LoadState": load, "ActiveState": active, "SubState": sub, "UnitFileState": enabled,
            "Type": typ, "Result": result, "MemoryCurrent": mem, "CPUUsageNSec": "1000000000",
            "NRestarts": nrest, "ActiveEnterTimestampMonotonic": enter}


UNITS = {
    "atlas-bot": U(mem="88000000"), "alpha": U(), "rootbot": U(), "uv": U(),
    "tool": U("failed", "failed", result="exit-code", mem="[not set]"),
    "x-ui": U(), "proxy": U("inactive", "dead", "disabled", mem="[not set]"),
    "nginx": U(mem="14000000"), "mysql": U(mem="400000000"), "docker": U(), "ssh": U(),
    "cron": U(), "pm2-root": U(),
    "fail2ban": U("inactive", "dead", "disabled"), "apache2": U("inactive", "dead", "disabled"),
    "postgresql": U("active", "exited", "enabled", typ="oneshot"),
    "redis-server": U("inactive", "dead", "enabled"),
    "wg-quick@wg0": U("active", "exited", "enabled", typ="oneshot"),
}
SYSTEMD_OK = True


async def fake_read_units(names):
    if not SYSTEMD_OK:
        return None
    out = {}
    for n in names:
        n = n[:-len(".service")] if n.endswith(".service") else n
        d = UNITS.get(n)
        if n == "sshd":
            continue  # an alias: systemd answers with Id=ssh.service
        out[n] = dict(d, Id=n + ".service", FragmentPath=T + f"/etc/systemd/system/{n}.service") \
            if d else {"Id": n + ".service", "LoadState": "not-found", "ActiveState": "inactive"}
    return out


JOURNAL = ("Oct 07 01:00:00 srv app[1]: started <ok> & fine\n"
           "Oct 07 01:00:01 srv app[1]: ERROR something broke\n"
           "Oct 07 01:00:02 srv app[1]: WARNING careful")
RUN_CALLS = []


async def fake_run_cmd(*args, **kw):
    RUN_CALLS.append(args)
    if args[:1] == ("journalctl",):
        return JOURNAL
    if args[:1] == ("systemd-detect-virt",):
        return "kvm"
    if args[:2] == ("systemctl", "list-units"):
        return "● broken-thing.service loaded failed failed Broken\ntool.service loaded failed failed Tool"
    return ""


# ── fake Docker Engine API ──
CONTAINERS = {
    "shop-web-1": {"State": {"Status": "running", "Running": True, "StartedAt": "2026-10-05T10:00:00.5Z",
                             "Health": {"Status": "healthy"}}, "RestartCount": 0,
                   "dir": "{T}/root/shop", "HostConfig": {"RestartPolicy": {"Name": "always"}}},
    "shop-db-1": {"State": {"Status": "running", "Running": True, "StartedAt": "2026-10-05T10:00:01Z"},
                  "RestartCount": 0, "dir": "{T}/root/shop",
                  "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}}},
    "watchtower": {"State": {"Status": "exited", "Running": False, "ExitCode": 1,
                             "StartedAt": "2026-10-01T10:00:00Z"}, "RestartCount": 0, "dir": "",
                   "HostConfig": {"RestartPolicy": {"Name": "no"}}},
}
DOCKER_OK = True


async def fake_docker_api(path, timeout=6.0, limit=0):
    if not DOCKER_OK:
        return 0, b""
    if path.startswith("/containers/json"):
        items = [{"Id": (name.encode().hex() * 8)[:64], "Names": ["/" + name], "Image": "img",
                  "State": c["State"]["Status"], "Status": "Up",
                  "Labels": {"com.docker.compose.project.working_dir": c["dir"].replace("{T}", T)}
                  if c["dir"] else {}} for name, c in CONTAINERS.items()]
        return 200, json.dumps(items).encode()
    m = re.match(r"/containers/([^/]+)/json", path)
    if m:
        c = CONTAINERS.get(m.group(1))
        if not c:
            return 404, b"{}"
        return 200, json.dumps(dict(c, Id=(m.group(1).encode().hex() * 8)[:64])).encode()
    m = re.match(r"/containers/([^/]+)/logs", path)
    if m:
        line = b"2026-10-07T01:00:00Z container says hello\n"
        err = b"2026-10-07T01:00:01Z ERROR container broke\n"
        frame = lambda s, b: bytes([s, 0, 0, 0]) + len(b).to_bytes(4, "big") + b  # noqa: E731
        return 200, frame(1, line) + frame(2, err)
    return 404, b"{}"


# ── fake Telegram ──
SENT = []      # every Bot API call: (method, parameters, files)
ME = {"id": 123456, "is_bot": True, "first_name": "wrench", "username": "wrench_test_bot"}
ALLOWED_TAGS = {"b", "i", "code", "pre"}


def check_html(text):
    """What Telegram's parser would reject: unknown or unbalanced tags, raw < > &."""
    stack = []
    for m in re.finditer(r"<(/?)([a-zA-Z]+)[^>]*>|<|>", text):
        if m.group(2) is None:
            raise AssertionError(f"raw < or > in: {text[max(0, m.start() - 60):m.start() + 30]!r}")
        tag = m.group(2)
        assert tag in ALLOWED_TAGS, f"tag <{tag}> in: {text[:200]!r}"
        if m.group(1):
            assert stack and stack.pop() == tag, f"unbalanced </{tag}> in: {text[:300]!r}"
        else:
            stack.append(tag)
    assert not stack, f"unclosed {stack} in: {text[:300]!r}"
    assert not re.search(r"&(?!amp;|lt;|gt;|quot;)", text), f"raw & in: {text[:200]!r}"
    assert "{T}" not in text and not re.search(r"\{[a-z_]+\}", text), f"unfilled placeholder: {text[:200]!r}"


def plain_len(text):
    import html as _html
    return len(_html.unescape(re.sub(r"<[^>]+>", "", text)).encode("utf-16-le")) // 2


class FakeRequest(BaseRequest):
    read_timeout = 5

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kw):
        name = url.rsplit("/", 1)[-1]
        params = dict(request_data.json_parameters) if request_data else {}
        files = {}
        if request_data and request_data.contains_files:
            for key, (fname, content, _mime) in request_data.multipart_data.items():
                files[fname] = content if isinstance(content, (bytes, bytearray)) else content.read()
        SENT.append((name, params, files))
        try:
            self.verify(name, params)
        except AssertionError as e:
            PROBLEMS.append(f"{name}: {e}")
            raise
        if name == "getMe":
            res = ME
        elif name in ("sendMessage", "editMessageText", "sendDocument", "sendPhoto"):
            res = {"message_id": len(SENT) + 10, "date": 0, "chat": {"id": ADMIN, "type": "private"}}
        elif name == "sendMediaGroup":
            res = [{"message_id": len(SENT) + 10, "date": 0, "chat": {"id": ADMIN, "type": "private"}}]
        else:
            res = True
        return 200, json.dumps({"ok": True, "result": res}).encode()


    @staticmethod
    def verify(name, params):
        """The rules Telegram enforces, checked on every single call."""
        text = params.get("text") or params.get("caption")
        if name == "answerCallbackQuery":
            assert "<" not in (text or "") and len(text or "") <= 200, f"toast: {text!r}"
        elif text:
            assert params.get("parse_mode") == "HTML", "no parse_mode"
            check_html(text)
            limit = 1024 if "caption" in params else 4096
            assert plain_len(text) <= limit, f"too long: {plain_len(text)}"
        for media in json.loads(params.get("media", "[]")):
            if media.get("caption"):
                check_html(media["caption"])
                assert plain_len(media["caption"]) <= 1024, "media caption too long"
        if params.get("reply_markup"):
            rows = json.loads(params["reply_markup"])["inline_keyboard"]
            assert len(rows) <= 100 and all(1 <= len(r) <= 8 for r in rows), "keyboard shape"
            for row in rows:
                for b in row:
                    assert len(b["callback_data"].encode()) <= 64, b["callback_data"]
                    assert b.get("style") in (None, "primary", "success", "danger"), b
                    assert 0 < len(b["text"]) <= 64, f"button label: {b['text']!r}"


UPD = [0]


def _user(uid, lang):
    return {"id": uid, "is_bot": False, "first_name": "Tester", "language_code": lang}


def cb_update(data, uid=ADMIN, lang="en"):
    UPD[0] += 1
    return {"update_id": UPD[0], "callback_query": {
        "id": str(UPD[0]), "chat_instance": "c", "data": data, "from": _user(uid, lang),
        "message": {"message_id": 7, "date": 0, "chat": {"id": uid, "type": "private"}, "text": "x"}}}


def msg_update(text, uid=ADMIN, lang="en", chat_type="private"):
    UPD[0] += 1
    ents = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}] if text.startswith("/") else []
    return {"update_id": UPD[0], "message": {
        "message_id": UPD[0], "date": 0, "text": text, "entities": ents, "from": _user(uid, lang),
        "chat": {"id": uid if chat_type == "private" else -5, "type": chat_type}}}


class Client:
    """Drives the bot the way a Telegram user would."""

    def __init__(self, app, uid=ADMIN, lang="en"):
        self.app, self.uid, self.lang = app, uid, lang
        self.calls = []

    async def _feed(self, raw):
        SENT.clear()
        await self.app.process_update(Update.de_json(raw, self.app.bot))
        self.calls = list(SENT)
        return self

    async def press(self, data):
        return await self._feed(cb_update(data, self.uid, self.lang))

    async def say(self, text, chat_type="private"):
        return await self._feed(msg_update(text, self.uid, self.lang, chat_type))

    def of(self, method):
        return [c for c in self.calls if c[0] == method]

    @property
    def screen(self):
        """Text of the last message the bot edited or sent."""
        for name, params, _ in reversed(self.calls):
            if name in ("editMessageText", "sendMessage"):
                return params["text"]
        return ""

    @property
    def buttons(self):
        for name, params, _ in reversed(self.calls):
            if name in ("editMessageText", "sendMessage") and params.get("reply_markup"):
                rows = json.loads(params["reply_markup"])["inline_keyboard"]
                return {b["text"]: b["callback_data"] for row in rows for b in row}
        return {}

    @property
    def styles(self):
        for name, params, _ in reversed(self.calls):
            if name in ("editMessageText", "sendMessage") and params.get("reply_markup"):
                rows = json.loads(params["reply_markup"])["inline_keyboard"]
                return {b["text"]: b.get("style") for row in rows for b in row}
        return {}

    @property
    def toast(self):
        got = self.of("answerCallbackQuery")
        return got[-1][1].get("text", "") if got else ""

    @property
    def docs(self):
        return [(fname, data, params.get("caption", ""))
                for name, params, files in self.calls if name == "sendDocument"
                for fname, data in files.items()]

    def find(self, part):
        """callback_data of the first button whose label contains `part`."""
        for label, data in self.buttons.items():
            if part in label:
                return data
        raise AssertionError(f"no button with {part!r} in {list(self.buttons)}")


def rel(path):
    return path[len(T):] if path.startswith(T) else path


# ════════════════════════════════════════════════════════════════
#  tests
# ════════════════════════════════════════════════════════════════
def test_static():
    section("1) texts and configuration")
    src = (REPO / "bot.py").read_text(encoding="utf-8")
    used = set(re.findall(r'''\bt\(\s*["']([a-z_.0-9]+)["']''', src))
    used |= set(re.findall(r'''["']((?:al|rp|sq|setup|gate|ev|cmd)\.[a-z_0-9]+)["']''', src))
    used = {k for k in used if not k.endswith(("_", "."))}
    check(not [k for k in used if k not in bot.TXT], "every text key used in the code exists")
    fmt = string.Formatter()
    bad = [k for k, pair in bot.TXT.items()
           if len(pair) != 2 or len({tuple(sorted({f for _, f, _, _ in fmt.parse(x) if f})) for x in pair}) != 1]
    check(not bad, f"English and Persian texts use the same placeholders {bad}")
    for lang in bot.LANGS:
        token = bot.LANG.set(lang)
        for key, pair in bot.TXT.items():
            fields = {f: "1" for _, f, _, _ in fmt.parse(pair[0]) if f}
            check_html(bot.t(key, **fields))
        bot.LANG.reset(token)
    check(True, f"all {len(bot.TXT)} texts are valid Telegram HTML in both languages")
    check(not re.search(r"\d{8,10}:[A-Za-z0-9_-]{35}", src), "no bot token in the source")
    env = Path(T + "/sample.env")
    env.write_text("# c\nexport BOT_TOKEN = \"1:a b\"\nADMIN_IDS=1, 2 # two\nEMPTY=\nBAD LINE\nX='q#z'\n")
    got = bot._read_env_file(env)
    check(got == {"BOT_TOKEN": "1:a b", "ADMIN_IDS": "1, 2", "EMPTY": "", "X": "q#z"}, ".env parser")
    check(bot.ADMIN_IDS == {ADMIN, ADMIN2}, "ADMIN_IDS parsed from the environment")
    check(bot._ENV_FILE == {}, "the test run reads no settings file")
    record = logging.LogRecord("any.library", logging.ERROR, __file__, 1, "rejected: %s (url …/bot%s/getMe)",
                               (bot.BOT_TOKEN, bot.BOT_TOKEN.replace(":", "%3A")), None)
    line = bot._log_handler.format(record)
    check(bot.BOT_TOKEN.partition(":")[2] not in line and line.count("<BOT_TOKEN>") == 2,
          "the bot token is blanked in every log line, whoever writes it")
    saved = {k: os.environ.get(k) for k in ("TIMEZONE", "TZ")}
    os.environ.update(TIMEZONE="", TZ=":Asia/Tehran")
    from_tz = bot._detect_timezone()[1]
    os.environ.update(TIMEZONE="Europe/Berlin")
    from_setting = bot._detect_timezone()[1]
    os.environ.update(TIMEZONE="Not/AZone", TZ="<+0330>-3:30")
    fallback = bot._detect_timezone()[1]
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    check(from_tz == "Asia/Tehran" and from_setting == "Europe/Berlin" and fallback not in ("", "Not/AZone"),
          "time zone: TIMEZONE, else TZ, else the server's own; a name that does not exist is skipped")
    check(bot.bar(0) == "░" * 10 and bot.bar(100) == "█" * 10 and bot.bar(52, 16).count("█") == 8, "text gauge")
    mark = bot.RLM
    check(bot.rtl_lines("⚙️ CPU: میانگین <b>5٪</b>\nسلام CPU\nnginx is up") == mark + "⚙️ CPU: میانگین <b>5٪</b>\nسلام CPU\nnginx is up",
          "Persian line that starts with a Latin word gets a right-to-left mark; other lines are untouched")
    check(bot.rtl_lines("<code>/root/shop</code> · a، b 5٪") == "<code>/root/shop</code> · a، b 5٪"
          and bot.rtl_lines("<pre>CPU ██ فارسی\nRAM فارسی</pre>") == "<pre>CPU ██ فارسی\nRAM فارسی</pre>",
          "…Persian punctuation alone does not turn a line, and <pre> blocks are never touched")
    token = bot.LANG.set("fa")
    fa_line = bot.t("rp.cpu", avg="1%", peak="2%", when="x")
    bot.LANG.reset(token)
    check(fa_line.startswith(mark) and not bot.t("rp.cpu", avg="1%", peak="2%", when="x").startswith(mark),
          "applied to Persian texts only")
    check(bot.human(1536) == "1.5 KB" and bot.human(3 * 1024 ** 4) == "3.0 TB", "sizes")
    check(bot._osa("confg", "config", 2) == 1 and bot._osa("cnofig", "config", 2) == 1
          and bot._osa("abc", "xyz", 1) == 2, "edit distance with swaps and an early exit")
    check(bot._fuzzy_word("reqirements", "requirements.txt") >= 40
          and bot._fuzzy_word("rqmts", "requirements.txt") >= 40
          and bot._fuzzy_word("zzzz", "requirements.txt") == 0, "fuzzy word matching")
    check(bot._clean_domains(["Example.com:443", "_", "*.x.y", "www.a.io", "a.io", "10.0.0.1", "https://b.dev/x"])
          == ["example.com", "a.io", "b.dev", "www.a.io"], "domain clean-up")


def test_host():
    section("2) the machine is recognised")
    h = bot.detect_host()
    check(h.os == "Ubuntu 24.04.1 LTS" and h.os_id == "ubuntu", "OS name from os-release")
    check(h.provider == "Hetzner" and h.virt == "VM", f"provider and virtualisation ({h.provider}, {h.virt})")
    check(h.cpu == "Intel Xeon (Skylake)" and h.mem_total == 4000000 * 1024, "CPU model and memory")
    for vendor, product, tag, want in (
            ("DigitalOcean", "Droplet", "", ("VM", "DigitalOcean")),
            ("Amazon EC2", "t3.micro", "", ("VM", "AWS")),
            ("Google", "Google Compute Engine", "", ("VM", "Google Cloud")),
            ("Microsoft Corporation", "Virtual Machine", bot._AZURE_TAG, ("Hyper-V", "Microsoft Azure")),
            ("QEMU", "Standard PC (i440FX + PIIX, 1996)", "", ("KVM", "")),
            ("VMware, Inc.", "VMware Virtual Platform", "", ("VMware", "")),
            ("Vultr", "VC2", "", ("VM", "Vultr")),
            ("Dell Inc.", "PowerEdge R640", "", ("", ""))):
        mk("/sys/class/dmi/id/sys_vendor", vendor)
        mk("/sys/class/dmi/id/product_name", product)
        mk("/sys/class/dmi/id/bios_vendor", "x")
        mk("/sys/class/dmi/id/chassis_asset_tag", tag)
        mk("/proc/cpuinfo", "model name : X\nflags : fpu" + ("" if vendor.startswith("Dell") else " hypervisor") + "\n")
        got = bot._detect_virt()
        check(got == want, f"{vendor} → {got}")
    mk("/run/systemd/container", "lxc\n")
    check(bot._detect_virt()[0] == "LXC", "LXC container")
    os.remove(T + "/run/systemd/container")
    mk("/sys/class/dmi/id/sys_vendor", "Hetzner")
    mk("/proc/cpuinfo", "processor : 0\nmodel name : Intel Xeon (Skylake)\nflags : fpu hypervisor\n")
    mem = bot.read_mem()
    check(mem == (1500000 * 1024, 4000000 * 1024, 100000 * 1024, 2000000 * 1024), "memory counters")
    check(bot.read_net() == (1000000, 2000000), "network counters skip lo and docker0")
    mounts = bot.read_mounts()
    check(mounts and mounts[0].path == "/" and 0 <= mounts[0].pct <= 100, "filesystems")


async def test_discovery():
    section("3) discovery: nothing is configured, everything is found")
    bot.HOST = bot.detect_host()
    bot.HOST.systemd, bot.HOST.journal, bot.HOST.docker_sock = True, True, "fake.sock"
    cat = await bot.scan_now()
    by = {rel(p.root) + ("=" if p.flat else ""): p for p in cat.projects}
    svc = {s.sid: s for s in cat.services}
    for root, p in sorted(by.items()):
        print(f"       {p.kind:5} {p.title:28} {root:28} {', '.join(p.services)}")
    print("       services:", ", ".join(f"{s.sid}[{s.group}]" for s in cat.services))

    def has(root, kind, *sids):
        p = by.get(root)
        return p is not None and p.kind == kind and set(p.services) == set(sids)

    check(has("/root/atlas_bot", "app", "atlas-bot"), "unit with WorkingDirectory → its folder")
    check(by["/root/atlas_bot"].title == "🐍 atlas_bot", "title: stack icon + folder name")
    check(has("/root/bots/alpha", "app", "alpha"), "nested folder, unit with a continued ExecStart line")
    check(has("/root/bots/beta", "app", "p/beta"), "pm2 app → found through its running process")
    check(has("/root/bots/gamma", "app", "p/gamma"), "cron @reboot program → found through its process")
    check(has("/root/scraper", "app", "p/scraper"), "nohup program → found through its process")
    check(has("/root/shop", "app", "d/shop-web-1", "d/shop-db-1") and by["/root/shop"].title.startswith("🐳"),
          "docker compose folder with its containers")
    check(has("/root=", "flat", "rootbot") and by["/root="].title == ("📄 " + T + "/root")[:48],
          "script directly in /root → 'loose files' entry with its unit")
    check(has("/home/ali/api", "app", "uv"), "hidden launcher path skipped, script path used")
    check(has("/opt/tool", "app", "tool"), "binary in bin/ → the application folder")
    check(has("/usr/local/x-ui", "app", "x-ui"), "service in /usr/local")
    check(has("/root/cronjob", "dir"), "one-shot unit: folder found, not watched")
    check(has("/srv/app1", "dir") and by["/srv/app1"].title.startswith("🐳"), "folder with a Dockerfile")
    check(has("/data/warehouse/etl", "dir"), "project two levels deep")
    check(has("/root/archive/old_site", "dir"), "old site folder")
    check(has("/root/my new<bot>&co", "dir"), "folder with an awkward name")
    check(has("/var/www/example.com", "site") and by["/var/www/example.com"].domains == ("example.com", "www.example.com"),
          "nginx site with its domains")
    check(has("/var/www/blog", "site") and by["/var/www/blog"].domains == ("blog.example.com",),
          "nginx root in a sub-folder → the site folder")
    check(has("/home/ali/public_html", "site") and "ali.example.net" in by["/home/ali/public_html"].domains,
          "Apache virtual host")
    check(has("/srv/caddysite", "site") and by["/srv/caddysite"].domains[0] == "caddy.example.org", "Caddy site")
    for bad in ("/root/notes", "/root/venv", "/root/.cache", "/root/.ssh", "/root/snap", "/root/.local",
                "/opt/vendor_thing", "/opt/containerd", "/var/www/html", "/usr/local/bin", "/root/bots",
                "/root/archive", "/data", "/srv", "/home/ali", "/root"):
        check(bad not in by, f"not a project: {bad}")
    cfg = {p.title for p in cat.projects if p.kind == "cfg"}
    check(cfg == {"⚙️ nginx", "⚙️ apache", "⚙️ caddy", "⚙️ systemd units", "⚙️ crontabs"}, f"config folders {sorted(cfg)}")

    check({s.sid for s in cat.services if s.group == "app"}
          == {"atlas-bot", "alpha", "rootbot", "uv", "tool", "x-ui", "proxy"}, "your units")
    check({s.sid for s in cat.services if s.group == "sys"}
          == {"nginx", "mysql", "docker", "ssh", "cron", "redis-server", "pm2-root"},
          "packaged services in use (unused, alias, umbrella and template units left out)")
    check({s.sid for s in cat.services if s.group == "docker"} == {"d/shop-web-1", "d/shop-db-1", "d/watchtower"},
          "containers")
    check({s.sid for s in cat.services if s.group == "proc"} == {"p/beta", "p/gamma", "p/scraper"},
          "programs without a service (shell, young process, cron job and container process ignored)")
    check("cron-job" not in svc and "snap.lxd.daemon" not in svc and "tmpl@" not in svc, "one-shot, snap, template units ignored")
    keys = [p.key for p in cat.projects]
    check(len(set(keys)) == len(keys) and all(re.fullmatch(r"[a-z0-9_]{1,20}", k) for k in keys), "keys unique and button-safe")
    check(len(cat.sites) == 5 and any(s.server == "caddy" for s in cat.sites), f"sites: {len(cat.sites)}")
    cat2 = await bot.scan_now()
    check(cat2 == cat, "a second scan gives exactly the same picture")
    started = time.perf_counter()
    for _ in range(5):
        bot.scan_server([])
    check((time.perf_counter() - started) / 5 < 0.5, f"one scan takes {(time.perf_counter() - started) / 5 * 1000:.0f} ms")
    return cat


def make_cert(days):
    """A self-signed certificate that expires in `days` days (needs the openssl tool)."""
    d = T + "/etc/letsencrypt/live/example.com"
    os.makedirs(d, exist_ok=True)
    try:
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", d + "/privkey.pem",
                        "-out", d + "/fullchain.pem", "-days", str(days), "-subj", "/CN=example.com"],
                       check=True, capture_output=True, timeout=60)
        shutil.copy(d + "/fullchain.pem", d + "/cert.pem")
        return True
    except Exception as e:
        print("       (openssl not usable here, certificate checks skipped:", type(e).__name__, ")")
        return False


async def new_app():
    app = (Application.builder().token(bot.BOT_TOKEN)
           .defaults(Defaults(parse_mode=ParseMode.HTML)).concurrent_updates(True)
           .request(FakeRequest()).get_updates_request(FakeRequest()).build())
    bot.register(app)
    await app.initialize()
    return app


async def test_startup():
    section("4) first start")
    bot.make_builder().build()
    check(True, "the real application builder builds")
    app = await new_app()
    real_detect = bot.detect_host

    def detect():
        h = real_detect()
        h.systemd, h.journal, h.docker_sock, h.hostname = True, True, "fake.sock", "srv-test"
        return h
    bot.detect_host = detect
    SENT.clear()
    await bot.post_init(app)
    first = [p for n, p, _ in SENT if n == "sendMessage"]
    check(len(first) == 2 and {int(p["chat_id"]) for p in first} == {ADMIN, ADMIN2}, "one welcome summary per admin")
    check("srv-test" in first[0]["text"] and "Ubuntu 24.04.1 LTS" in first[0]["text"], "summary names the host and OS")
    print("       " + first[0]["text"].replace("\n", "\n       "))
    cmds = [p for n, p, _ in SENT if n == "setMyCommands"]
    check(len(cmds) == 2 and any(p.get("language_code") == "fa" for p in cmds), "command menu set in both languages")
    check(len(bot.PROJECTS) >= 18 and len(bot.SERVICES) == 20, f"live picture: {len(bot.PROJECTS)} projects, {len(bot.SERVICES)} services")
    task = app.bot_data["monitor_task"]
    check(not task.done() and bot.MON.view, "the monitoring loop is running and has a first reading")
    task.cancel()   # from here on the tests call tick() themselves, so nothing depends on the clock
    SENT.clear()
    await bot.sync_catalog(app, force=True)
    check(not SENT, "second scan: nothing new → silent")
    check(sorted(os.listdir(bot.DATA_DIR)) == ["monitor.sqlite3"] and os.listdir(bot.WORK_DIR) == [],
          "only the history file exists; the work folder is empty")
    return app


async def test_menus(app):
    section("5) menus, in English and Persian")
    en = Client(app, ADMIN, "en")
    await en.say("/start")
    check("Server Wrench" in en.screen and "srv-test" in en.screen, "home screen")
    check(en.styles["📁 Files"] == "primary" and en.styles["📦 Backup"] == "success" and en.styles["📊 Status"] is None,
          "buttons carry colours (primary / success)")
    fa = Client(app, ADMIN2, "fa")
    await fa.say("/start")
    check("فقط‌خواندنی" in fa.screen and "📁 فایل‌ها" in fa.buttons, "Persian for a Persian Telegram client")
    for client in (en, fa):
        for data in ("m", "hp", "fl", "fg:cfg:0", "bk", "mp", "bq", "st", "sl", "sl:1", "dk", "tp", "rp", "ev",
                     "se", "hd", "sq"):
            await client.press(data)
            check_html(client.screen)
            assert client.screen, data
    check(True, "every top-level screen renders in both languages (HTML and lengths verified)")

    await en.press("fl")
    check("🐍 atlas_bot" in en.buttons and "🌐 example.com" in en.buttons and "1/2" in en.buttons,
          "project list: applications first, then sites; long lists are paged")
    check("🟢 ⚙️ atlas-bot" in en.screen and T + "/root/atlas_bot" in en.screen,
          "the list shows folder, service and its live state")
    print("       " + en.screen.replace(T, "").replace("\n", "\n       ")[:640] + " …")
    await en.press("fl:1")
    check("my new&lt;bot&gt;&amp;co" in en.screen and any(k.startswith("📄") for k in en.buttons),
          "second page: awkward names are escaped; the loose-files entry is there")
    await en.press("fg:cfg:0")
    check("⚙️ nginx" in en.buttons and "⚙️ systemd units" in en.buttons, "configuration folders group")
    return en, fa


async def test_files(app, en):
    section("6) file browser, preview, basket")
    await en.press("fl")
    await en.press(en.buttons["🐍 atlas_bot"])
    check("📍" in en.screen and "📁 5" in en.screen, "project root shows its info line")
    names = " | ".join(en.buttons)
    check(any(k.startswith("📄 atlas_bot.py") for k in en.buttons) and "1/2" in names, "folders first, then files, with a pager")
    root_cb = en.find("data") if any("📁 data" in k for k in en.buttons) else None
    check(root_cb is not None, "folders first")
    await en.press(root_cb)
    check("state.json" in " ".join(en.buttons) and "⬆️ Up" in en.buttons, "inside a sub-folder")
    await en.press(en.find("state.json"))
    check("state.json" in en.screen and "⬇️ Download" in en.buttons and en.styles["⬇️ Download"] == "success", "file card")
    file_cb = en.find("⬇️ Download")
    await en.press(file_cb)
    check(len(en.docs) == 1 and en.docs[0][0] == "state.json" and en.docs[0][1] == b'{"n": 1}', "download = the exact bytes")
    # preview hides .env values
    p = bot.PMAP["atlas_bot"]
    h = bot.REG.put(p.key, p.root + "/.env")
    await en.press(f"v:{h}")
    check("API_HASH=••••••" in en.screen and "supersecretvalue" not in en.screen, ".env preview hides the values")
    await en.press(f"dl:{h}")
    check(en.docs[0][1] == b"API_HASH=supersecretvalue\nTOKEN=12345:abcdef\n", "…while the download is complete")
    # a path outside every project is never served
    outside = bot.REG.put(p.key, T + "/root/.ssh/id_ed25519")
    await en.press(f"dl:{outside}")
    check(not en.docs and "expired" in en.toast, "a path outside the project is refused")
    os.symlink(T + "/root/.ssh/id_ed25519", p.root + "/sneaky")
    await en.press(f"dl:{bot.REG.put(p.key, p.root + '/sneaky')}")
    check(not en.docs, "a symlink pointing outside the project is refused")
    await en.press(f"d:{bot.REG.put(p.key, p.root)}:1")
    check(en.buttons.get("🔗 sneaky") == "noop", "symlinks are shown in the browser but cannot be opened")
    os.remove(p.root + "/sneaky")
    # loose files of /root
    flat = next(x for x in bot.PROJECTS if x.flat)
    await en.press(f"d:{bot.REG.put(flat.key, flat.root)}:0")
    labels = " | ".join(en.buttons)
    check("bot.py" in labels and "backup.sh" in labels and "atlas_bot" not in labels and ".bash_history" not in labels,
          "/root shows only its loose, visible files")
    await en.press(f"dl:{bot.REG.put(flat.key, flat.root + '/.bash_history')}")
    check(not en.docs, "hidden files of /root are not reachable")
    await en.press(f"d:{bot.REG.put(flat.key, flat.root + '/.ssh')}:0")
    check("expired" in en.toast, "folders of /root are not reachable through the loose-files entry")
    # basket
    await en.press(f"d:{bot.REG.put(p.key, p.root)}:0")
    await en.press(en.find("Select several"))
    check("Selecting" in en.screen, "select mode")
    await en.press(en.find("✅ This page"))
    check(en.toast == "6 items", "'This page' ticks everything on it except virtualenvs and caches")
    await en.press("bq")
    check("Basket" in en.screen and "items" in en.screen, "basket lists what was picked")
    await en.press("bqz")
    name, data, cap = en.docs[0]
    inside = zipfile.ZipFile(io.BytesIO(data)).namelist()
    check(name.startswith("selection_") and all(n.startswith("atlas_bot/") for n in inside)
          and not any("/venv/" in n or "__pycache__" in n for n in inside), f"basket zip ({len(inside)} files, venv skipped)")
    check(os.listdir(bot.WORK_DIR) == [] and bot.JOB is None, "nothing left in the work folder")
    await en.press("bq")
    check("empty" in en.screen, "the basket empties itself after a successful send")
    await en.press(f"sx:{bot.REG.put(p.key, p.root)}:0")


async def test_backup(app, en):
    section("7) backups")
    await en.press("bk")
    check("🐍 atlas_bot" in en.buttons and "Nothing is stored on the server" in en.screen, "backup menu")
    await en.press("b:atlas_bot")
    check("Essentials only" in " ".join(en.buttons), "project backup screen")
    ess = bot.essentials(bot.PMAP["atlas_bot"], True)
    check(set(ess) == {".env", "atlas_bot.py", "config.json", "requirements.txt", "my_account.session",
                       "data/state.json", "data/users.sqlite3"},
          f"essentials: code, config, data — not logs, media, venv or deep folders {sorted(ess)}")
    await en.press("bi:atlas_bot")
    name, data, cap = en.docs[0]
    zf = zipfile.ZipFile(io.BytesIO(data))
    check(sorted(zf.namelist()) == sorted("atlas_bot/" + r for r in ess) and zf.testzip() is None
          and name.startswith("atlas_bot_essentials_"), "essentials zip")
    snap = T + "/snap_check.sqlite3"
    Path(snap).write_bytes(zf.read("atlas_bot/data/users.sqlite3"))
    copy = sqlite3.connect(snap)
    rows = copy.execute("select count(*) from u").fetchone()[0]
    copy.close()
    check(rows == 200, "SQLite database inside the zip is intact")
    check("📦" in cap and "essentials" in cap and "7 files" in cap, "caption summarises the backup")
    await en.press("bf:atlas_bot")
    inside = zipfile.ZipFile(io.BytesIO(en.docs[0][1])).namelist()
    check("atlas_bot/logs/app.log" in inside and "atlas_bot/media/cover.jpg" in inside
          and not any("venv/" in n or ".pyc" in n for n in inside), "whole-project zip keeps logs and media, skips venv and caches")
    # pin a media file → it joins the essentials
    p = bot.PMAP["atlas_bot"]
    h = bot.REG.put(p.key, p.root + "/media/cover.jpg")
    await en.press(f"o:{h}")
    await en.press(en.find("Mark essential"))
    check("Marked essential" in en.toast and "☆ Unmark" in en.buttons, "mark a file essential")
    check("media/cover.jpg" in bot.essentials(p, True), "…and it is in the essentials from now on")
    await en.press(f"pin:{h}")
    # hand-picked
    await en.press("bs:atlas_bot")
    check("Selected: <b>0</b> of 7" in en.screen, "pick screen")
    await en.press(en.find("config.json"))
    await en.press(en.find("atlas_bot.py"))
    await en.press("bgs:atlas_bot")
    check(sorted(d[0] for d in en.docs) == ["atlas_bot.py", "config.json"] and "2 of 2" in en.screen, "picked files, one by one")
    # several projects
    await en.press("mp")
    await en.press("mt:atlas_bot:0")
    await en.press("mt:scraper:0")
    await en.press("mg:i:z")
    inside = zipfile.ZipFile(io.BytesIO(en.docs[0][1])).namelist()
    check("atlas_bot/config.json" in inside and "scraper/scraper.py" in inside, "two projects in one zip")
    await en.press("mg:f:s")
    check(len(en.docs) == 2 and "2 of 2" in en.screen, "two projects, a zip each")
    mk("/etc/nginx/ssl/site.key", "PRIVATE KEY")
    await en.press("ba")
    inside = zipfile.ZipFile(io.BytesIO(en.docs[0][1])).namelist()
    check(any(n.startswith("example_com/") for n in inside) and "cfg_nginx/sites-enabled/example" in inside
          and "cfg_systemd_units/atlas-bot.service" in inside,
          "essentials of everything includes sites and config folders (extension-less site files too)")
    check("cfg_nginx/ssl/site.key" not in inside, "…but never private keys")
    # zip a folder from the browser
    await en.press(f"bd:{bot.REG.put(p.key, p.root + '/data')}")
    inside = zipfile.ZipFile(io.BytesIO(en.docs[0][1])).namelist()
    check("data/state.json" in inside and "data/deep/a/b/c/too_deep.py" in inside, "zip of one folder")
    flat = next(x for x in bot.PROJECTS if x.flat)
    await en.press(f"bf:{flat.key}")
    inside = zipfile.ZipFile(io.BytesIO(en.docs[0][1])).namelist()
    check(sorted(inside) == [f"{flat.key}/backup.sh", f"{flat.key}/bot.py"], "'loose files' backup takes only those files")
    check(os.listdir(bot.WORK_DIR) == [], "work folder empty after all of that")


async def test_databases(app, en):
    section("8) databases inside a backup")
    d = Path(T) / "dbtest"
    work = d / "out"
    work.mkdir(parents=True)

    def make(name, mode, rows=1000, keep_open=False, no_checkpoint=False):
        db = sqlite3.connect(d / name)
        db.execute(f"PRAGMA journal_mode={mode}")
        if no_checkpoint:
            db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("create table t(x)")
        db.executemany("insert into t values(?)", [(i,) for i in range(rows)])
        db.commit()
        if keep_open:
            return db
        db.close()

    def state():
        return {n: (os.path.getsize(d / n), os.stat(d / n).st_mtime_ns) for n in sorted(os.listdir(d)) if n != "out"}

    def pack(*names):
        buf, flags = io.BytesIO(), {}
        with zipfile.ZipFile(buf, "w") as zf:
            for n in names:
                flags[n] = bot._add_file(zf, str(d / n), n, str(work))
        return zipfile.ZipFile(buf), flags

    def restored(zf, name):
        """Unpack the database (and whatever came with it) and open it the way anyone would."""
        box = Path(tempfile.mkdtemp(dir=T))
        for n in zf.namelist():
            if n == name or n.startswith(name + "-"):
                (box / n).write_bytes(zf.read(n))
        db = sqlite3.connect(box / name)
        out = (db.execute("select count(*) from t").fetchone()[0], db.execute("PRAGMA integrity_check").fetchone()[0])
        db.close()
        return out

    # recognised by content, not by name
    make("account.session", "DELETE")
    (d / "notes.db").write_text("just a text file with a database-like name\n")
    check(bot._sqlite_kind(str(d / "account.session")) == "journal" and bot._sqlite_kind(str(d / "notes.db")) == "",
          "a database is recognised by its header, whatever the file is called")
    zf, flags = pack("account.session", "notes.db")
    check(flags == {"account.session": True, "notes.db": True} and restored(zf, "account.session") == (1000, "ok")
          and zf.read("notes.db").startswith(b"just a text"), "…and snapshotted; a text file called .db is simply copied")
    # WAL mode, nobody has it open: SQLite would create -wal / -shm files to read it
    make("closed.sqlite3", "WAL")
    before = state()
    zf, flags = pack("closed.sqlite3")
    check(state() == before and "closed.sqlite3-wal" not in before, "a closed WAL database: nothing is created next to it")
    check(flags["closed.sqlite3"] is True and zf.namelist() == ["closed.sqlite3"] and restored(zf, "closed.sqlite3") == (1000, "ok"),
          "…and the copy is complete")
    # WAL mode, in use: committed rows that are still only in the write-ahead log
    app_db = make("live.sqlite3", "WAL", rows=1500, keep_open=True, no_checkpoint=True)
    before = state()
    zf, flags = pack("live.sqlite3")
    check("live.sqlite3-wal" in before and state() == before, "a WAL database in use: read without touching its files")
    check(flags["live.sqlite3"] is True and zf.namelist() == ["live.sqlite3"] and restored(zf, "live.sqlite3") == (1500, "ok"),
          "…one consistent snapshot with every committed row")
    # WAL mode after a crash: a log with content and no program
    shutil.copy(d / "live.sqlite3", d / "crashed.sqlite3")
    shutil.copy(d / "live.sqlite3-wal", d / "crashed.sqlite3-wal")
    app_db.close()
    before = state()
    logging_off()
    zf, flags = pack("crashed.sqlite3")
    logging_on()
    check(state() == before and flags["crashed.sqlite3"] is False
          and sorted(zf.namelist()) == ["crashed.sqlite3", "crashed.sqlite3-wal"],
          "a write-ahead log left by a crash: database and log go in as found, and the copy is flagged")
    check(restored(zf, "crashed.sqlite3") == (1500, "ok"), "…SQLite completes it when the copy is opened: nothing is lost")
    # rollback-journal mode, interrupted in the middle of a transaction ("hot" journal)
    writer = make("ledger.db", "DELETE", keep_open=True)
    writer.execute("PRAGMA cache_size=5")
    writer.execute("begin")
    writer.executemany("insert into t values(?)", [(i,) for i in range(20000)])
    shutil.copy(d / "ledger.db", d / "hot.db")
    shutil.copy(d / "ledger.db-journal", d / "hot.db-journal")
    writer.rollback()
    writer.close()
    before = state()
    logging_off()
    zf, flags = pack("hot.db")
    logging_on()
    check(state() == before and flags["hot.db"] is False and sorted(zf.namelist()) == ["hot.db", "hot.db-journal"]
          and restored(zf, "hot.db") == (1000, "ok"),
          "an interrupted transaction: database and journal go in together, and opening the copy rolls it back")
    # a large rollback-journal database is copied in steps, so its program can keep committing
    big = sqlite3.connect(d / "big.db")
    big.execute("create table t(x)")
    big.executemany("insert into t values(?)", [("y" * 1000,) for _ in range(12000)])
    big.commit()
    big.close()
    one_go, limit = bot.SNAP_ONE_GO, bot.SNAP_SECONDS
    bot.SNAP_ONE_GO = 0
    zf, flags = pack("big.db")
    check(flags["big.db"] is True and restored(zf, "big.db") == (12000, "ok"), "a large database: snapshot in short steps")
    bot.SNAP_SECONDS = -1
    logging_off()
    zf, flags = pack("big.db")
    logging_on()
    bot.SNAP_ONE_GO, bot.SNAP_SECONDS = one_go, limit
    check(flags["big.db"] is False and "big.db" in zf.namelist(), "…with a time limit: a database that never rests is copied as found")
    check(os.listdir(work) == [], "no snapshot file is left in the work folder")
    # SQLite's working files are left out of a backup — an ordinary file with such a name is not
    (d / "trade-journal").write_text("monday: bought\n")
    make("shop.db", "WAL", keep_open=True).close()
    (d / "shop.db-wal").write_bytes(b"")
    names = {os.path.basename(src) for src, _ in bot.pairs_tree(str(d), "x")}
    check("trade-journal" in names and "shop.db" in names and "shop.db-wal" not in names and "hot.db-journal" not in names,
          "working files of a database are left out; a file that just ends in -journal stays in")
    # the admin is told, in the caption
    scraper = Path(T + "/root/scraper")
    shutil.copy(d / "crashed.sqlite3", scraper / "state.sqlite3")
    shutil.copy(d / "crashed.sqlite3-wal", scraper / "state.sqlite3-wal")
    logging_off()
    await en.press("bf:scraper")
    logging_on()
    caption = "\n".join(c for _, _, c in en.docs) + en.screen
    names = zipfile.ZipFile(io.BytesIO(en.docs[0][1])).namelist()
    check("copied as found" in caption and "scraper/state.sqlite3-wal" in names, "the backup caption says which kind of copy it was")
    os.remove(scraper / "state.sqlite3")
    os.remove(scraper / "state.sqlite3-wal")


async def test_split_and_big(app, en):
    section("9) large files: parts, and the big-file channel")
    big = T + "/root/scraper/dump.bin"
    Path(big).write_bytes(os.urandom(300 * 1024))
    saved = (bot.TG_PART_SIZE, bot.MT_PART_SIZE, bot.MT_MAX_BYTES)
    bot.TG_PART_SIZE, bot.MT_PART_SIZE, bot.MT_MAX_BYTES = 64 * 1024, 256 * 1024, 512 * 1024
    p = bot.PMAP["scraper"]
    h = bot.REG.put(p.key, big)
    await en.press(f"o:{h}")
    check("5 parts" in en.screen, "the file card says how it will arrive")
    await en.press(f"dl:{h}")
    parts = en.docs
    check([d[0] for d in parts] == [f"dump.bin.{i:03d}" for i in range(1, 6)]
          and b"".join(d[1] for d in parts) == Path(big).read_bytes(), "without big-file mode: 5 parts that join back exactly")
    check("7-Zip" in parts[0][2] and "5/5" in parts[-1][2], "the first part explains how to join them")
    await en.press("bf:scraper")
    zparts = en.docs
    check(len(zparts) >= 5 and zparts[0][0].endswith(".zip.001"), f"a large zip is streamed in {len(zparts)} parts")
    zf = zipfile.ZipFile(io.BytesIO(b"".join(d[1] for d in zparts)))
    check(zf.read("scraper/dump.bin") == Path(big).read_bytes() and os.listdir(bot.WORK_DIR) == [],
          "…which join into a valid zip; nothing stays on disk")

    class FakeMT:
        uploads, fail = [], None

        async def upload_file(self, source, file_size=None, file_name=None, progress_callback=None):
            if FakeMT.fail:
                raise FakeMT.fail
            data = Path(source).read_bytes() if isinstance(source, str) else source.read()
            assert len(data) == file_size or isinstance(source, str)
            if progress_callback:
                progress_callback(len(data), len(data))
            return (file_name, data)

        async def send_file(self, chat_id, handle, caption=None, force_document=False, parse_mode=None):
            assert force_document and parse_mode == "html"
            if caption:
                check_html(caption)
            FakeMT.uploads.append((handle[0], handle[1], caption or ""))

        async def disconnect(self):
            pass

    async def connect():
        bot.BIG.client = FakeMT()
        return bot.BIG.client
    bot.API_ID, bot.API_HASH = 12345, "0123456789abcdef0123456789abcdef"
    bot.BIG._lib, bot.BIG._connect = True, connect
    check(bot.BIG.ready() and bot.one_file_cap() == 512 * 1024, "big-file mode is on")
    await en.press(f"dl:{h}")
    check(not en.docs and len(FakeMT.uploads) == 1 and FakeMT.uploads[0][0] == "dump.bin"
          and FakeMT.uploads[0][1] == Path(big).read_bytes(), "with big-file mode: the same file arrives in one piece")
    FakeMT.uploads.clear()
    await en.press("bf:scraper")
    pieces = sorted([(u[0], u[1]) for u in FakeMT.uploads] + [(d[0], d[1]) for d in en.docs])
    check(len(FakeMT.uploads) == 1 and len(pieces) == 2 and pieces[0][0].endswith(".zip.001")
          and len(pieces[0][1]) == 256 * 1024,
          "a zip above the part size: big parts through the channel, a small tail through the Bot API")
    zf = zipfile.ZipFile(io.BytesIO(b"".join(d for _, d in pieces)))
    check(zf.read("scraper/dump.bin") == Path(big).read_bytes(), "…that also join into a valid zip")
    Path(big).write_bytes(os.urandom(1200 * 1024))
    FakeMT.uploads.clear()
    await en.press(f"dl:{bot.REG.put(p.key, big)}")
    check([u[0] for u in FakeMT.uploads] == [f"dump.bin.{i:03d}" for i in range(1, 6)]
          and b"".join(u[1] for u in FakeMT.uploads) == Path(big).read_bytes(),
          "a file above Telegram's own limit is split even in big-file mode")
    # the channel breaks → automatic fall-back to ordinary parts
    Path(big).write_bytes(os.urandom(200 * 1024))
    FakeMT.uploads.clear()
    FakeMT.fail = ConnectionError("network unreachable")
    await en.press(f"dl:{bot.REG.put(p.key, big)}")
    check(len(en.docs) == 4 and b"".join(d[1] for d in en.docs) == Path(big).read_bytes()
          and "not available right now" in " ".join(c[1].get("text", "") for c in en.of("sendMessage")),
          "channel failure → the admin is told and the file still arrives, in parts")
    check(not bot.BIG.ready() and bot.BIG.status() == "paused", "the channel pauses itself after a failure")
    await en.press("se")
    check("paused after an error" in en.screen and "ConnectionError" in en.screen, "settings show why")
    bot.BIG.retry_at = 0
    FakeMT.fail = None
    await asyncio.sleep(0)
    bot.TG_PART_SIZE, bot.MT_PART_SIZE, bot.MT_MAX_BYTES = saved
    bot.API_ID, bot.API_HASH = 0, ""
    os.remove(big)
    check(os.listdir(bot.WORK_DIR) == [], "work folder empty")


async def test_search(app, en, fa):
    section("10) search")
    await en.press("sq")
    check("Typos are fine" in en.screen, "search prompt")
    await en.say("atlas_bot.py")
    check("atlas_bot.py" in " ".join(en.buttons) and "results" in en.screen, "exact name")
    first = list(en.buttons)[0]
    check("atlas_bot.py" in first, "the exact match is first")
    await en.press(en.buttons[first])
    check("⬇️ Download" in en.buttons, "a result opens the file card")
    await en.say("confg")
    check("config.json" in " ".join(en.buttons) and "closest names" in en.screen, "typo → nearest names")
    await en.say("reqirements")
    check(sum("requirements.txt" in k for k in en.buttons) >= 1, "missing letter")
    await en.say("scraper req")
    check(any("requirements.txt" in k for k in en.buttons) and "scraper" in en.screen, "several words narrow by path")
    await en.say("state")
    check("state.json" in " ".join(en.buttons), "part of a name")
    await en.say("data")
    check(any(k.startswith("📁 data") for k in en.buttons), "folders are found too")
    await en.say("qqqqzzzz")
    check("Nothing matches" in en.screen, "no result")
    await en.say("x")
    check("at least 2" in en.screen, "too short")
    await en.say("/find CHANNEL_ID")
    check("Nothing matches" in en.screen and "🔍 Search inside files" in en.buttons, "name search finds nothing…")
    await en.press("sc")
    check("alpha" in en.screen and "scraper" in en.screen and "CHANNEL_ID = -100123" in en.screen
          and ":2" in en.screen, "…content search finds the files and the line")
    await en.say("supersecretvalue")
    await en.press("sc")
    check("API_HASH=••••••" in en.screen and "supersecretvalue</code>\n<code>" not in en.screen
          and en.screen.count("supersecretvalue") == 1, "content search never echoes .env values")
    await fa.say("کانفیگ")
    check("چیزی پیدا نشد" in fa.screen, "Persian query, Persian answer")
    res = bot.search_names("bot", bot.PROJECTS)
    check(res["scanned"] > 40 and res["secs"] < 2 and not res["cut"], f"scanned {res['scanned']} entries in {res['secs'] * 1000:.0f} ms")
    check(not any("/venv/" in h.path or "/.ssh/" in h.path or ".bash_history" in h.path for h in res["hits"]),
          "search never leaves the projects (no venv, no hidden home files)")


async def test_services(app, en, fa):
    section("11) status, services, logs")
    await en.say("/status")
    s = en.screen
    check("Ubuntu 24.04.1 LTS" in s and "KVM · Hetzner" in s and "vCPU" in s, "header: OS, virtualisation, provider, size")
    check(re.search(r"CPU  [█░]{10} +\d", s) and "RAM  " in s and "Disk " in s, "resource gauges")
    check("Services:" in s and "🔴 ⚙️ <code>tool</code> — failed (exit-code)" in s, "failed service is called out")
    check("broken-thing.service" in s and s.count("tool") == 1, "other failed units listed once")
    check("Sites: 5" in s and "This bot:" in s, "sites and the bot's own footprint")
    print("       " + s.replace("\n", "\n       "))
    await en.press("sl")
    s = en.screen
    check("Your apps" in s and "System services" in s, "services grouped")
    check("🟢 ⚙️ <code>atlas-bot</code> · " in s and "83.9 MB" in s, "running service: uptime and memory")
    check("⚪️ ⚙️ <code>proxy</code> — off" in s, "a disabled, stopped unit is 'off', not an alarm")
    await en.press("sl:1")
    s = en.screen
    check("🔴 ⚙️ <code>redis-server</code> — stopped" in s, "enabled but not running → red")
    check("🟢 🐳 <code>shop-web-1</code>" in s and "⚪️ 🐳 <code>watchtower</code> — off (exit 1)" in s, "containers")
    check("🟢 ▶️ <code>scraper</code> · 2d · 60.0 MB" in s, "program without a service: uptime and memory")
    await en.press("sv:atlas-bot")
    s = en.screen
    check("running" in s and "systemd service · enabled" in s and "🐍 atlas_bot" in s, "service card")
    await en.press("lv:atlas-bot")
    check("&lt;ok&gt; &amp; fine" in en.screen and "last 40 lines" in en.screen, "log, safely escaped")
    await en.press("le:atlas-bot")
    check("2 errors/warnings" in en.screen and "started" not in en.screen, "errors only")
    await en.press("lf:atlas-bot")
    check(en.docs and en.docs[0][0].startswith("atlas-bot_") and en.docs[0][1] == JOURNAL.encode(), "log file from memory")
    await en.press("uf:atlas-bot")
    check("ExecStart=" in en.screen and "Environment=SECRET_KEY=••••••" in en.screen and "topsecret" not in en.screen,
          "unit file preview hides Environment values")
    await en.press("sv:d/shop-web-1")
    check("Docker container" in en.screen and "always" in en.screen, "container card")
    await en.press("lv:d/shop-web-1")
    check("container says hello" in en.screen and "ERROR container broke" in en.screen, "container log (stdout and stderr)")
    await en.press("le:d/shop-web-1")
    check("1 errors" in en.screen, "container errors only")
    await en.press("sv:p/scraper")
    check("without a service manager" in en.screen and "has no journal" in en.screen and "📜 Logs" not in en.buttons,
          "program card explains why there is no journal")
    await en.press("mu:tool")
    check("muted" in en.toast and "🔔 Unmute alerts" in en.buttons and bot.SET["muted"] == ["tool"], "mute a service")
    await en.press("mu:tool")
    await en.say("/logs nginx")
    check("nginx" in en.screen and "last 40 lines" in en.screen, "/logs <name>")
    await en.say("/logs nope")
    check("Services" in en.screen, "/logs with an unknown name → the list")
    await fa.press("sl")
    check("برنامه‌های تو" in fa.screen and "در حال اجرا" not in fa.screen.split("\n")[0], "services in Persian")
    await en.press("pj:atlas_bot")
    check("Application" in en.screen and "Essential files: <b>7</b>" in en.screen and any("📜" in k for k in en.buttons),
          "project card links files, backup and its services")


async def test_disk_procs(app, en):
    section("12) disk and processes")
    await en.press("dk")
    check(re.search(r"<code>[█░]{16} \d+%</code>", en.screen) and "free" in en.screen, "disk gauge per filesystem")
    await en.press("du")
    s = en.screen
    check("What takes the space" in s and "atlas_bot" in s and "logs (/var/log)" in s, "space usage per project and system place")
    first = s.split("\n")[3]
    check("<code>████████</code>" in first, "largest first, with a bar")
    print("       " + s.replace(T, "").replace("\n", "\n       ")[:700])
    measured = []
    real_measure = bot.measure_usage
    bot.measure_usage = lambda projects: measured.append(1) or real_measure(projects)
    await en.press("du")
    bot.measure_usage = real_measure
    check(not measured and "What takes" in en.screen, "the measurement is cached")
    await en.press("di")
    photos = en.of("sendPhoto")
    if bot.Image is None:
        check(not photos and "Pillow" in en.toast, "without Pillow the chart button says what is missing")
    else:
        check(len(photos) == 1 and list(photos[0][2].values())[0][:4] == b"\x89PNG" and "full" in photos[0][1]["caption"],
              "disk chart image")
    await en.press("tp")
    s = en.screen
    print("       " + s.replace("\n", "\n       "))
    check("Processes" in s and "python3 scraper.py" in s and "atlas-bot" in s and "docker" in s,
          "processes with the service or project they belong to")
    check(s.index("node main.js") < s.index("python atlas_bot.py"), "sorted by memory")
    # a network drive whose server is gone: statvfs() on it never returns
    nas = T + "/mnt/nas"
    mkd("/mnt/nas")
    table = Path(T + "/proc/mounts").read_text()
    mk("/proc/mounts", table + f"nas:/export {nas} nfs4 rw 0 0\n")
    server_back = threading.Event()
    asked = []
    real_statvfs, real_stat, wait = os.statvfs, os.stat, bot.MOUNT_WAIT

    def statvfs(path):
        if path == nas:
            asked.append(path)
            server_back.wait(60)
        return real_statvfs(path)

    def stat(path, *args, **kwargs):
        if path == nas and not args and not kwargs:
            return types.SimpleNamespace(st_dev=987654321)   # a filesystem of its own
        return real_stat(path, *args, **kwargs)
    os.statvfs, os.stat, bot.MOUNT_WAIT = statvfs, stat, 0.3
    try:
        first = await asyncio.wait_for(asyncio.to_thread(bot.read_mounts), 20)
        second = await asyncio.wait_for(asyncio.to_thread(bot.read_mounts), 20)
        check([m.path for m in first] == ["/"] == [m.path for m in second] and len(asked) == 1
              and bot.SILENT_MOUNTS == [(nas, "nfs4")],
              "a network drive that does not answer is left out after a short wait; one helper waits for it, not one per reading")
        bot.MON.mounts_at = 0
        await en.press("dk")
        check("does not answer" in en.screen and "/mnt/nas" in en.screen and "<b>/</b>" in en.screen,
              "the Disk screen says so and still shows the other filesystems")
        await asyncio.wait_for(bot.MON.tick(), 20)
        check(True, "the monitor keeps ticking meanwhile")
        server_back.set()
        for _ in range(200):
            if not bot._WAITING[nas].is_alive():
                break
            await asyncio.sleep(0.02)
        third = await asyncio.to_thread(bot.read_mounts)
        check([m.path for m in third] == ["/", nas] and not bot.SILENT_MOUNTS, "it is listed again as soon as it answers")
    finally:
        server_back.set()
        os.statvfs, os.stat, bot.MOUNT_WAIT = real_statvfs, real_stat, wait
        mk("/proc/mounts", table)
        bot.MON.mounts_at = 0


def fill_history(days=8):
    """Synthetic monitoring history, so reports have something to draw."""
    import math
    import random
    random.seed(7)
    now = int(time.time()) // 60 * 60
    rows, svc_rows = [], []
    for i in range(days * 1440):
        ts = now - (days * 1440 - i) * 60
        hour = (ts // 3600) % 24
        cpu = 8 + 30 * max(0.0, math.sin((hour - 9) / 24 * 2 * math.pi)) + random.random() * 6
        if i % 1440 == 800:
            cpu = 97
        rows.append((ts, round(cpu * 10), 55, 480 + i % 37, 30, 412 + i // 2000, 40000 + hour * 9000, 30000 + hour * 4000))
        if i % 5 == 0:
            for sid, mem in (("atlas-bot", 86000), ("nginx", 14000), ("d/shop-web-1", 120000), ("p/scraper", 61000),
                             ("tool", None)):
                svc_rows.append((ts, sid, 0 if sid == "tool" else 100, mem, 12))
    with bot.STORE.db as db:
        db.executemany("INSERT OR REPLACE INTO sys VALUES(?,?,?,?,?,?,?,?)", rows)
        for ts, name, up, mem, cpu in svc_rows:
            db.execute("INSERT OR REPLACE INTO svc VALUES(?,?,?,?,?)", (ts, bot.STORE._sid(db, name), up, mem, cpu))
        db.execute("INSERT OR REPLACE INTO logstat VALUES(?,?,?,?,?)", (now - 7200, bot.STORE._sid(db, "atlas-bot"), 900, 14, 3))
        db.execute("INSERT OR REPLACE INTO logstat VALUES(?,?,?,?,?)", (now - 7200, bot.STORE._sid(db, bot.WEB_KEY), 12345, 3, 0))
    bot.STORE.add_event("crash", "atlas-bot", "auto-restart #1", now - 5000)
    bot.STORE.add_event("restart", "nginx", "manual", now - 4000)
    return now


async def test_reports(app, en, fa):
    section("13) reports")
    await en.press("rg:7")
    check("Not enough data" in en.screen, "no data yet → a clear message")
    fill_history()
    await en.press("rp")
    check("one-minute samples" in en.screen and "Scheduled report: every Monday at 09:00" in en.screen, "report menu")
    if bot.Image is None:
        print("       (Pillow is not installed: chart checks skipped)")
    await en.press("rg:7")
    groups = en.of("sendMediaGroup")
    if bot.Image is not None:
        check(len(groups) == 1 and len(groups[0][2]) == 3
              and all(v[:4] == b"\x89PNG" for v in groups[0][2].values()), "three chart images")
        caption = json.loads(groups[0][1]["media"])[0].get("caption") or en.screen
    else:
        caption = en.screen
    check("<b>Resources</b>" in caption and "<b>Rhythm</b>" in caption and "<b>Services</b>" in caption
          and "\n\n" in caption, "caption in sections with blank lines between them")
    check(re.search(r"CPU: avg <b>[\d.]+%</b> · peak <b>97%</b>", caption) and "Crashes: <b>1</b> (atlas-bot ×1)" in caption
          and "Errors in logs: <b>14</b>" in caption and "Site requests: <b>12,345</b>" in caption,
          "key figures are bold and correct")
    check(plain_len(caption) <= 1000, f"caption fits under the images ({plain_len(caption)} characters)")
    print("       " + caption.replace("\n", "\n       "))
    await fa.press("rg:1")
    groups = fa.of("sendMediaGroup")
    cap_fa = (json.loads(groups[0][1]["media"])[0].get("caption") if groups else None) or fa.screen
    check("<b>منابع</b>" in cap_fa and "میانگین" in cap_fa and "٪" in cap_fa, "Persian caption")
    await en.say("/report 30")
    check(en.of("sendMediaGroup") or "Resources" in en.screen, "/report 30")
    await en.press("ev")
    check("crashed and was restarted" in en.screen and "<code>1001</code> download" in en.screen, "events: services and admin actions")
    r = bot.build_report(bot.STORE, int(time.time()) - 7 * 86400, int(time.time()))
    check(abs(r.coverage - 1) < 0.01 and r.peak_hour is not None and r.web_hits == 12345, "report numbers")


def said(uid=ADMIN):
    """Everything the bot just sent to one admin (alerts are per admin, per language)."""
    return "\n".join(c[1]["text"] for c in SENT if c[0] == "sendMessage" and int(c[1]["chat_id"]) == uid)


async def test_monitor(app, en):
    section("14) the monitor: events and alerts")
    mon = bot.MON
    bot.CATALOG_AT = time.time()
    SENT.clear()
    await mon.tick()
    await mon.tick()
    check(not [c for c in SENT if c[0] == "sendMessage"], "steady state: no alerts")
    check({"atlas-bot", "d/shop-web-1", "p/scraper"} <= mon.seen_up and "tool" not in mon.seen_up, "who is up is known")
    # a service fails
    UNITS["alpha"] = U("failed", "failed", result="exit-code", mem="[not set]")
    SENT.clear()
    await mon.tick()
    check(not [c for c in SENT if c[0] == "sendMessage"], "one bad reading is not an alert yet")
    await mon.tick()
    texts = [c[1]["text"] for c in SENT if c[0] == "sendMessage"]
    check(len(texts) == 2 and "<code>alpha</code> is down" in texts[0] and "failed" in texts[0], "second bad reading → one alert per admin")
    await mon.tick()
    check(len([c for c in SENT if c[0] == "sendMessage"]) == 2, "…and it is not repeated")
    UNITS["alpha"] = U(enter="2000000")
    SENT.clear()
    await mon.tick()
    check("alpha</code> is running again" in said(), "recovery alert")
    # crash with automatic restart
    UNITS["atlas-bot"] = U(nrest="1", enter="3000000")
    SENT.clear()
    await mon.tick()
    check("crashed and was restarted automatically (restart #1)" in said(), "crash alert")
    UNITS["atlas-bot"] = U(nrest="2", enter="4000000")
    SENT.clear()
    await mon.tick()
    check(not [c for c in SENT if c[0] == "sendMessage"], "a crash loop does not flood the chat")
    # manual restart is an event, not an alert
    UNITS["nginx"] = U(enter="9999999")
    await mon.tick()
    kinds = [(k, w) for _, k, w, _ in bot.STORE.fetch_events(limit=30)]
    check(("restart", "nginx") in kinds and ("crash", "atlas-bot") in kinds and ("down", "alpha") in kinds
          and ("up", "alpha") in kinds, "events recorded")
    # container: unhealthy, then gone
    CONTAINERS["shop-web-1"]["State"]["Health"]["Status"] = "unhealthy"
    SENT.clear()
    await mon.tick()
    check("shop-web-1</code> is running but reports <b>unhealthy</b>" in said(), "unhealthy container")
    CONTAINERS["shop-web-1"]["State"]["Health"]["Status"] = "healthy"
    CONTAINERS["shop-db-1"]["State"].update(Running=False, Status="exited", ExitCode=137, OOMKilled=True)
    SENT.clear()
    await mon.tick()
    await mon.tick()
    check("shop-db-1</code> is down" in said() and "exit 137 OOM" in said(), "container down, with the reason")
    CONTAINERS["shop-db-1"]["State"].update(Running=True, Status="running", ExitCode=0, OOMKilled=False)
    await mon.tick()
    # a hand-started program dies
    rmproc(600)
    SENT.clear()
    await mon.tick()
    await mon.tick()
    check("▶️ <code>scraper</code> is down" in said(), "program without a service stopped → alert")
    mkproc(600, "python3", ["python3", "scraper.py"], "{T}/root/scraper", age=2 * 86400, exe="/usr/bin/python3.12", rss_mb=60)
    await mon.tick()
    # muted service
    bot.SET.set("muted", ["x-ui"])
    UNITS["x-ui"] = U("failed", "failed", result="signal")
    SENT.clear()
    await mon.tick()
    await mon.tick()
    check(not [c for c in SENT if c[0] == "sendMessage"] and ("down", "x-ui") in
          [(k, w) for _, k, w, _ in bot.STORE.fetch_events(limit=10)], "muted: recorded, not announced")
    UNITS["x-ui"] = U()
    bot.SET.set("muted", [])
    await mon.tick()
    # disk and memory
    real_mounts = bot.read_mounts
    bot.read_mounts = lambda: [bot.Mount("/", "/dev/vda1", "ext4", 100 * bot.MIB, 95 * bot.MIB, 5 * bot.MIB)]
    mon.mounts_at = 0
    SENT.clear()
    await mon.tick()
    check("Disk <code>/</code> is <b>95%</b> full (5.0 MB free)" in said(), "disk alert")
    mon.mounts_at = 0
    SENT.clear()
    await mon.tick()
    check(not [c for c in SENT if c[0] == "sendMessage"], "…once a day")
    bot.read_mounts = real_mounts
    mon.mounts_at = 0
    mk("/proc/meminfo", "MemTotal: 4000000 kB\nMemAvailable: 100000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n")
    SENT.clear()
    for _ in range(bot.MEM_ALERT_SAMPLES):
        await mon.tick()
    texts = [c[1]["text"] for c in SENT if c[0] == "sendMessage"]
    check(len(texts) == 2 and "RAM has been above <b>98%</b> for 5 minutes" in texts[0], "RAM alert only after it stays high")
    mk("/proc/meminfo", "MemTotal: 4000000 kB\nMemAvailable: 2500000 kB\nSwapTotal: 2000000 kB\nSwapFree: 1900000 kB\n")
    # alerts off
    bot.SET.set("alerts", False)
    UNITS["uv"] = U("failed", "failed")
    SENT.clear()
    await mon.tick()
    await mon.tick()
    check(not [c for c in SENT if c[0] == "sendMessage"], "alerts switched off → silence")
    UNITS["uv"] = U()
    bot.SET.set("alerts", True)
    await mon.tick()
    # per-admin language
    bot.SET.set("langs", {str(ADMIN): "en", str(ADMIN2): "fa"})
    UNITS["rootbot"] = U("failed", "failed")
    SENT.clear()
    await mon.tick()
    await mon.tick()
    by_chat = {int(c[1]["chat_id"]): c[1]["text"] for c in SENT if c[0] == "sendMessage"}
    check("is down" in by_chat[ADMIN] and "از کار افتاده" in by_chat[ADMIN2], "each admin is told in their own language")
    UNITS["rootbot"] = U()
    await mon.tick()
    # the monitor survives systemd / docker not answering
    global SYSTEMD_OK, DOCKER_OK
    SYSTEMD_OK = DOCKER_OK = False
    SENT.clear()
    bot.CATALOG_AT = 0
    await mon.tick()
    await mon.tick()
    check(not [c for c in SENT if c[0] == "sendMessage"] and len(bot.SERVICES) == 20,
          "systemd and Docker silent for a moment → no false alarms, nothing 'disappears'")
    SYSTEMD_OK = DOCKER_OK = True
    bot.CATALOG_AT = time.time()
    rows = bot.STORE.fetch_svc(0, 2 ** 40)
    check(any(n == "p/scraper" for n, *_ in rows) and any(n == "d/shop-web-1" for n, *_ in rows), "per-service history is stored")
    # the loop itself: one tick per interval, and a tick that fails never stops it
    calls = []

    async def flaky_tick():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
    every = bot.SAMPLE_EVERY
    mon.tick, bot.SAMPLE_EVERY = flaky_tick, 0.2
    logging_off()
    loop_task = asyncio.create_task(mon.run())
    for _ in range(400):
        if len(calls) >= 3:
            break
        await asyncio.sleep(0.05)
    alive = not loop_task.done()
    loop_task.cancel()
    await asyncio.gather(loop_task, return_exceptions=True)
    logging_on()
    del mon.tick
    bot.SAMPLE_EVERY = every
    check(alive and len(calls) >= 3, "the loop keeps ticking on schedule, also after a tick that failed")


async def test_changes(app, en):
    section("15) the server changes while the bot runs")
    SENT.clear()
    mk("/root/brand_new_bot/bot.py", "# new\n")
    await en.press("rs")
    check("1 new" in en.toast and any("brand_new_bot" in k for k in en.buttons) or "1 new" in en.toast, "'Scan again' finds a new folder")
    notes = [c[1]["text"] for c in SENT if c[0] == "sendMessage"]
    check(len(notes) == 2 and "brand_new_bot" in notes[0] and "New on the server" in notes[0], "admins are told")
    unit("brand-new-bot", "[Service]\nWorkingDirectory={T}/root/brand_new_bot\nExecStart=/usr/bin/python3 bot.py\n")
    UNITS["brand-new-bot"] = U()
    SENT.clear()
    bot.CATALOG_AT = 0
    await bot.MON.tick()
    p = bot.PMAP["brand_new_bot"]
    check(p.kind == "app" and p.services == ("brand-new-bot",), "its service is attached on the next automatic scan")
    check(any("New service" in c[1]["text"] and "brand-new-bot" in c[1]["text"] for c in SENT if c[0] == "sendMessage"),
          "…and announced")
    check("brand-new-bot" in bot.MON.states, "…and watched from the same minute")
    # rename, hide, unhide
    await en.press("pj:brand_new_bot")
    await en.press(en.find("Rename"))
    check("Send the new name" in en.screen, "rename asks for a name")
    await en.say("🚀 My <new> bot")
    check("Renamed" in en.screen and bot.PMAP["brand_new_bot"].title == "🚀 My <new> bot", "custom title, kept escaped")
    await en.press("fl")
    await en.press("fl:1")
    check(True, "list renders with the custom title")
    await en.press("ph:brand_new_bot")
    check("brand_new_bot" not in bot.PMAP and len(bot.HIDDEN) == 1 and "brand-new-bot" in bot.SMAP,
          "hidden from the menus; its service is still watched")
    await en.press("hd")
    await en.press("uh:0")
    check("brand_new_bot" in bot.PMAP and not bot.HIDDEN, "shown again")
    await en.press("pa:brand_new_bot")
    check(bot.PMAP["brand_new_bot"].title == "🐍 brand_new_bot", "automatic name restored")
    # removal
    shutil.rmtree(T + "/root/brand_new_bot")
    os.remove(T + "/etc/systemd/system/brand-new-bot.service")
    UNITS.pop("brand-new-bot")
    bot.CATALOG_AT = 0
    await bot.MON.tick()
    check("brand_new_bot" not in bot.PMAP and "brand-new-bot" not in bot.SMAP and "brand-new-bot" not in bot.MON.states,
          "a removed project and its service leave cleanly")
    await en.press("ev")
    check("found on the server" in en.screen and "is no longer on the server" in en.screen, "both are in the events list")
    # a program started by hand: ignored while it is seconds old, watched once it keeps running
    check("p/notes" not in bot.SMAP, "a program that has only just started is not a service yet")
    mkproc(620, "python3", ["python3", "tmp.py"], "{T}/root/notes", age=300, exe="/usr/bin/python3")
    SENT.clear()
    bot.CATALOG_AT = 0
    await bot.MON.tick()
    check("p/notes" in bot.SMAP and bot.is_up(bot.MON.states.get("p/notes")) and "notes" in said(),
          "…after two minutes it is found, announced and watched (nohup, screen, tmux, …)")
    mkproc(620, "python3", ["python3", "tmp.py"], "{T}/root/notes", age=JUST_STARTED, exe="/usr/bin/python3")
    bot.CATALOG_AT = 0
    await bot.MON.tick()
    check("p/notes" not in bot.SMAP and "notes" not in bot.PMAP, "…and forgotten again when it is gone")
    # a failing scan keeps the last good picture
    real = bot.scan_server

    def boom(containers=None):
        raise PermissionError("nope")
    bot.scan_server = boom
    before = bot.PROJECTS
    logging_off()
    await en.press("rs")
    logging_on()
    check("scan failed" in en.toast and bot.PROJECTS is before, "scan error → previous list kept, and the admin is told")
    # a scan that hangs (a project on a network drive that is gone) must not take the bot with it
    release = threading.Event()
    scans = []

    def hang(containers=None):
        scans.append(1)
        release.wait(60)
        return real(containers)
    bot.scan_server, limit, bot.SCAN_TIMEOUT = hang, bot.SCAN_TIMEOUT, 0.3
    logging_off()
    try:
        ok1 = await asyncio.wait_for(bot.sync_catalog(app, force=True), 20)
        ok2 = await asyncio.wait_for(bot.sync_catalog(app, force=True), 20)
        await asyncio.wait_for(bot.MON.tick(), 20)
        await en.press("fl")
        check(ok1 is False and ok2 is False and len(scans) == 1 and bot.PROJECTS is before and "Projects on this server" in en.screen,
              "a scan that hangs is given up on, never started twice, and the menus keep working")
    finally:
        release.set()
        for _ in range(300):
            if bot._SCAN_JOB is None or bot._SCAN_JOB.done():
                break
            await asyncio.sleep(0.02)
        bot.scan_server, bot.SCAN_TIMEOUT = real, limit
        logging_on()
    check(await bot.sync_catalog(app, force=True) is True, "…and scanning resumes once the drive answers")


def logging_off():
    """For steps where the bot is *supposed* to log an error or a refusal."""
    logging.disable(logging.CRITICAL)


def logging_on():
    logging.disable(logging.NOTSET)
    PROBLEMS.clear()


async def test_settings(app, en, fa):
    section("16) settings and scheduled reports")
    await en.press("se")
    check("Language: <b>English</b>" in en.screen and "Disk alert at <b>90%</b>" in en.screen and "Big files: <b>off</b>" in en.screen,
          "settings screen")
    await en.press("set:lang")
    check("زبان: <b>فارسی</b>" in en.screen and bot.SET["langs"][str(ADMIN)] == "fa", "language switch is immediate and remembered")
    await en.press("m")
    check("فقط‌خواندنی" in en.screen, "…for every later screen")
    await en.press("set:lang")
    for data, key, want in (("set:disk", "disk_pct", 95), ("set:mem", "mem_pct", 98), ("set:report", "report", "off"),
                            ("set:report", "report", "daily"), ("set:hour:1", "report_hour", 10),
                            ("set:hour:-1", "report_hour", 9), ("set:week", "week_start", 5),
                            ("set:alerts", "alerts", False), ("set:alerts", "alerts", True)):
        await en.press(data)
        check(bot.SET[key] == want, f"{data} → {key} = {want}")
    await asyncio.sleep(0.05)
    saved = json.loads(bot.STORE.get_meta("settings"))
    check(saved["disk_pct"] == 95 and saved["report"] == "daily", "settings are stored in the history file")
    # daily report fires once at its hour
    mon = bot.MON
    now = time.time()
    hour = bot.local(now).hour
    bot.SET.set("report_hour", hour)
    mon.report_last = now - 86400
    SENT.clear()
    await mon.maybe_report(now)
    sent = [c for c in SENT if c[0] in ("sendMediaGroup", "sendMessage")]
    check(len(sent) == 2, "daily report sent to both admins at its hour")
    SENT.clear()
    await mon.maybe_report(now + 60)
    check(not SENT, "…once")
    bot.SET.set("report", "weekly")
    bot.SET.set("report_wd", bot.local(now).weekday())
    mon.report_last = now - 7 * 86400
    await mon.maybe_report(now)
    check(len([c for c in SENT if c[0] in ("sendMediaGroup", "sendMessage")]) == 2, "weekly report on its day")


async def test_certs(app, en):
    section("17) SSL certificates")
    if not make_cert(40):
        return
    await bot.sync_catalog(app, force=True)
    check(len(bot.CERTS) == 1 and bot.CERTS[0][1].startswith("example.com"), "certificate found once (nginx + Let's Encrypt folder)")
    SENT.clear()
    alerts = await bot.MON.check_certs()
    days = int((bot.MON.certs[0][2] - time.time()) // 86400)
    check(not alerts and days in (39, 40), f"expiry date read: {days} days left")
    await en.press("st")
    check("SSL: <code>example.com" in en.screen and f"<b>{days}</b> days" in en.screen, "status shows the nearest expiry")
    make_cert(3)
    alerts = await bot.MON.check_certs()
    token = bot.LANG.set("en")
    text = bot.t(alerts[0][0], **alerts[0][1]) if alerts else ""
    bot.LANG.reset(token)
    check(len(alerts) == 1 and re.search(r"expires in <b>[23]</b> days", text), "three days left → alert")
    check(not await bot.MON.check_certs(), "…once a day")


async def test_access(app):
    section("18) access control")
    intruder = Client(app, STRANGER, "en")
    logging_off()
    await intruder.say("/start")
    check("do not have access" in intruder.screen and "4242" in intruder.screen and not intruder.buttons, "a stranger is refused")
    await intruder.press("fl")
    check(not intruder.of("editMessageText") and "No access" in intruder.toast, "…buttons too")
    await intruder.say("atlas")
    check("do not have access" in intruder.screen, "…and search")
    group = Client(app, ADMIN, "en")
    await group.say("/status", chat_type="group")
    check(not group.of("sendMessage"), "an admin in a group chat gets no answer either")
    saved = set(bot.ADMIN_IDS)
    bot.ADMIN_IDS.clear()
    await intruder.say("/start")
    check("has no admin yet" in intruder.screen and "ADMIN_IDS=4242" in intruder.screen, "setup mode only tells you your id")
    await intruder.press("fl")
    check(not intruder.of("editMessageText"), "…and nothing else")
    bot.ADMIN_IDS.update(saved)
    logging_on()


async def test_trouble(app, en):
    section("20) when things go wrong")
    ctx = lambda err: types.SimpleNamespace(error=err, bot=app.bot)  # noqa: E731
    # the same token used by a second program (a second copy, or another server)
    bot._TROUBLE_AT.clear()
    SENT.clear()
    logging_off()
    for _ in range(3):
        await bot.on_error(None, ctx(Conflict("terminated by other getUpdates request")))
    logging_on()
    told = [c[1]["text"] for c in SENT if c[0] == "sendMessage"]
    check(len(told) == 2 and "Another program is using this bot" in said(),
          "one token used twice → each admin is told once, not on every poll")
    # a network hiccup while waiting for updates is the library's business
    SENT.clear()
    for _ in range(3):
        await bot.on_error(None, ctx(NetworkError("httpx.ReadError: connection reset")))
    check(not SENT, "a connection hiccup while polling: no noise in the chat, no error in the log")
    # a bug in a screen must not leave the admin with a dead button
    real_help = bot.help_view

    def broken():
        raise RuntimeError("boom")
    bot.help_view = broken
    logging_off()
    await en.press("hp")
    logging_on()
    bot.help_view = real_help
    check("did not work" in en.toast, "an internal error is answered, not swallowed")
    await en.press("hp")
    check("Commands" in en.screen, "…and the next press works again")
    # the bot's own folder cannot be written (moved after install, wrong owner, …)
    real_access = os.access
    os.access = lambda path, mode, **kw: False if str(path) == str(bot.WORK_DIR) else real_access(path, mode, **kw)
    try:
        await en.press("bi:atlas_bot")
        check("cannot write to its own work folder" in en.screen and not en.docs, "no work folder → a zip is refused with the reason")
        p = bot.PMAP["atlas_bot"]
        await en.press(f"dl:{bot.REG.put(p.key, p.root + '/config.json')}")
        check(len(en.docs) == 1 and en.docs[0][0] == "config.json", "…while a single file still downloads")
    finally:
        os.access = real_access


async def test_store():
    section("21) the history file keeps its size limit")
    path = T + "/cap.sqlite3"
    saved = (bot.DB_MAX_BYTES, bot.DB_SOFT_BYTES)
    bot.DB_MAX_BYTES, bot.DB_SOFT_BYTES = 400 * 1024, 340 * 1024
    logging_off()
    st = bot.Store(path)
    st.db.execute("PRAGMA synchronous=OFF")   # the cap is what is tested here, not durability
    for i in range(40000):
        st.add_sample((1_000_000 + i * 60, 1, 1, 1, 1, 1, 1, 1), [(1_000_000 + i * 60, "svc", 100, 1, 1)], [])
        if i % 5000 == 4999:
            st.prune(1_000_000 + i * 60)
    logging_on()
    size = os.path.getsize(path)
    lo, hi, n = st.span()
    check(size <= 400 * 1024 and hi == 1_000_000 + 39999 * 60 and n < 40000, f"40,000 samples → {size // 1024} KB, oldest dropped, newest kept")
    check(st.toggle_pin("/r", "a.txt") is True and st.pins("/r") == ["a.txt"] and st.toggle_pin("/r", "a.txt") is False, "pins")
    st.close()
    Path(path).write_bytes(b"this is not a database")
    logging_off()
    st = bot.Store(path)
    logging_on()
    st.add_event("up", "x")
    check(st.fetch_events(limit=1)[0][2] == "x", "a corrupt history file is replaced instead of crashing the bot")
    st.close()
    bot.DB_MAX_BYTES, bot.DB_SOFT_BYTES = saved


async def test_bare():
    section("24) a machine without systemd or Docker")
    bot.HOST.systemd = bot.HOST.journal = False
    bot.HOST.docker_sock = ""
    cat = await bot.scan_now()
    check({s.group for s in cat.services} == {"app", "proc"} and all(s.kind != "docker" for s in cat.services),
          "units are listed from their files, programs from /proc")
    states = await bot.poll_states(cat.services)
    check(bot.is_up(states.get("p/scraper")) and "atlas-bot" not in states, "running programs are still watched")
    bot.apply_catalog(cat)
    text, _ = await bot.services_view()
    check_html(text)
    check("scraper" in text, "the services screen still works")


async def test_plumbing():
    section("22) plumbing: real sockets and real subprocesses")
    # ExecStart parsing
    cases = {
        '/usr/bin/python3 /root/x/bot.py --fast': (["/usr/bin/python3"], ["/root/x/bot.py"]),
        '-/opt/app/bin/run --config=/etc/app/c.yml': (["/opt/app/bin/run"], ["/etc/app/c.yml"]),
        '@/usr/bin/node "/srv/my app/server.js"': (["/usr/bin/node"], []),
        "/bin/bash -c 'cd /root/shop && exec ./run.sh >> /var/log/shop.log'":
            (["/bin/bash"], ["/root/shop", "/var/log/shop.log"]),
        '/usr/bin/env FOO=/data/x python3 app.py': (["/usr/bin/env"], ["/data/x"]),
        'python3 relative.py': ([], []),
    }
    for line, want in cases.items():
        got = bot._exec_paths(line)
        check(got == want, f"ExecStart: {line[:44]}… → {got[1] or got[0]}")
    spaced = mk("/srv/my app/server.js", "x")
    check(bot._exec_paths(f'/usr/bin/node "{spaced}"') == (["/usr/bin/node"], [spaced]),
          "a quoted path with spaces is kept whole when it exists")
    # the Docker client against a real unix socket
    sock_path = T + "/docker.sock"
    if len(sock_path.encode()) > 100:      # a unix socket path may be ~107 bytes at most
        sock_dir = tempfile.mkdtemp(prefix="wr", dir="/tmp")
        atexit.register(shutil.rmtree, sock_dir, ignore_errors=True)
        sock_path = sock_dir + "/d.sock"
    answers = {}

    async def handle(reader, writer):
        request = (await reader.readuntil(b"\r\n\r\n")).decode()
        path = request.split(" ")[1]
        status, headers, body, close = answers[path]
        writer.write(f"HTTP/1.1 {status} X\r\n{headers}\r\n".encode() + body)
        await writer.drain()
        if close:
            writer.close()
        else:
            await asyncio.sleep(3)   # a server that keeps the connection open
            writer.close()
    server = await asyncio.start_unix_server(handle, sock_path)
    real_api, bot.docker_api = bot.docker_api, REAL["docker_api"]
    bot.HOST.docker_sock = sock_path
    listing = json.dumps([{"Id": "ab" * 32, "Names": ["/web"], "State": "running", "Status": "Up 2 hours",
                           "Image": "nginx", "Labels": {"com.docker.compose.project.working_dir": "/srv/x"}}]).encode()
    answers["/containers/json?all=1"] = (200, f"Content-Type: application/json\r\nContent-Length: {len(listing)}\r\n", listing, False)
    started = time.perf_counter()
    got = await bot.docker_list()
    check(got == [{"id": "ab" * 32, "name": "web", "state": "running", "status": "Up 2 hours", "image": "nginx", "dir": "/srv/x"}]
          and time.perf_counter() - started < 2.5, "container list over a real socket (stops at Content-Length)")
    chunked = b"".join(hex(len(c))[2:].encode() + b"\r\n" + c + b"\r\n" for c in (listing[:40], listing[40:])) + b"0\r\n\r\n"
    answers["/containers/json?all=1"] = (200, "Transfer-Encoding: chunked\r\n", chunked, False)
    started = time.perf_counter()
    check((await bot.docker_list())[0]["name"] == "web" and time.perf_counter() - started < 2.5, "chunked answer")
    frame = lambda s, b: bytes([s, 0, 0, 0]) + len(b).to_bytes(4, "big") + b  # noqa: E731
    answers["/containers/web/logs?stdout=1&stderr=1&timestamps=1&tail=5"] = (200, "", frame(1, b"out line\n") + frame(2, b"err line\n"), True)
    check(await bot.docker_logs("web", 5) == "out line\nerr line", "log stream is de-multiplexed")
    answers["/containers/web/logs?stdout=1&stderr=1&timestamps=1&tail=5"] = (200, "", b"plain tty output\n", True)
    check(await bot.docker_logs("web", 5) == "plain tty output", "a TTY container's raw log")
    answers["/containers/gone/json"] = (404, "Content-Length: 2\r\n", b"{}", True)
    st = await bot.docker_state(bot.Svc("d/gone", "docker", "gone", "docker"))
    check(st is not None and st.state == "removed", "a removed container is reported as such")
    server.close()
    await server.wait_closed()
    check(await bot.docker_list() is None, "daemon not answering → None (the old picture is kept)")
    bot.docker_api = real_api
    # real subprocesses: a stand-in systemctl / journalctl on PATH
    tools = T + "/fakebin"
    os.makedirs(tools, exist_ok=True)
    Path(tools + "/systemctl").write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = show ]; then\n"
        "printf 'Id=nginx.service\\nLoadState=loaded\\nActiveState=active\\nSubState=running\\n"
        "UnitFileState=enabled\\nMemoryCurrent=14000000\\nNRestarts=0\\nDescription=A=B web\\n\\n"
        "Id=ghost.service\\nLoadState=not-found\\nActiveState=inactive\\n\\n"
        "Id=wg-quick@wg0.service\\nLoadState=loaded\\nActiveState=active\\nSubState=exited\\nMemoryCurrent=[not set]\\n'\n"
        "fi\n")
    Path(tools + "/journalctl").write_text(
        "#!/bin/sh\nprintf 'ok line\\nERROR bad\\nTraceback (most recent call last):\\nWARNING hmm\\n[warn] x\\nlast'\n")
    os.chmod(tools + "/systemctl", 0o755)
    os.chmod(tools + "/journalctl", 0o755)
    old_path = os.environ["PATH"]
    os.environ["PATH"] = tools + os.pathsep + old_path
    real_run, bot.run_cmd = bot.run_cmd, REAL["run_cmd"]
    try:
        units = await REAL["read_units"](("nginx", "ghost", "sshd", "wg-quick@wg0"))
        check(set(units) == {"nginx", "ghost", "wg-quick@wg0"} and units["nginx"]["Description"] == "A=B web"
              and units["ghost"]["LoadState"] == "not-found", "systemctl output is parsed (an alias simply has no entry)")
        st = bot.unit_state(units["nginx"], time.time(), time.monotonic() * 1e6)
        check(st.state == "active" and st.mem == 14000000 and bot.unit_state(units["wg-quick@wg0"], 0, 0).mem is None,
              "unit state, including '[not set]' values")
        check(await bot.count_log("x", 0, 1) == (5, 2, 2), "journal lines, errors and warnings are counted without keeping the text")
        check(await bot.run_cmd("definitely-not-a-command") == "(FileNotFoundError)", "a missing tool is not an error")
        check(await bot.run_cmd("sleep", "5", timeout=0.3) == "(TimeoutError)", "a hanging command is cut off")
    finally:
        os.environ["PATH"] = old_path
        bot.run_cmd = real_run


async def test_cancel(app, en):
    section("19) cancelling a job")
    slow = T + "/root/scraper/slow.bin"
    Path(slow).write_bytes(os.urandom(600 * 1024))
    saved = bot.TG_PART_SIZE
    bot.TG_PART_SIZE = 64 * 1024
    p = bot.PMAP["scraper"]
    real_send = bot.send_doc
    sent = []

    async def slow_send(botobj, chat_id, path, filename, caption=None, offset=0, length=None, job=None):
        sent.append(filename)
        if len(sent) == 2:
            await Client(app, ADMIN, "en").press("cx")
        await asyncio.sleep(0.05)
    bot.send_doc = slow_send
    await en.press(f"dl:{bot.REG.put(p.key, slow)}")
    check(2 <= len(sent) <= 3 and bot.JOB is None and os.listdir(bot.WORK_DIR) == [], f"download stopped after part {len(sent)} of 10")
    sent.clear()
    await en.press("bf:scraper")
    check(2 <= len(sent) <= 3 and bot.JOB is None and os.listdir(bot.WORK_DIR) == [],
          "zip stopped mid-way; the half-written part is removed")
    bot.send_doc = real_send
    # a second job cannot start while one is running
    gate = asyncio.Event()

    async def held(botobj, chat_id, path, filename, caption=None, offset=0, length=None, job=None):
        await gate.wait()
    bot.send_doc = held
    first = asyncio.ensure_future(Client(app, ADMIN, "en").press(f"dl:{bot.REG.put(p.key, slow)}"))
    await asyncio.sleep(0.1)
    other = Client(app, ADMIN2, "fa")
    await other.press("bf:scraper")
    check("یک عملیات دیگر" in other.toast and not other.docs, "one heavy job at a time")
    gate.set()
    await first
    bot.send_doc = real_send
    bot.TG_PART_SIZE = saved
    os.remove(slow)


def test_optional_libs():
    section("23) optional big-file library")
    try:
        import inspect
        from telethon import TelegramClient
    except ImportError:
        print("       (Telethon is not installed here: signature checks skipped)")
        return
    init = inspect.signature(TelegramClient.__init__).parameters
    check({"receive_updates", "flood_sleep_threshold", "connection_retries", "request_retries", "proxy"} <= set(init),
          "Telethon's client accepts the options the bot passes")
    check({"file_size", "file_name", "progress_callback"} <= set(inspect.signature(TelegramClient.upload_file).parameters)
          and {"caption", "force_document", "parse_mode"} <= set(inspect.signature(TelegramClient.send_file).parameters)
          and "bot_token" in inspect.signature(TelegramClient.sign_in).parameters,
          "upload_file / send_file / sign_in have the parameters the bot uses")
    check(bot._mt_proxy() is None, "no proxy configured")
    bot.PROXY_URL = "socks5://user:pa55@127.0.0.1:1080"
    check(bot._mt_proxy() == {"proxy_type": "socks5", "addr": "127.0.0.1", "port": 1080, "rdns": True,
                              "username": "user", "password": "pa55"}, "proxy URL in the library's format")
    bot.PROXY_URL = ""


class StandIn:
    """A local stand-in for the Telegram Bot API: enough of it for the real bot process to
    start, receive updates and answer them, uploads included. Listens on 127.0.0.1 only."""

    def __init__(self, reject=False):
        self.reject = reject
        self.calls = []                  # (method, parameters)
        self.files = []                  # (method, file name, content) of every upload
        self.pending = []                # updates waiting for the bot's next getUpdates
        self.changed = asyncio.Event()   # set after every call; wait_for() watches it
        self.update_id = 0
        self.server = None
        self.url = ""

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0, limit=1 << 20)
        self.url = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"
        return self

    async def stop(self):
        self.server.close()
        with bot.suppress(Exception):
            await asyncio.wait_for(self.server.wait_closed(), 5)

    # ── what the "user" does ──
    def say(self, text):
        self.update_id += 1
        ents = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}] if text.startswith("/") else []
        self.pending.append({"update_id": self.update_id, "message": {
            "message_id": 10 + self.update_id, "date": int(time.time()), "text": text, "entities": ents,
            "chat": {"id": ADMIN, "type": "private", "first_name": "Admin"},
            "from": {"id": ADMIN, "is_bot": False, "first_name": "Admin", "language_code": "en"}}})

    def press(self, data):
        self.update_id += 1
        self.pending.append({"update_id": self.update_id, "callback_query": {
            "id": str(self.update_id), "chat_instance": "c", "data": data,
            "from": {"id": ADMIN, "is_bot": False, "first_name": "Admin", "language_code": "en"},
            "message": {"message_id": 7, "date": int(time.time()), "text": "x",
                        "chat": {"id": ADMIN, "type": "private", "first_name": "Admin"}}}})

    def texts(self):
        return [p.get("text", "") for m, p in self.calls if m in ("sendMessage", "editMessageText")]

    async def wait_for(self, done, seconds=90):
        """Wait until done() is true (it is looked at again after every call of the bot)."""
        end = time.monotonic() + seconds
        while not done():
            left = end - time.monotonic()
            if left <= 0:
                return False
            self.changed.clear()
            with bot.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.changed.wait(), min(left, 1.0))
        return True

    # ── what "Telegram" answers ──
    def answer(self, method, params):
        if method == "getMe":
            return {"id": 123456, "is_bot": True, "first_name": "Wrench", "username": "wrench_e2e_bot",
                    "can_join_groups": False, "can_read_all_group_messages": False, "supports_inline_queries": False}
        if method == "getUpdates":
            out, self.pending = self.pending, []
            return out
        if method in ("sendMessage", "editMessageText", "sendDocument", "sendPhoto"):
            return {"message_id": 100 + len(self.calls), "date": int(time.time()), "text": "ok",
                    "chat": {"id": int(params.get("chat_id", ADMIN)), "type": "private"}}
        return True

    async def read_request(self, reader):
        head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
        first, *lines = head.split("\r\n")
        headers = {}
        for line in lines:
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
        if "chunked" in headers.get("transfer-encoding", "").lower():
            body = b""
            while True:
                size = int((await reader.readline()).split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    await reader.readuntil(b"\r\n")
                    break
                body += await reader.readexactly(size)
                await reader.readexactly(2)
        else:
            size = int(headers.get("content-length", "0") or 0)
            body = await reader.readexactly(size) if size else b""
        return first.split()[1].rsplit("/", 1)[-1].split("?")[0], headers, body

    def parse(self, method, headers, body):
        ctype = headers.get("content-type", "")
        if not ctype.startswith("multipart/"):
            return {k: v[0] for k, v in urllib.parse.parse_qs(body.decode("utf-8", "replace")).items()}
        import email
        params = {}
        message = email.message_from_bytes(f"Content-Type: {ctype}\r\n\r\n".encode() + body)
        for part in message.get_payload():
            name = part.get_param("name", header="content-disposition")
            content = part.get_payload(decode=True)
            if part.get_filename():
                self.files.append((method, part.get_filename(), content))
            else:
                params[name] = content.decode("utf-8", "replace")
        return params

    async def handle(self, reader, writer):
        try:
            while True:
                method, headers, body = await self.read_request(reader)
                params = self.parse(method, headers, body)
                self.calls.append((method, params))
                if self.reject:
                    status, doc = "401 Unauthorized", {"ok": False, "error_code": 401, "description": "Unauthorized"}
                else:
                    if method == "getUpdates" and not self.pending:
                        await asyncio.sleep(0.2)       # a (very) short long-poll
                    status, doc = "200 OK", {"ok": True, "result": self.answer(method, params)}
                self.changed.set()
                payload = json.dumps(doc).encode()
                writer.write(f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
                             f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.LimitOverrunError):
            pass
        finally:
            writer.close()


E2E_TOKEN = "123456:E2E-TOKEN-not-a-real-one-0000000000000"


async def run_bot(home, script=None, command=None, **settings):
    """Start `python bot.py` in its own folder with a clean environment; returns
    (exit code, log). With a script (a coroutine function) the bot is left running until the
    script is through, then stopped with SIGINT — the way systemd stops the service.
    `command` puts a wrapper in front of the interpreter."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "NO_PROXY": "127.0.0.1,localhost",
           "no_proxy": "127.0.0.1,localhost", "WRENCH_ENV_FILE": os.devnull, "LANGUAGE": "en",
           "PYTHONDONTWRITEBYTECODE": "1"}
    env.update(settings)
    proc = await asyncio.create_subprocess_exec(
        *(command or []), sys.executable, str(home / "bot.py"), cwd=str(home), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    chunks = []

    async def drain():
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                return
            chunks.append(chunk)
    reader = asyncio.ensure_future(drain())
    if script is not None:
        acting = asyncio.ensure_future(script())
        ended = asyncio.ensure_future(proc.wait())
        await asyncio.wait({acting, ended}, timeout=180, return_when=asyncio.FIRST_COMPLETED)
        acting.cancel()
        await asyncio.gather(acting, return_exceptions=True)
        if proc.returncode is None:
            proc.send_signal(signal.SIGINT)
    try:
        await asyncio.wait_for(proc.wait(), 60)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        await reader
        return -999, b"".join(chunks).decode("utf-8", "replace")
    await reader
    return proc.returncode, b"".join(chunks).decode("utf-8", "replace")


def make_demo_project(parent):
    """A small project for the real process to find and back up."""
    demo = parent / "wrench_e2e_demo"
    (demo / "data").mkdir(parents=True)
    (demo / "venv" / "lib").mkdir(parents=True)
    (demo / ".git").mkdir()
    (demo / "app.py").write_text("print('hello')\n")
    (demo / ".env").write_text("SECRET=not-for-the-log\n")
    (demo / "venv" / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (demo / "venv" / "lib" / "big_library.py").write_text("x = 1\n" * 1000)
    (demo / ".git" / "config").write_text("[core]\n")
    blob = os.urandom(3 * 1024 * 1024 + 123)          # incompressible: the upload is ~3 MB
    (demo / "data" / "blob.bin").write_bytes(blob)
    db = sqlite3.connect(demo / "data" / "app.sqlite3")
    db.execute("create table u(id integer primary key, name text)")
    db.executemany("insert into u values(?, ?)", [(i, f"user{i}") for i in range(500)])
    db.commit()
    db.close()
    return demo, blob


async def test_process():
    section("25) the real process, against a local stand-in for Telegram")
    home = Path(T) / "opt" / "wrench-e2e"
    home.mkdir(parents=True)
    shutil.copy(REPO / "bot.py", home / "bot.py")
    projects = Path(T) / "e2e_projects"
    demo, blob = make_demo_project(projects)
    # 1) a normal life: start, look at this machine, answer, send a backup, stop on SIGINT
    api = await StandIn().start()
    api.say("/start")
    seen = {}

    async def admin():
        seen["menu"] = await api.wait_for(lambda: any("Choose" in x for x in api.texts()))
        api.say("/status")
        seen["status"] = await api.wait_for(lambda: any("Server status" in x for x in api.texts()))
        api.press("bf:wrench_e2e_demo")
        seen["zip"] = await api.wait_for(lambda: bool(api.files))
        await api.wait_for(lambda: any(m == "deleteMessage" for m, _ in api.calls), 20)

    code, out = await run_bot(home, admin, BOT_TOKEN=E2E_TOKEN, ADMIN_IDS=str(ADMIN), BOT_API_URL=api.url,
                              SCAN_PATHS=str(projects))
    await api.stop()
    sent = api.texts()
    if code != 0 or not all(seen.get(k) for k in ("menu", "status", "zip")):
        print(out)
    check(seen.get("menu") and any("Server Wrench" in x and "Choose" in x for x in sent),
          "the real process starts, scans this very machine and answers /start with the menu")
    check(any("is now watching this server" in x for x in sent), "…after telling the admin what it found here")
    status = next((x for x in sent if "Server status" in x), "")
    check(seen.get("status") and "CPU" in status and "RAM" in status and "Disk" in status and "This bot" in status,
          "/status shows this machine's real figures")
    check(seen.get("zip") and len(api.files) == 1 and api.files[0][0] == "sendDocument"
          and api.files[0][1].startswith("wrench_e2e_demo_full_") and api.files[0][1].endswith(".zip"),
          "a project found through SCAN_PATHS is backed up: one zip arrives over real HTTP")
    zf = zipfile.ZipFile(io.BytesIO(api.files[0][2]))
    check(zf.testzip() is None and sorted(zf.namelist()) == sorted(
        "wrench_e2e_demo/" + n for n in (".env", "app.py", "data/app.sqlite3", "data/blob.bin")),
        "the zip is valid: project files in, virtualenv and .git left out")
    snap = Path(T) / "e2e_snapshot.sqlite3"
    snap.write_bytes(zf.read("wrench_e2e_demo/data/app.sqlite3"))
    copy = sqlite3.connect(snap)
    rows = copy.execute("select count(*) from u").fetchone()[0]
    copy.close()
    check(rows == 500 and zf.read("wrench_e2e_demo/data/blob.bin") == blob,
          "its contents are byte-for-byte right (3 MB streamed from disk, database intact)")
    check(code == 0 and "started as @wrench_e2e_bot" in out and "Traceback" not in out, "SIGINT → a clean stop, exit code 0, no error in the log")
    check(sorted(os.listdir(home)) == [".work", "bot.py", "data"] and os.listdir(home / ".work") == []
          and os.listdir(home / "data") == ["monitor.sqlite3"],
          "on disk afterwards: the history file and an empty work folder — nothing else")
    check(E2E_TOKEN.partition(":")[2] not in out and "not-for-the-log" not in out, "neither the token nor file contents appear in the log")
    # 2) a token Telegram rejects: say so once and stay stopped (exit code 78)
    api = await StandIn(reject=True).start()
    code, out = await run_bot(home, BOT_TOKEN=E2E_TOKEN, ADMIN_IDS=str(ADMIN), BOT_API_URL=api.url)
    await api.stop()
    check(code == bot.EX_CONFIG == 78 and "does not accept BOT_TOKEN" in out and E2E_TOKEN.partition(":")[2] not in out,
          "rejected token → a clear line, exit code 78 (systemd does not restart), token not printed")
    # 3) Telegram cannot be reached: exit 1, so that the service manager tries again
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead = f"http://127.0.0.1:{probe.getsockname()[1]}"
    code, out = await run_bot(home, BOT_TOKEN=E2E_TOKEN, ADMIN_IDS=str(ADMIN), BOT_API_URL=dead)
    check(code == 1 and "Telegram cannot be reached" in out and "PROXY_URL" in out,
          "Telegram unreachable → a hint about PROXY_URL and exit code 1 (the service is restarted)")
    # 4) no token at all
    code, out = await run_bot(home, ADMIN_IDS=str(ADMIN))
    check(code == 78 and "BOT_TOKEN is missing" in out, "no token → exit code 78 with the reason")


REAL = {}


async def main():
    bot.log.addHandler(_Catch())
    build_server()
    retarget()
    REAL.update(read_units=bot.read_units, docker_api=bot.docker_api, run_cmd=bot.run_cmd)
    bot.read_units = fake_read_units
    bot.docker_api = fake_docker_api
    bot.run_cmd = fake_run_cmd
    test_static()
    test_host()
    await test_discovery()
    app = await test_startup()
    en, fa = await test_menus(app)
    await test_files(app, en)
    await test_backup(app, en)
    await test_databases(app, en)
    await test_split_and_big(app, en)
    await test_search(app, en, fa)
    await test_services(app, en, fa)
    await test_disk_procs(app, en)
    await test_reports(app, en, fa)
    await test_monitor(app, en)
    await test_changes(app, en)
    await test_settings(app, en, fa)
    await test_certs(app, en)
    await test_access(app)
    await test_cancel(app, en)
    await test_trouble(app, en)
    await test_store()
    await test_plumbing()
    test_optional_libs()
    await test_bare()
    await bot.post_stop(app)
    check(os.listdir(bot.WORK_DIR) == [], "after shutdown the work folder is empty")
    await app.shutdown()
    await test_process()


if __name__ == "__main__":
    asyncio.run(main())
    print(f"\nALL {PASSED} CHECKS PASSED in {time.time() - NOW:.0f} s")

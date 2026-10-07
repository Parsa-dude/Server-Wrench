<div align="center">

# 🧰 Server Wrench

**The pocket wrench for your Linux server — inside Telegram.**

A read-only bot that works out by itself what runs on a server, watches it,<br>
and hands you files, backups, logs and reports whenever you ask.

**English** · [فارسی](README.fa.md)

[![tests](https://github.com/Parsa-dude/Server-Wrench/actions/workflows/tests.yml/badge.svg)](https://github.com/Parsa-dude/Server-Wrench/actions/workflows/tests.yml)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-3776ab)
![Linux](https://img.shields.io/badge/platform-linux-informational)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

</div>

<p align="center">
  <img src="docs/report-1.png" width="32%" align="top" alt="Weekly report: overview">
  <img src="docs/report-2.png" width="32%" align="top" alt="Weekly report: peak hours">
  <img src="docs/report-3.png" width="32%" align="top" alt="Weekly report: services">
</p>
<p align="center"><sub>The three images of a weekly report. Drawn by the bot's own chart code from demo data
(<a href="docs/make_samples.py">docs/make_samples.py</a>).</sub></p>

---

## Why a wrench?

A wrench does not need to know which machine it will be used on. It fits the nut in
front of it, it is in your pocket when something comes loose, and it never changes the
machine on its own.

That is the idea of this bot:

- **It fits any server.** There is no list of projects or services to maintain. You
  give it a bot token and your Telegram ID; it looks at the machine and finds the rest.
  Deploy something new next week and it shows up by itself.
- **It is always in your pocket.** Status, a log, a file, a full backup, a weekly
  report: one tap or one command, from your phone, at any moment.
- **It only reads.** It never edits, restarts or deletes anything. With the service
  file that ships here, the operating system itself enforces that.
- **It is precise.** Figures are the ones `df`, `systemctl` and `/proc` give. A
  database inside a backup is a consistent snapshot, not a file copied mid-write. A
  service is reported down after two failed checks, not on a hiccup.

## What you get

| | |
|---|---|
| 📁 **Files** | Browse every project, preview text files (values in `.env` are masked), download anything, collect files and folders in a basket and send them together. |
| 🔎 **Search** | Type part of a name at any time. Typos are forgiven (`confg` finds `config.json`), several words narrow it down, and you can search inside files as well. |
| 📦 **Backup** | A whole project, only its essentials, hand-picked files, several projects at once. Built as a stream and sent to your chat; nothing is stored on the server. |
| 📊 **Status** | CPU, RAM, swap, disks, load, uptime, what is down — one screen. |
| ⚙️ **Services** | systemd services, Docker containers and programs started by hand (nohup, screen, tmux, pm2), each with state, uptime, memory, logs, errors only, log file, unit file. |
| 💽 **Disk** | Every filesystem as a gauge, "what takes the space?" per project and per usual suspect (logs, Docker, caches), as text or as a chart. |
| 🧮 **Processes** | Who uses the CPU and the memory right now, and which service or project it belongs to. |
| 📈 **Reports** | Charts for 24 hours, 7 or 30 days: usage over time, peak hours, a weekday × hour heat map, per-service uptime, crashes and log errors. Also on a schedule, daily or weekly. |
| 🔔 **Alerts** | A service goes down or comes back, a crash loop, an unhealthy container, a disk or the RAM filling up, an SSL certificate about to expire, a new project or service appearing. |
| 🧾 **Events** | What happened to the services, and which admin fetched what. |

Everything is available in **English and Persian**; each admin picks their own language.

<details>
<summary><b>What it looks like in the chat</b> (text of the messages, demo data)</summary>

```
🧰 Server Wrench · demo-server
Read-only: browse, back up and watch — nothing on the server is changed.

⚙️ Services: 7 of 8 running · 🔴 1 in trouble

Choose 👇
[ 📁 Files    ]  [ 🔎 Search     ]
[ 📦 Backup   ]  [ 🧺 Basket (0) ]
[ 📊 Status   ]  [ ⚙️ Services   ]
[ 💽 Disk     ]  [ 🧮 Processes  ]
[ 📈 Reports  ]  [ 🧾 Events     ]
[ 🛠 Settings ]  [ ❔ Help       ]
```

```
📊 Server status
demo-server · Ubuntu 24.04.1 LTS
KVM · Hetzner · 2 vCPU · 3.8 GB RAM
⏱ Up 5d · 🕒 21:20 Europe/Amsterdam
CPU  ██░░░░░░░░  18.4%
RAM  ██████░░░░  63.0%
Swap ░░░░░░░░░░   5.0%
Disk ██████░░░░  62.3%  /
⚙️ Load: 0.42 · 0.38 · 0.30 (2 cores)
🧠 RAM: 2.4 GB of 3.8 GB · swap 97.7 MB of 1.9 GB
💽 /: 47.3 GB of 80.0 GB · 28.6 GB free

⚙️ Services: 🟢 7 of 8 running · 🔴 1 down
🔴 ⚙️ worker — failed (exit-code)
🌐 Sites: 2 (3 domains)

🧰 This bot: 58.3 MB RAM · 0.1% CPU · history 1.2 MB of 10.0 MB
```

```
⚙️ Services — 🟢 7 · 🔴 1 · ⚪️ 0

Your apps
🟢 ⚙️ shop-api · 5d 2h · 205.0 MB
🟢 ⚙️ telegram-bot · 5d 2h · 82.4 MB
🔴 ⚙️ worker — failed (exit-code)

System services
🟢 ⚙️ nginx · 5d 2h · 17.7 MB
🟢 ⚙️ postgresql · 5d 2h · 371.7 MB
🟢 ⚙️ redis-server · 5d 2h · 21.5 MB

Containers
🟢 🐳 shop-web · 1d 13h

Running without a service
🟢 ▶️ scraper · 2d · 58.7 MB
```

Alerts:

> 🔴 ⚙️ `worker` is down (`failed/failed/exit-code`).<br>
> 🟢 ⚙️ `worker` is running again.<br>
> ♻️ ⚙️ `shop-api` crashed and was restarted automatically (restart #1).<br>
> 💽 Disk `/mnt/data` is **91%** full (18.0 GB free).<br>
> 🔐 The SSL certificate of `shop.example.com` expires in **7** days (2026-10-12).

The caption under a weekly report:

> 📈 **Weekly server report**<br>
> 🗓 2026-09-28 00:00 → 2026-10-05 00:00 · UTC
>
> **Resources**<br>
> ⚙️ CPU: avg **15.7%** · peak **96%** (Friday 21:14)<br>
> 🧠 RAM: avg **63%** · peak **84%** (Friday 21:06)<br>
> 💽 Disk: **62.6%** (+1.4 pt)<br>
> 🌐 Traffic: ⬇️ **4.7 GB** · ⬆️ **11.4 GB**
>
> **Rhythm**<br>
> 🔥 Peak hour: **20:00–21:00** (CPU 38.5%)<br>
> 🌙 Quietest hour: 03:00–04:00<br>
> 📍 Busiest slot: **Sunday 20:00**<br>
> 📡 Traffic peak: 20:00–21:00<br>
> 🌍 Site requests: **157,206** · peak 20:00–21:00 · 5xx errors: 42
>
> **Services**<br>
> 🟢 Availability: **99.96%**<br>
> ♻️ Crashes: **3** (worker ×2, shop-api ×1) · 🔁 manual restarts: **1**<br>
> ❗️ Errors in logs: **162** (worker ×112, shop-api ×37, shop-web (docker) ×9)
>
> 📊 Data coverage: 100% (10,080 samples)

<p align="center"><img src="docs/disk.png" width="48%" alt="Disk usage chart"></p>

</details>

## It configures itself

After the first start the bot sends you a short summary of what it found. From then on
it looks again every five minutes, and whenever you open the menu. What it reads:

| Source | What it tells |
|---|---|
| Unit files in `/etc/systemd/system` | Your own services, and the folder each one runs from (`WorkingDirectory`, `ExecStart`). |
| Packaged services that are installed and in use | nginx, Apache, MySQL/MariaDB, PostgreSQL, Redis, Docker, php-fpm, cron, ssh and other usual ones. |
| Running processes (`/proc`) | Which folder a program runs from and which unit it belongs to. This is how programs started with `nohup`, `screen`, `tmux` or `pm2` are found: after two minutes of running, such a program is watched like a service. |
| Docker (or Podman) socket | Containers, their state and health, and the compose folder they come from. |
| nginx, Apache and Caddy configuration | Sites, their domains, document roots and certificates. |
| Folders | Anything under `/root`, `/home/*`, `/srv`, `/var/www` and a few more that looks like a project (source files, a `Dockerfile`, `package.json`, …). Folders elsewhere — `/opt`, `/usr/local` — are listed when a service or a program runs from them. |
| The machine itself | Distribution, virtualisation (KVM, VMware, Hyper-V, LXC, Docker, …) and, where it can be recognised, the provider (Hetzner, DigitalOcean, AWS, Google Cloud, Azure, OVH, Vultr, Linode, Contabo and others). |

Nothing about any particular server is built in. If the automatic picture ever needs a
nudge, a few optional settings exist (`SCAN_PATHS`, `EXTRA_PATHS`, `IGNORE_DIRS`,
`IGNORE_SERVICES`), and from inside the bot you can rename or hide a project, mute a
service's alerts and mark files as essential (⭐).

## It only reads

- The bot offers no way to edit, delete, restart or execute anything on the server. It
  can only read, and send what it read to the admins.
- It runs as root, because that is what reading every project takes — and that is why
  the service file written by `install.sh` takes everything else away, making
  "read-only" a property of the system and not just of the code: for the bot's process
  **the whole filesystem is mounted read-only** (`ProtectSystem=strict`), except its
  own `data/` and `.work/` folders, and it keeps only two capabilities — reading files
  regardless of their owner, and reading the process table.
- Only the Telegram accounts in `ADMIN_IDS` get an answer, and only in a private chat.
  Anyone else is told their own ID and nothing more.
- On disk it keeps one small history file (`data/monitor.sqlite3`, capped at 10 MB by
  default) and, during a backup, at most one part of the zip in `.work/`, deleted as
  soon as it is sent. The work folder is emptied after every job and on every start.
- The bot token and the API hash never appear in the log, whatever writes the line.

Two things to keep in mind, because they follow from what the tool is for:

- **Whoever controls an admin's Telegram account can read every file on the server**,
  secrets included. Protect those accounts with two-step verification, and keep
  `ADMIN_IDS` short.
- **Backups are files in a Telegram chat.** They are as private as that chat.

## Light on the server

- Once a minute it takes one sample: a few small files in `/proc`, one `systemctl show`
  call for all watched units together, and the Docker socket.
- Once an hour it counts the log lines of the past hour (lines, errors, warnings — the
  text is not kept) and prunes its history.
- The service runs at the lowest CPU and I/O priority (`Nice=10`,
  `IOSchedulingClass=idle`, `CPUWeight=20`), with a memory ceiling (`MemoryMax=400M`),
  and is the first process to go if the machine ever runs out of memory.
- Backups are streamed: a file is never loaded into memory, a zip never exists in one
  piece on disk, and a job is refused up front when free disk space is short.

Measured on a 2-vCPU test VM with Python 3.13: 50–70 MB of memory, and for the
once-a-minute sample 3 ms of CPU on a machine with about 20 processes, 25 ms with 300,
85 ms with 1,000 — between 0.01% and 0.2% of one core. (The bot's own work only; the
`systemctl` helper it starts once a minute on systemd machines comes on top.) The
Status screen shows the bot's own memory and CPU use, so you can check on yours.

## Install

**You need:** a Linux server with root access, Python 3.9 or newer (the installer gets
it if it is missing), and a bot token from [@BotFather](https://t.me/BotFather).
**One bot per server:** two servers must not share a token.

```bash
git clone https://github.com/Parsa-dude/Server-Wrench.git /opt/server-wrench
cd /opt/server-wrench
sudo bash install.sh
```

(No `git` on the server? `apt install -y git`, or download the repository as a zip and
unpack it to `/opt/server-wrench`.)

The installer asks for the token and your numeric Telegram ID, creates a virtual
environment, writes `.env` (readable by root only) and a systemd service, starts it and
checks that it stays up. Then open your bot in Telegram and send `/start`.

Don't know your Telegram ID? Leave it empty: the bot tells it to whoever sends
`/start` while no admin is set. Put it in `.env` and run `systemctl restart server-wrench`.

```bash
sudo bash install.sh --status      # state and the last log lines
sudo bash install.sh --update      # git pull, update packages, restart
sudo bash install.sh --uninstall   # stop and remove the service (folder and data stay)

# unattended
BOT_TOKEN=123456789:AA... ADMIN_IDS=111,222 bash install.sh --yes
```

Options: `--big-files` (also install the big-file library), `--no-sandbox` (leave the
read-only sandbox out of the service file), `--no-start`.

<details>
<summary><b>Manual setup</b>, or a machine without systemd</summary>

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env      # fill in BOT_TOKEN and ADMIN_IDS
venv/bin/python bot.py
```

A reference service file is in [`deploy/server-wrench.service`](deploy/server-wrench.service).
Without systemd the bot still watches containers and running programs; start it with
whatever supervisor the machine has.

</details>

**Where it runs:** any Linux with Python 3.9 or newer. The installer is for systemd
distributions and knows `apt`, `dnf`, `yum`, `zypper` and `pacman`; Ubuntu 22.04+,
Debian 11+, RHEL / AlmaLinux / Rocky 8+, Fedora, openSUSE, Arch and Amazon Linux 2023
have what it needs (Ubuntu 20.04 ships Python 3.8 — install a newer one first). The
code is tested automatically on Python 3.9 to 3.14. It has not been run on every
distribution in that list, so reports of what you find are welcome.

## Settings

All in `.env` next to `bot.py` (real environment variables win). Only the first two
are needed.

| Setting | Default | Meaning |
|---|---|---|
| `BOT_TOKEN` | — | Token from @BotFather. |
| `ADMIN_IDS` | — | Telegram user IDs that may use the bot, comma separated. |
| `LANGUAGE` | `auto` | `auto`, `en` or `fa`. Language of alerts and of admins who have not chosen one. |
| `TIMEZONE` | server's | Time zone for reports and schedules, e.g. `Europe/Amsterdam`. |
| `API_ID`, `API_HASH` | — | Enable big-file mode (below). |
| `BIG_PART_MB` | `1900` | Part size in big-file mode. |
| `EXTRA_PATHS` | — | Extra folders to offer for browsing, e.g. `/var/log,/backup`. |
| `SCAN_PATHS` | — | Extra parent folders whose sub-folders are checked for projects. Also works for places that are ignored otherwise, such as `/var/lib/apps`. |
| `IGNORE_DIRS` | — | Folder names that are never projects. |
| `IGNORE_SERVICES` | — | Services to leave out; containers are written `d/<name>`. |
| `SYSTEM_CONFIGS` | `on` | The "Config folders" group (`/etc/nginx`, unit files, crontabs, …). |
| `MAX_JOB_MB` | `4096` | Largest backup or file one job may send. |
| `MIN_FREE_DISK_MB` | `500` | Free disk space that is always left untouched. |
| `DATA_MAX_MB` | `10` | Ceiling of the monitoring history. |
| `PROXY_URL` | — | Proxy for reaching Telegram (`http://…` or `socks5://…`). |
| `BOT_API_URL`, `BOT_API_LIMIT_MB` | — / `50` | Your own Bot API server and its upload limit. |
| `BUTTON_COLORS` | `on` | Coloured buttons; older Telegram apps simply show them plain. |
| `LOG_LEVEL` | `INFO` | |

Alert thresholds, the report schedule, the first day of the week and the language are
changed inside the bot, under 🛠 Settings.

## Big files

Bots may upload 50 MB per file. Without further setup anything larger arrives in
numbered parts (`name.zip.001`, `.002`, …) that 7-Zip, or `cat name.zip.* > name.zip`,
puts back together.

With an `api_id` and `api_hash` from <https://my.telegram.org> (→ *API development
tools*) the bot can send **one file of up to 2 GB**, and larger things in 1.9 GB parts:

```bash
sudo bash install.sh --big-files        # asks for the two values and installs Telethon
```

or put `API_ID` and `API_HASH` in `.env` and run
`venv/bin/pip install -r requirements-bigfiles.txt cryptg`. The bot still logs in as
the same bot; no personal account is involved. If the big-file channel is unavailable
the upload falls back to parts. A [local Bot API server](https://github.com/tdlib/telegram-bot-api)
is the other way to raise the limit (`BOT_API_URL`, `BOT_API_LIMIT_MB`).

## Commands

The menu covers everything; the commands are shortcuts.

| | |
|---|---|
| `/start`, `/menu` | Main menu |
| `/status` | Server status |
| `/services` | Services, containers, programs |
| `/logs <name>` | Last log lines of a service |
| `/disk` | Disk space |
| `/top` | Busiest processes |
| `/report [days]` | Report with charts, e.g. `/report 7` (1 to 35 days) |
| `/find <words>` | Find a file — or just type the words without a command |
| `/backup` | Back up a project |
| `/events` | Recent events |
| `/settings` | Settings |
| `/id` | Your Telegram ID |

## Backups in detail

- **Whole project** takes everything except virtual environments, `node_modules`,
  `.git`, caches and compiled Python.
- **Essentials** are source code, configuration and small data files (up to 25 MB
  each, three folder levels deep), plus any file you marked ⭐.
- **SQLite databases** — recognised by their content, whatever the file is called — go
  in as one consistent snapshot made with SQLite's own backup API, opened read-only.
  Nothing is created next to your database. When a snapshot is not possible (a
  write-ahead log left by a crash, a database that is written without pause) the file
  is copied as found together with its `-wal` / `-journal` file, and the caption of
  the backup says so.
- A zip is written as a stream in parts; each part is sent and deleted before the next
  one is written. Free disk space is checked before a job starts.
- One job runs at a time, shows its progress and can be cancelled.

## Alerts

| Alert | When |
|---|---|
| 🔴 down / 🟢 running again | A service, container or watched program fails two checks in a row (about two minutes). |
| ♻️ crashed and restarted | systemd restarted it automatically; at most one alert per service in 30 minutes. |
| 🟠 unhealthy | A container's health check fails. |
| 💽 disk | A filesystem reaches the limit (90% by default), once a day per filesystem. |
| 🧠 RAM | Memory stays above the limit (95% by default) for five minutes. |
| 🔐 SSL | 14, 7, 3, 2 and 1 day before a certificate expires, and when it has. |
| 🆕 new | A project or service appears on the server. |

Alerts can be switched off as a whole or muted per service.

## Troubleshooting

| | |
|---|---|
| The service does not start, the log mentions `NAMESPACE` | The read-only sandbox is not available on this system (some containers). Run `sudo bash install.sh --no-sandbox`. |
| `Telegram does not accept BOT_TOKEN` | The token in `.env` is wrong. The service stays stopped (exit code 78) until you fix it and start it again. |
| `Telegram cannot be reached` | No route to Telegram from this server. Set `PROXY_URL`; for `socks5://` also run `venv/bin/pip install "python-telegram-bot[socks]"`. |
| "Another program is using this bot's token" | Two copies run with one token, or the token is used on another server. Every server needs its own bot. |
| Reports arrive without images | Pillow is missing: `venv/bin/pip install pillow`. |
| A project is missing | Tap 🔄 *Scan again* in Files. If its place is unusual, name the parent folder in `SCAN_PATHS`. |
| Something is listed that should not be | Hide it from its project card, or use `IGNORE_DIRS` / `IGNORE_SERVICES`. |

Logs: `journalctl -u server-wrench -f`

## Tests

```bash
pip install -r requirements.txt -r requirements-bigfiles.txt
python tests/test_offline.py
```

The suite needs no Telegram account, no network and no root. It builds a small fake
server in a temporary folder — project folders, unit files, a `/proc` tree, web-server
configuration, Docker API answers — and runs the real bot code against it through a
fake HTTP layer, checking every message for valid Telegram HTML and length limits.
At the end it starts the real process against a local stand-in for the Bot API: start,
menu, status, a full backup over HTTP, a clean stop.

What it cannot stand in for is a real server. Distribution-specific layouts are covered
by fixtures, not by machines of every kind; if discovery misses something on yours, an
issue with the layout is very welcome.

## Project layout

```
bot.py                        the bot — one file
install.sh                    installer / updater / uninstaller for systemd servers
deploy/server-wrench.service  reference service file
.env.example                  every setting, commented
requirements.txt              python-telegram-bot, tzdata, pillow
requirements-bigfiles.txt     telethon (optional, for big-file mode)
tests/test_offline.py         the test suite
docs/                         sample images and the script that draws them
```

## License

[MIT](LICENSE) © Parsa Rahmani

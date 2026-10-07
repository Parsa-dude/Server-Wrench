#!/usr/bin/env bash
#
# Server Wrench — installer for systemd-based Linux servers
#
#   sudo bash install.sh               install, or repair an existing install
#   sudo bash install.sh --update      pull the latest code, update packages, restart
#   sudo bash install.sh --status      service state and the last log lines
#   sudo bash install.sh --uninstall   stop and remove the service (folder and data stay)
#
# Options
#   --yes          never ask a question (BOT_TOKEN / ADMIN_IDS come from the
#                  environment or from an existing .env)
#   --big-files    also install the optional big-file library (Telethon)
#   --no-sandbox   leave the systemd sandbox out of the service file
#   --no-start     prepare everything, but do not enable or start the service
#
# Unattended example:
#   BOT_TOKEN=123:abc ADMIN_IDS=111,222 bash install.sh --yes
#
set -euo pipefail

SERVICE="server-wrench"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT="/etc/systemd/system/${SERVICE}.service"
ACTION="install"
ASSUME_YES=0
BIG_FILES=0
SANDBOX=1
START=1

for arg in "$@"; do
    case "$arg" in
        --update)     ACTION="update" ;;
        --status)     ACTION="status" ;;
        --uninstall)  ACTION="uninstall" ;;
        --yes|-y)     ASSUME_YES=1 ;;
        --big-files)  BIG_FILES=1 ;;
        --no-sandbox) SANDBOX=0 ;;
        --no-start)   START=0 ;;
        -h|--help)    sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $arg (try --help)" >&2; exit 2 ;;
    esac
done

if [ -t 1 ]; then
    BOLD=$'\033[1m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'
else
    BOLD=""; RED=""; GREEN=""; YELLOW=""; RESET=""
fi
say()  { printf '%s\n' "${BOLD}==>${RESET} $*"; }
ok()   { printf '%s\n' "${GREEN}  ✓${RESET} $*"; }
warn() { printf '%s\n' "${YELLOW}  !${RESET} $*" >&2; }
die()  { printf '%s\n' "${RED}  ✗ $*${RESET}" >&2; exit 1; }

need_root() {
    [ "$(id -u)" -eq 0 ] || die "Run this as root:  sudo bash install.sh
    (the bot runs as root so that it can read every project on the server)"
}

have() { command -v "$1" >/dev/null 2>&1; }

need_systemd() {
    if [ ! -d /run/systemd/system ] || ! have systemctl; then
        die "systemd was not found on this machine.
    The bot itself still runs here — start it by hand or with your own supervisor:
        python3 -m venv venv && venv/bin/pip install -r requirements.txt
        cp .env.example .env    # fill it in
        venv/bin/python bot.py"
    fi
}

# ── Python 3.9+ ─────────────────────────────────────────────────
python_ok() {
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1
}

find_python() {
    local c
    for c in python3 python3.14 python3.13 python3.12 python3.11 python3.10 python3.9; do
        if have "$c" && python_ok "$c"; then
            command -v "$c"
            return 0
        fi
    done
    return 1
}

install_python() {
    say "Python 3.9 or newer was not found — installing it"
    if have apt-get; then
        apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv
    elif have dnf; then
        dnf install -y -q python3.12 || dnf install -y -q python3.11 \
            || dnf install -y -q python39 || dnf install -y -q python3
    elif have yum; then
        yum install -y -q python39 || yum install -y -q python3
    elif have zypper; then
        zypper --non-interactive install python311 || zypper --non-interactive install python3
    elif have pacman; then
        pacman -Sy --noconfirm python
    else
        die "No known package manager. Install Python 3.9+ yourself and run this again."
    fi
}

make_venv() {
    local py="$1"
    if [ -x "$DIR/venv/bin/python" ] && python_ok "$DIR/venv/bin/python"; then
        ok "virtual environment: $DIR/venv"
        return 0
    fi
    say "Creating the virtual environment"
    rm -rf "$DIR/venv"
    if ! "$py" -m venv "$DIR/venv" >/dev/null 2>&1; then
        if have apt-get; then   # Debian and Ubuntu ship venv as a separate package
            local ver
            ver="$("$py" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
            apt-get update -qq
            DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "python${ver}-venv" \
                || DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv
        fi
        rm -rf "$DIR/venv"
        "$py" -m venv "$DIR/venv" || die "Could not create a virtual environment with $py"
    fi
    ok "virtual environment: $DIR/venv"
}

install_packages() {
    say "Installing Python packages"
    "$DIR/venv/bin/python" -m pip install --quiet --upgrade pip
    "$DIR/venv/bin/python" -m pip install --quiet -r "$DIR/requirements.txt"
    ok "python-telegram-bot, tzdata, pillow"
    if [ "$BIG_FILES" -eq 1 ]; then
        "$DIR/venv/bin/python" -m pip install --quiet -r "$DIR/requirements-bigfiles.txt"
        if "$DIR/venv/bin/python" -m pip install --quiet cryptg >/dev/null 2>&1; then
            ok "telethon + cryptg (big-file mode)"
        else
            ok "telethon (big-file mode)"
            warn "cryptg could not be installed; big uploads will work but be slower"
        fi
    fi
}

# ── configuration ───────────────────────────────────────────────
env_value() {   # env_value KEY  → value from .env, empty when missing
    [ -f "$DIR/.env" ] || return 0
    sed -n "s/^[[:space:]]*\(export[[:space:]]\+\)\{0,1\}$1[[:space:]]*=[[:space:]]*//p" "$DIR/.env" \
        | tail -n 1 | sed "s/^[\"']//; s/[\"']\$//"
}

valid_token()  { [[ "$1" =~ ^[0-9]{5,12}:[A-Za-z0-9_-]{30,}$ ]]; }
valid_admins() { [[ "$1" =~ ^[0-9]+([,\ ]+[0-9]+)*$ ]]; }

write_env() {
    local token admins api_id api_hash answer
    token="${BOT_TOKEN:-$(env_value BOT_TOKEN)}"
    admins="${ADMIN_IDS:-$(env_value ADMIN_IDS)}"
    api_id="${API_ID:-$(env_value API_ID)}"
    api_hash="${API_HASH:-$(env_value API_HASH)}"

    if [ "$ASSUME_YES" -eq 0 ] && [ ! -t 0 ]; then
        ASSUME_YES=1    # nobody at the keyboard (piped, or started by another tool)
    fi
    if [ -f "$DIR/.env" ] && valid_token "$token" && [ -z "${BOT_TOKEN:-}${ADMIN_IDS:-}${API_ID:-}" ]; then
        # an existing installation: nothing to ask — unless --big-files wants its two values
        if [ "$BIG_FILES" -eq 0 ] || [ -n "$api_id" ] || [ "$ASSUME_YES" -eq 1 ]; then
            ok "configuration: $DIR/.env (kept as it is)"
            if [ -n "$api_id" ]; then
                BIG_FILES=1
            elif [ "$BIG_FILES" -eq 1 ]; then
                warn "Big-file mode also needs API_ID and API_HASH in .env (see README)."
            fi
            return 0
        fi
    fi
    if [ "$ASSUME_YES" -eq 0 ]; then
        say "Configuration"
        echo "    Create a bot with @BotFather in Telegram and paste its token here."
        while ! valid_token "$token"; do
            read -r -p "    Bot token: " token
            token="${token//[[:space:]]/}"
            valid_token "$token" || warn "That does not look like a bot token (123456789:AA…)."
        done
        if ! valid_admins "$admins"; then
            echo "    Your numeric Telegram ID (several: separate with commas)."
            echo "    Don't know it? Press Enter — the bot will tell you when you send it /start."
            while true; do
                read -r -p "    Admin ID(s): " admins
                admins="${admins//[[:space:]]/}"
                if [ -z "$admins" ] || valid_admins "$admins"; then break; fi
                warn "Digits and commas only, for example 123456789,987654321"
            done
        fi
        if [ -z "$api_id" ]; then
            echo "    Optional — big files: send one file of up to 2 GB instead of 45 MB parts."
            echo "    It needs an api_id and api_hash from https://my.telegram.org"
            answer="y"
            if [ "$BIG_FILES" -eq 0 ]; then
                read -r -p "    Set it up now? [y/N] " answer
            fi
            if [[ "$answer" =~ ^[Yy] ]]; then
                read -r -p "    api_id: " api_id
                read -r -p "    api_hash: " api_hash
                api_id="${api_id//[[:space:]]/}"
                api_hash="${api_hash//[[:space:]]/}"
                if [[ ! "$api_id" =~ ^[0-9]+$ ]] || [[ ! "$api_hash" =~ ^[0-9a-fA-F]{32}$ ]]; then
                    warn "Those values do not look right; skipping big files (add them to .env later)."
                    api_id=""
                    api_hash=""
                fi
            fi
        fi
    fi
    valid_token "$token" || die "BOT_TOKEN is missing or does not look like a bot token.
    Without a keyboard to ask on, pass it in the environment:
        BOT_TOKEN=123456789:AA... ADMIN_IDS=111,222 bash install.sh --yes"
    if [ -n "$admins" ] && ! valid_admins "$admins"; then
        die "ADMIN_IDS must be numeric IDs separated by commas."
    fi

    local old_umask keep
    keep=""
    if [ -f "$DIR/.env" ]; then     # keep every other setting the admin already had
        keep="$(grep -v -E '^[[:space:]]*(export[[:space:]]+)?(BOT_TOKEN|ADMIN_IDS|API_ID|API_HASH)[[:space:]]*=' "$DIR/.env" \
                | grep -v '^# Server Wrench — written by install.sh' || true)"
    fi
    old_umask="$(umask)"
    umask 077
    {
        echo "# Server Wrench — written by install.sh; every option is described in .env.example"
        echo "BOT_TOKEN=$token"
        echo "ADMIN_IDS=$admins"
        if [ -n "$api_id" ] && [ -n "$api_hash" ]; then
            echo "API_ID=$api_id"
            echo "API_HASH=$api_hash"
        fi
        if [ -n "$keep" ]; then
            printf '%s\n' "$keep"
        fi
    } > "$DIR/.env.new"
    mv -f "$DIR/.env.new" "$DIR/.env"
    umask "$old_umask"
    chmod 600 "$DIR/.env"
    ok "configuration written: $DIR/.env (readable by root only)"
    if [ -n "$api_id" ]; then
        BIG_FILES=1
    fi
    if [ -z "$admins" ]; then
        warn "No admin yet: send /start to the bot, put the ID it shows into ADMIN_IDS in .env,"
        warn "then run:  systemctl restart $SERVICE"
    fi
}

# ── the service ─────────────────────────────────────────────────
write_unit() {
    say "Writing $UNIT"
    {
        cat <<EOF
[Unit]
Description=Server Wrench - read-only Telegram bot for this server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$DIR
ExecStart=$DIR/venv/bin/python $DIR/bot.py
Restart=always
RestartSec=10
# 78 = the bot found a configuration mistake (no token, or a token Telegram rejects);
# restarting would not fix it, so systemd leaves it stopped with the reason in the log.
RestartPreventExitStatus=78
KillSignal=SIGINT
TimeoutStopSec=30
UMask=0077

# Gentle on the server: lowest CPU and disk priority, capped memory, and the
# first process to go if the machine ever runs out of RAM.
Nice=10
IOSchedulingClass=idle
CPUWeight=20
MemoryMax=400M
OOMScoreAdjust=600
EOF
        if [ "$SANDBOX" -eq 1 ]; then
            cat <<EOF

# Read-only by construction: for this process the whole filesystem is mounted
# read-only; only its own data/ and .work/ folders can be written.
# (Delete this block if the service does not start on an unusual system.)
ProtectSystem=strict
ReadWritePaths=-$DIR/data -$DIR/.work
NoNewPrivileges=yes
ProtectKernelModules=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes
CapabilityBoundingSet=CAP_DAC_READ_SEARCH CAP_SYS_PTRACE
EOF
        fi
        cat <<EOF

[Install]
WantedBy=multi-user.target
EOF
    } > "$UNIT"
    chmod 644 "$UNIT"
    ok "service file written$([ "$SANDBOX" -eq 1 ] && echo ' (with the read-only sandbox)')"
}

start_service() {
    systemctl daemon-reload
    if [ "$START" -eq 0 ]; then
        ok "prepared; start it with:  systemctl enable --now $SERVICE"
        return 0
    fi
    say "Starting the service"
    systemctl enable "$SERVICE" >/dev/null 2>&1 || true
    systemctl restart "$SERVICE"
    sleep 4
    if systemctl is-active --quiet "$SERVICE"; then
        ok "$SERVICE is running and will start again after a reboot"
        echo
        echo "    Open your bot in Telegram and send  /start"
        echo "    Logs:     journalctl -u $SERVICE -f"
        echo "    Restart:  systemctl restart $SERVICE"
    else
        warn "$SERVICE did not stay up. Its last log lines:"
        journalctl -u "$SERVICE" -n 25 --no-pager >&2 || true
        if [ "$SANDBOX" -eq 1 ]; then
            warn "If the lines above mention NAMESPACE or 'Failed to set up mount namespacing',"
            warn "run again with:  bash install.sh --no-sandbox"
        fi
        exit 1
    fi
}

# ── actions ─────────────────────────────────────────────────────
do_install() {
    need_root
    need_systemd
    [ -f "$DIR/bot.py" ] || die "bot.py was not found next to this script ($DIR)."
    case "$DIR" in
        *[[:space:]]*) die "The folder path contains a space ($DIR). Move it, e.g. to /opt/server-wrench." ;;
    esac
    say "Installing Server Wrench in $DIR"
    local py
    py="$(find_python)" || { install_python; py="$(find_python)" || die "Python 3.9+ is still missing."; }
    ok "python: $py ($("$py" -c 'import sys; print(sys.version.split()[0])'))"
    write_env           # first: a missing token should stop the run before anything is built
    make_venv "$py"
    install_packages
    mkdir -p "$DIR/data" "$DIR/.work"
    chown -R root:root "$DIR/data" "$DIR/.work" "$DIR/.env"
    chmod 700 "$DIR/data" "$DIR/.work"
    write_unit
    start_service
}

do_update() {
    need_root
    need_systemd
    [ -x "$DIR/venv/bin/python" ] || die "Not installed yet — run:  bash install.sh"
    if [ -d "$DIR/.git" ] && have git; then
        say "Fetching the latest version"
        git -C "$DIR" pull --ff-only || die "git pull failed (local changes?). Resolve it and run again."
    else
        warn "This folder is not a git checkout; replace the files yourself, then run this again."
    fi
    if [ -n "$(env_value API_ID)" ]; then
        BIG_FILES=1
    fi
    install_packages
    systemctl restart "$SERVICE"
    sleep 3
    if systemctl is-active --quiet "$SERVICE"; then
        ok "$SERVICE restarted"
    else
        journalctl -u "$SERVICE" -n 25 --no-pager >&2 || true
        die "$SERVICE did not come back up (see the log above)."
    fi
}

do_status() {
    need_systemd
    systemctl status "$SERVICE" --no-pager -l | head -n 15 || true
    echo
    journalctl -u "$SERVICE" -n 20 --no-pager || true
}

do_uninstall() {
    need_root
    need_systemd
    local answer="y"
    if [ "$ASSUME_YES" -eq 0 ]; then
        [ -t 0 ] || die "Nobody at the keyboard to confirm; add --yes to remove the service."
        read -r -p "Stop and remove the $SERVICE service? [y/N] " answer
    fi
    [[ "$answer" =~ ^[Yy] ]] || { echo "Nothing was changed."; exit 0; }
    systemctl disable --now "$SERVICE" >/dev/null 2>&1 || true
    rm -f "$UNIT"
    systemctl daemon-reload
    ok "service removed"
    echo "    The folder (with .env and the monitoring history) is still here:"
    echo "        $DIR"
    echo "    Delete it too with:  rm -rf \"$DIR\""
}

case "$ACTION" in
    install)   do_install ;;
    update)    do_update ;;
    status)    do_status ;;
    uninstall) do_uninstall ;;
esac

#!/bin/sh
# Copy the Pi's nightly database backups to this machine (#56).
#
# Runs on ANOTHER machine, not the Pi, and pulls: the Pi holds no key for
# this machine, so a broken-into Pi cannot delete these copies. The backups
# are already checked on the Pi; each new copy is checked again here.
#
# Install (once, on the receiving machine):
#   1. `ssh raspberrypi5-02 true` must work without a password.
#   2. crontab -e, and add:  30 5 * * * /path/to/pull_backups.sh >> ~/news_bot_backups/pull.log 2>&1
#
# Settings (environment): PI_HOST (ssh alias), REMOTE_DIR, LOCAL_DIR, KEEP.
set -eu

PI_HOST="${PI_HOST:-raspberrypi5-02}"
REMOTE_DIR="${REMOTE_DIR:-/data/news_bot_backups}"
LOCAL_DIR="${LOCAL_DIR:-$HOME/news_bot_backups}"
KEEP="${KEEP:-60}"

mkdir -p "$LOCAL_DIR"
rsync -a --ignore-existing "$PI_HOST:$REMOTE_DIR/trade_history-*.db" "$LOCAL_DIR/"

newest=$(ls -1 "$LOCAL_DIR"/trade_history-*.db 2>/dev/null | sort | tail -1)
if [ -z "$newest" ]; then
    echo "$(date '+%F %T') pull_backups: no backups copied" >&2
    exit 1
fi
if command -v sqlite3 >/dev/null 2>&1; then
    result=$(sqlite3 "file:$newest?mode=ro" "PRAGMA integrity_check;" 2>&1 || true)
    if [ "$result" != "ok" ]; then
        echo "$(date '+%F %T') pull_backups: $newest failed its check: $result" >&2
        exit 1
    fi
fi

# Keep the newest $KEEP copies; the names sort by date.
ls -1 "$LOCAL_DIR"/trade_history-*.db | sort -r | tail -n "+$((KEEP + 1))" | while read -r old; do rm -f "$old"; done
echo "$(date '+%F %T') pull_backups: ok, newest $(basename "$newest"), $(ls -1 "$LOCAL_DIR"/trade_history-*.db | wc -l | tr -d ' ') kept"

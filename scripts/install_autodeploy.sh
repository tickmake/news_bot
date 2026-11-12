#!/bin/sh
# Install the git-poll deploy timer on the Pi (user units; no sudo needed, but
# `loginctl enable-linger $USER` must be on so it runs without a login).
#
# Runs ON the Pi:  cd ~/news_bot && scripts/install_autodeploy.sh
# Stop polling:    systemctl --user disable --now news-bot-deploy.timer
# Watch it:        journalctl --user -u news-bot-deploy -f
set -eu

cd "$(dirname "$0")/.."
unit_dir="$HOME/.config/systemd/user"
mkdir -p "$unit_dir"
cp deploy/systemd/news-bot-deploy.service deploy/systemd/news-bot-deploy.timer "$unit_dir/"
systemctl --user daemon-reload
systemctl --user enable --now news-bot-deploy.timer
systemctl --user list-timers news-bot-deploy.timer --no-pager

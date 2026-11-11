#!/bin/sh
# Build the dashboard image here and load it onto the Pi.
#
# Runs on the Mac, not the Pi: the Evidence site build needs more than 1 GB of
# RAM, which the Pi does not have to spare next to Ollama. The Mac is arm64
# like the Pi, so the image runs there as built. The site holds no data (the
# Pi's dashboard-data service fills that in), so this is only needed when
# something under dashboard/ changes.
#
# This only loads the image. To start using it, on the Pi:
#   cd ~/news_bot && docker compose up -d dashboard-data dashboard
#
# Settings (environment): PI_HOST (ssh alias), IMAGE.
set -eu

PI_HOST="${PI_HOST:-raspberrypi5-02}"
IMAGE="${IMAGE:-news_bot-dashboard:latest}"
cd "$(dirname "$0")/../dashboard"

docker build --platform linux/arm64 -t "$IMAGE" .
docker save "$IMAGE" | gzip | ssh "$PI_HOST" 'gunzip | docker load'
echo "$(date '+%F %T') ship_dashboard: loaded $IMAGE on $PI_HOST"

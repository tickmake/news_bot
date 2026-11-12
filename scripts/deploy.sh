#!/bin/sh
# Deploy the latest main on the Pi, safely (#108).
#
# Runs ON the Pi, from the checkout:  cd ~/news_bot && scripts/deploy.sh
# Every step must pass before the next one runs:
#   1. tag the running bot image as a rollback,
#   2. copy .env and take a checked database backup,
#   3. git pull --ff-only, then rebuild and restart the bot and the dashboard,
#   4. wait until the bot is healthy, then show the new commit and log errors,
#   5. print the command that rolls back.
#
# The systemd timer in deploy/systemd runs this every few minutes, so a merge to
# main deploys itself; scripts/install_autodeploy.sh sets that up. A deploy that
# fails, or one that succeeds, is reported on Telegram.
#
# Settings (environment): FORCE=1 deploys even when there is nothing new to
# pull; HEALTH_TIMEOUT (seconds, default 240); NOTIFY=0 sends no Telegram.
set -eu

SERVICE=news-notifier
DASHBOARD=dashboard-streamlit
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-240}"

say() { printf '\n== %s\n' "$*"; }
# Telegram, with the bot's own token from .env. Best effort: a failed message
# must not fail the deploy.
notify() {
    [ "${NOTIFY:-1}" = 1 ] && [ -f .env ] || return 0
    token=$(sed -n 's/^TELEGRAM_TOKEN=//p' .env | tail -1)
    chat=$(sed -n 's/^TELEGRAM_CHAT_ID=//p' .env | tail -1)
    [ -n "$token" ] && [ -n "$chat" ] || return 0
    curl -s -m 20 -o /dev/null "https://api.telegram.org/bot$token/sendMessage" \
        --data-urlencode "chat_id=$chat" --data-urlencode "text=$1" || true
}
die() {
    printf '\nDEPLOY STOPPED: %s\n' "$*" >&2
    notify "news_bot deploy STOPPED: $*"
    exit 1
}

# `git pull` below may rewrite this very file, and sh reads a script as it
# runs it. Run from a private copy instead, remembering where the checkout is.
if [ -z "${DEPLOY_REPO:-}" ]; then
    DEPLOY_REPO=$(cd "$(dirname "$0")/.." && pwd)
    copy=$(mktemp)
    cp "$0" "$copy"
    DEPLOY_REPO="$DEPLOY_REPO" exec sh "$copy" "$@"
fi
cd "$DEPLOY_REPO"

# One deploy at a time: the timer must not start a second run while a slow
# build is still going.
exec 9>"${TMPDIR:-/tmp}/news_bot_deploy.lock"
flock -n 9 || { echo "Another deploy is running; skipping."; exit 0; }

# --- 0. preflight -------------------------------------------------------------
[ "$(git rev-parse --abbrev-ref HEAD)" = main ] || die "checkout is not on main"
if ! git diff --quiet || ! git diff --cached --quiet; then die "tracked files have local changes (git status)"; fi
git fetch -q origin main
# Compare with what was last deployed, not with HEAD: a manual `git pull` moves
# HEAD without rebuilding, and the timer would then never deploy that change.
marker=.git/news_bot_deployed
old=$(git rev-parse --short "$(cat "$marker" 2>/dev/null || echo HEAD)")
new=$(git rev-parse --short origin/main)
if [ "$old" = "$new" ] && [ "${FORCE:-0}" != 1 ]; then
    echo "Already at origin/main ($old). Nothing to deploy; FORCE=1 to rebuild anyway."
    exit 0
fi
container=$(docker compose ps -q "$SERVICE")
[ -n "$container" ] || die "$SERVICE is not running; start it by hand first"
image=$(docker inspect -f '{{.Config.Image}}' "$container")
image_repo=${image%%:*}
echo "Deploying $old -> $new"
git log --oneline "$old..origin/main" | sed 's/^/  /'

# --- 1. rollback image --------------------------------------------------------
say "1/5 Tag rollback image"
stamp=$(date +%Y%m%d-%H%M)
rollback="$image_repo:rollback-$stamp-$old"
running_id=$(docker inspect -f '{{.Image}}' "$container")
if docker image inspect "$running_id" >/dev/null 2>&1; then
    docker tag "$running_id" "$rollback"
else
    # The running container's image can be gone from the store (an image
    # prune removes it once it is untagged, even while it runs). The newest
    # build is the closest thing left to roll back to.
    echo "WARNING: the running image ${running_id#sha256:} is no longer stored;"
    echo "         tagging the newest build ($image:latest) as the rollback instead."
    docker tag "$image:latest" "$rollback"
fi
echo "Rollback image: $rollback"

# --- 2. .env copy and database backup -----------------------------------------
say "2/5 Back up .env and the database"
env_copy=".env.bak-$stamp"
cp -p .env "$env_copy"
echo ".env copy: $env_copy"
# As the app user (uid 10001): as root the backup would be root-owned and the
# bot's nightly pruning could not remove it. backup_trade_history() checks the
# copy with PRAGMA integrity_check and raises if it is bad.
backup=$(docker exec -u 10001 "$container" python -c \
    "from newsbot.backup import backup_trade_history; print(backup_trade_history() or '')") \
    || die "database backup failed"
[ -n "$backup" ] || die "TRADE_BACKUP_DIR is not set; no database backup was made"
host_dir=$(sed -n 's/^TRADE_BACKUP_HOST_DIR=//p' .env | tail -1)
echo "Database backup (checked): ${host_dir:-./backups}/$(basename "$backup")"

# --- 3. pull and rebuild ------------------------------------------------------
say "3/5 Pull and rebuild"
git pull -q --ff-only origin main
docker compose build "$SERVICE" "$DASHBOARD"
# --force-recreate keeps the plain container name (plain `up -d` after a build
# can leave it renamed <hash>_daily_news_bot).
docker compose up -d --force-recreate "$SERVICE" "$DASHBOARD"

# --- 4. health ----------------------------------------------------------------
say "4/5 Wait for the bot to be healthy (up to ${HEALTH_TIMEOUT}s)"
container=$(docker compose ps -q "$SERVICE")
waited=0
while :; do
    status=$(docker inspect -f '{{.State.Health.Status}}' "$container" 2>/dev/null || echo missing)
    [ "$status" = healthy ] && break
    [ "$status" = unhealthy ] && break
    [ "$waited" -ge "$HEALTH_TIMEOUT" ] && break
    sleep 5
    waited=$((waited + 5))
done
echo "Health: $status after ${waited}s. Now at $(git log --oneline -1)"
errors=$(docker logs --since "${waited}s" "$container" 2>&1 | grep -iE 'error|traceback|exception' || true)
if [ -n "$errors" ]; then
    echo "Errors in the log since start:"
    echo "$errors" | tail -20 | sed 's/^/  /'
else
    echo "No errors in the log since start."
fi

# --- 5. rollback command ------------------------------------------------------
say "5/5 To roll back"
cat <<EOF
  cd $(pwd) && git checkout -q $old && cp -p $env_copy .env \\
    && docker tag $rollback $image && docker compose up -d --no-build --force-recreate $SERVICE
  (then 'git checkout main' once the fix is merged)
EOF

[ "$status" = healthy ] || die "bot is $status after deploying $new. Roll back on the Pi: git checkout -q $old && cp -p $env_copy .env && docker tag $rollback $image && docker compose up -d --no-build --force-recreate $SERVICE"
echo
echo "Deployed $new."
git rev-parse HEAD >"$marker"
notify "news_bot deployed $new ($(git log -1 --format=%s)). Bot healthy after ${waited}s.$([ -n "$errors" ] && echo ' Errors in the log, check it.')"

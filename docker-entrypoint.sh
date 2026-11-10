#!/bin/sh
# Start as root only long enough to make the data volume writable, then drop to
# the unprivileged `app` user for the bot itself.
#
# A named volume keeps the owner it was created with. Volumes made by earlier
# versions of this image are root-owned, so without this step switching to a
# non-root user would stop the bot from saving state and trade history.
set -e

DATA_DIR="${DATA_DIR:-/app/data}"
# Backup target. A bind mount arrives owned by whoever owns the host path, so
# without this the nightly snapshot fails with EACCES as `app`. Empty when the
# mount is absent, which is why the chown below is guarded rather than assumed.
BACKUP_DIR="${TRADE_BACKUP_DIR:-}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR"
    find "$DATA_DIR" ! -user app -exec chown app:app {} +
    if [ -n "$BACKUP_DIR" ] && [ -d "$BACKUP_DIR" ]; then
        # Only the directory itself, not -R: old snapshots are already app-owned
        # and recursing costs a stat per file on every start.
        chown app:app "$BACKUP_DIR"
    fi
    exec setpriv --reuid=app --regid=app --init-groups "$@"
fi

exec "$@"

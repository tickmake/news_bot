#!/bin/sh
# Start as root only long enough to make the data volume writable, then drop to
# the unprivileged `app` user for the bot itself.
#
# A named volume keeps the owner it was created with. Volumes made by earlier
# versions of this image are root-owned, so without this step switching to a
# non-root user would stop the bot from saving state and trade history.
set -e

DATA_DIR="${DATA_DIR:-/app/data}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR"
    find "$DATA_DIR" ! -user app -exec chown app:app {} +
    exec setpriv --reuid=app --regid=app --init-groups "$@"
fi

exec "$@"

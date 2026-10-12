#!/bin/sh
# Starts Azan SIP in Box mode with everything it keeps in /data.
# AZAN_PASSWORD (from compose.yaml) is the box's factory password: it works until the owner
# chooses their own under Settings > Password and reset, and a factory reset goes back to it.
set -eu
cfg=/data/config.toml
if ! touch /data/.write-test 2>/dev/null; then
    echo "The storage folder mounted at /data can't be written to (the container runs as user 1000)." >&2
    echo "Give it to that user, then start again:  sudo chown -R 1000:1000 <the folder>  &&  docker compose up -d" >&2
    exit 1
fi
rm -f /data/.write-test
if [ -n "${AZAN_PASSWORD:-}" ]; then
    if ! printf '%s\n' "$AZAN_PASSWORD" | python -m azan_sip -c "$cfg" box-password --factory --stdin >/dev/null; then
        echo "Couldn't set AZAN_PASSWORD from compose.yaml (see the line above)." >&2
        exit 1
    fi
elif [ ! -f /data/auth/factory.json ]; then
    echo "No password is set, so settings can't be changed yet. Set AZAN_PASSWORD in compose.yaml" \
         "(at least 8 characters) and run: docker compose up -d" >&2
fi
exec python -m azan_sip -c "$cfg" run --box "$@"

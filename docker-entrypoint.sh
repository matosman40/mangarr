#!/bin/sh
# Démarre en root, répare les droits de /library et /config, puis passe à PUID:PGID (1000 par défaut).
# Si le conteneur est lancé avec `user:` (non root), on lance directement sans rien modifier.
set -e

if [ "$(id -u)" != "0" ]; then
    exec "$@"
fi

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

for d in "${MANGARR_LIBRARY:-/library}" "${MANGARR_CONFIG:-/config}"; do
    mkdir -p "$d"
    # Ne relance un chown récursif que si quelque chose n'appartient pas à PUID:PGID.
    if [ -n "$(find "$d" \( ! -user "$PUID" -o ! -group "$PGID" \) -print -quit 2>/dev/null)" ]; then
        echo "[entrypoint] correction des droits de $d -> $PUID:$PGID"
        chown -R "$PUID:$PGID" "$d" || echo "[entrypoint] ATTENTION : chown impossible sur $d (volume en lecture seule ou NAS ?)"
    fi
done

export HOME=/tmp
exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"

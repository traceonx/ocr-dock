#!/bin/sh
set -eu
DATA_DIR="${DATA_DIR:-/app/data}"
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
case "$PUID:$PGID" in *[!0-9:]*|:*|*:) echo 'PUID and PGID must be numeric' >&2; exit 1;; esac
export DATA_DIR PUID PGID
if [ "$(id -u)" = "0" ]; then
  mkdir -p "$DATA_DIR"
  chown "$PUID:$PGID" "$DATA_DIR"
  if [ -L "$DATA_DIR/api_key" ]; then echo 'api_key must not be a symbolic link' >&2; exit 1; fi
  if [ -f "$DATA_DIR/api_key" ]; then
    chown "$PUID:$PGID" "$DATA_DIR/api_key"
    chmod 600 "$DATA_DIR/api_key"
  fi
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$0" "$@"
fi
umask "${UMASK:-022}"
echo "OCR Dock v${APP_VERSION:-$(cat /app/VERSION)} · CPU · $(id -u):$(id -g) · ${DATA_DIR}"
exec uvicorn ocr_dock.app:app --host 0.0.0.0 --port "${PORT:-8080}" --workers 1 --no-proxy-headers

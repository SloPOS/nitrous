#!/bin/bash
# Builds and (re)starts Nitrous next to a Rust server container on the same Docker host
# (written for Unraid, works on any Linux Docker host). Run from this folder:
#
#   RUST_CONTAINER=my-rust-server RUST_OXIDE=/path/to/rust/oxide bash deploy.sh
#
# The page password is generated on first run and kept in $BASE/password.txt.
# The RCON password, port and address are read from the Rust container, so they are
# never written down here. Re-run this script after changing the RCON password.
set -euo pipefail

RUST_CONTAINER=${RUST_CONTAINER:-rust-server}             # name of the Rust server container
RUST_OXIDE=${RUST_OXIDE:-/mnt/user/appdata/rust/oxide}    # the server's oxide folder on the host
BASE=${BASE:-/mnt/user/appdata/oxide-manager}             # where Nitrous keeps its data
NAME=${NAME:-oxide-manager}                               # container name
PORT=${PORT:-8790}                                        # port for the web page
TZ=${TZ:-America/New_York}
TAG=nitrous:$(date +%Y%m%d-%H%M%S)

[ -d "$RUST_OXIDE/plugins" ] || { echo "No plugins folder in $RUST_OXIDE - set RUST_OXIDE." >&2; exit 1; }
docker inspect "$RUST_CONTAINER" >/dev/null 2>&1 || { echo "No container named $RUST_CONTAINER - set RUST_CONTAINER." >&2; exit 1; }

mkdir -p "$BASE/data"
[ -s "$BASE/password.txt" ] || (umask 077; openssl rand -hex 12 > "$BASE/password.txt")
chmod 600 "$BASE/password.txt"

rust_env() { docker inspect "$RUST_CONTAINER" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n "s/^$1=//p" | head -1; }
RCON_HOST=${RCON_HOST:-$(docker inspect "$RUST_CONTAINER" --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')}
RCON_PORT=${RCON_PORT:-$(rust_env RCON_PORT)}
RCON_PASSWORD=${RCON_PASSWORD:-$(rust_env RCON_PASSWORD)}

docker build -q -t "$TAG" "$(dirname "$0")"
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" --restart unless-stopped -p "$PORT:8080" \
  -e TZ="$TZ" \
  -e MANAGER_PASSWORD="$(cat "$BASE/password.txt")" \
  -e RCON_HOST="$RCON_HOST" -e RCON_PORT="${RCON_PORT:-28016}" -e RCON_PASSWORD="$RCON_PASSWORD" \
  -v "$RUST_OXIDE:/oxide" -v "$BASE/data:/data" "$TAG" >/dev/null
echo "Nitrous is running on port $PORT (image $TAG, RCON target ${RCON_HOST:-not set})"
echo "Page password: $BASE/password.txt (any username)"

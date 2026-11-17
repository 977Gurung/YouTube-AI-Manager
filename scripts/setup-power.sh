#!/usr/bin/env bash
# Allow the dashboard's System tab to restart / shut down this machine (asks for your sudo password once).
#   ./scripts/setup-power.sh            install /etc/sudoers.d/ytm-power
#   ./scripts/setup-power.sh --remove   take the permission away again
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${YTM_SUDOERS_DEST:-/etc/sudoers.d/ytm-power}"
if [[ "${1:-}" == "--remove" ]]; then
    sudo rm -f "$DEST" && echo "==> Removed $DEST - the dashboard can no longer restart or shut down the server."
    exit 0
fi
USER_NAME="${YTM_SERVICE_USER:-$(id -un)}"
TMP="$(mktemp)"; trap 'rm -f "$TMP"' EXIT
sed "s/@USER@/$USER_NAME/g" "$ROOT/deploy/sudoers/ytm-power" > "$TMP"
if command -v visudo >/dev/null; then
    visudo -cf "$TMP" >/dev/null || { echo "!! sudoers rule did not validate - nothing changed"; exit 1; }
fi
sudo install -m 0440 -o root -g root "$TMP" "$DEST"
echo "==> Dashboard power controls enabled for user $USER_NAME ($DEST)"

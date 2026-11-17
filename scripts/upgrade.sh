#!/usr/bin/env bash
# Upgrade an installed YT AI Manager from a new yt-ai-manager.zip, keeping your data and settings.
#   ./scripts/upgrade.sh ~/yt-ai-manager-new.zip [--keep-settings]
#   ./scripts/upgrade.sh --in-place        after `git pull` in a git clone: only the steps below
# Kept: .env, config/channels.yaml, config/n8n.env, data/, secrets/, models/, assets/ (your music,
# fonts and b-roll), logs/, .venv/. config/settings.yaml is replaced (old copy saved as
# settings.yaml.bak) unless --keep-settings. Then: new Python packages, database migration,
# AI image model download, service restart.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ZIP="${1:-}"; KEEP_SETTINGS=0; IN_PLACE=0
[[ "${2:-}" == "--keep-settings" ]] && KEEP_SETTINGS=1
[[ "$ZIP" == "--in-place" ]] && IN_PLACE=1
[[ $IN_PLACE -eq 1 || -f "$ZIP" ]] || { echo "usage: $0 <path/to/yt-ai-manager.zip> [--keep-settings]  |  $0 --in-place" >&2; exit 2; }
if [[ $IN_PLACE -eq 0 ]]; then
command -v rsync >/dev/null || sudo apt-get install -y rsync

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
unzip -q "$ZIP" -d "$TMP"
SRC="$TMP/yt-ai-manager"
[[ -d "$SRC/app" ]] || { echo "zip does not contain yt-ai-manager/app" >&2; exit 1; }
OLD_V="$(cat "$ROOT/VERSION" 2>/dev/null || echo "0.9 (before versioning)")"
NEW_V="$(cat "$SRC/VERSION" 2>/dev/null || echo unknown)"
echo "==> Upgrading YT AI Manager: $OLD_V -> $NEW_V"
if [[ "$OLD_V" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ && "$NEW_V" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] && \
   [[ "$(printf '%s\n%s\n' "$OLD_V" "$NEW_V" | sort -V | head -1)" == "$NEW_V" && "$OLD_V" != "$NEW_V" ]]; then
    echo "!! $NEW_V is OLDER than the installed $OLD_V (downgrade). Continuing in 10 s - press Ctrl+C to stop."
    sleep 10
fi
if [[ -f "$SRC/CHANGELOG.md" ]]; then
    echo "==> What's new:"
    awk '/^## \[/{n++} n==1' "$SRC/CHANGELOG.md" | sed -n '2,40p' | sed 's/^/    /'
fi

cd "$ROOT"
"$ROOT/ytm.sh" backup >/dev/null 2>&1 && echo "==> Backup written to backups/" || true
[[ $KEEP_SETTINGS -eq 0 ]] && cp config/settings.yaml config/settings.yaml.bak

EXCLUDES=(--exclude .env --exclude config/channels.yaml --exclude config/n8n.env --exclude data/
          --exclude secrets/ --exclude models/ --exclude assets/ --exclude logs/ --exclude .venv/ --exclude backups/)
[[ $KEEP_SETTINGS -eq 1 ]] && EXCLUDES+=(--exclude config/settings.yaml)
rsync -a "${EXCLUDES[@]}" "$SRC/" "$ROOT/"
mkdir -p assets/broll assets/music assets/fonts models/piper models/hf
[[ -f config/channels.yaml ]] || cp config/channels.example.yaml config/channels.yaml
chmod +x install.sh ytm.sh scripts/*.sh
if [[ $KEEP_SETTINGS -eq 0 ]]; then
    # carry over the LLM model, timezone and host you had
    for key in "llm:model" "app:timezone" "app:host"; do
        sec="${key%%:*}"; k="${key##*:}"
        old="$(sed -n "/^$sec:/,/^[a-z]/ s|^  $k:\s*\([^ #]*\).*|\1|p" config/settings.yaml.bak | head -1)"
        [[ -n "$old" ]] && sed -i -E "/^$sec:/,/^[a-z]/ s|^(  $k:)\s*[^ #]+|\1 $old|" config/settings.yaml
    done
fi
echo "==> Files updated"
else
    cd "$ROOT"
    echo "==> Updating in place (git clone) to version $(cat VERSION 2>/dev/null || echo unknown)"
    mkdir -p assets/broll assets/music assets/fonts models/piper models/hf
    [[ -f config/channels.yaml ]] || cp config/channels.example.yaml config/channels.yaml
    chmod +x install.sh ytm.sh scripts/*.sh
fi

.venv/bin/python -m pip install -q -r requirements.txt
.venv/bin/python -m pip install -q -r requirements-gpu.txt || true
.venv/bin/python -m pip install -r requirements-ai.txt
echo "==> Python packages updated"

# shellcheck source=scripts/env.sh
source "$ROOT/scripts/env.sh"
"$YTM_PY" -m app.cli init-db
"$YTM_PY" -m app.cli fetch-models || echo "!! model download failed - retry: ./ytm.sh fetch-models"
"$ROOT/scripts/setup-power.sh" || echo "!! dashboard power controls not enabled - retry: ./scripts/setup-power.sh"
"$ROOT/ytm.sh" restart
echo "==> Now running version $(cat "$ROOT/VERSION")"
"$ROOT/ytm.sh" doctor || true
if [[ $IN_PLACE -eq 1 ]]; then echo "==> Upgrade complete."; else echo "==> Upgrade complete. Old settings: config/settings.yaml.bak"; fi

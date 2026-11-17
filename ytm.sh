#!/usr/bin/env bash
# YouTube AI Manager control script.
#   ./ytm.sh start|stop|restart|status     manage the systemd services (ytm-api, ytm-n8n, ollama)
#   ./ytm.sh logs [api|n8n|ollama]         follow logs (default: api + n8n)
#   ./ytm.sh doctor                        check GPU, NVENC, Ollama, keys, channel tokens
#   ./ytm.sh auth <slug>                   link a YouTube channel (prints SSH-tunnel instructions)
#   ./ytm.sh research <slug>               run research for one channel now (foreground)
#   ./ytm.sh run <video_id> script|produce|upload
#   ./ytm.sh tick                          advance the pipeline now
#   ./ytm.sh fetch-models                  download the AI image + animation models (~32 GB, once)
#   ./ytm.sh version                       show the installed version
#   ./ytm.sh setup-voice [--enable]        install the expressive voice (Chatterbox, ~6 GB); --enable switches all channels to it
#   ./ytm.sh voice-sample <slug> ["text"]  speak a test paragraph with a channel's voice -> data/voice-sample-<slug>.wav
#   ./ytm.sh gpu                           live GPU usage
#   ./ytm.sh serve                         run the API in the foreground (debugging; stop the service first)
#   ./ytm.sh key                           print the dashboard API key
#   ./ytm.sh backup                        tar.gz of database, tokens, config and .env into ./backups
set -euo pipefail
# shellcheck source=scripts/env.sh
source "$(dirname "${BASH_SOURCE[0]}")/scripts/env.sh"
cd "$YTM_ROOT"

SERVICES=(ytm-api)
systemctl list-unit-files ytm-n8n.service 2>/dev/null | grep -q ytm-n8n && SERVICES+=(ytm-n8n)

cmd="${1:-help}"; shift || true
case "$cmd" in
    start|stop|restart)
        sudo systemctl "$cmd" "${SERVICES[@]}"
        if [[ "$cmd" != stop ]]; then sleep 2; systemctl --no-pager --lines=0 status "${SERVICES[@]}" | grep -E "●|Active:" || true; fi ;;
    version|--version|-v)
        echo "YT AI Manager $(cat "$YTM_ROOT/VERSION" 2>/dev/null || echo unknown)"; exit 0 ;;
    status)
        systemctl --no-pager --lines=0 status ollama "${SERVICES[@]}" | grep -E "●|Active:" || true
        echo
        "$YTM_PY" -m app.cli status || true ;;
    logs)
        case "${1:-}" in
            api) journalctl -u ytm-api -f -n 100 ;;
            n8n) journalctl -u ytm-n8n -f -n 100 ;;
            ollama) journalctl -u ollama -f -n 100 ;;
            *) journalctl -u ytm-api -u ytm-n8n -f -n 100 ;;
        esac ;;
    setup-voice)
        exec bash "$YTM_ROOT/scripts/setup-voice.sh" "$@" ;;
    doctor|auth|research|run|tick|init-db|sync-channels|fetch-models|voice-sample)
        exec "$YTM_PY" -m app.cli "$cmd" "$@" ;;
    gpu)
        exec watch -n 2 nvidia-smi ;;
    serve)
        exec "$YTM_PY" -m app.server ;;
    key)
        grep -E '^YTM_API_KEY=' .env | cut -d= -f2- ;;
    backup)
        mkdir -p backups
        out="backups/ytm-$(date +%Y%m%d-%H%M%S).tar.gz"
        "$YTM_PY" - <<'EOF'
import sqlite3
from app.config import data_dir
src = sqlite3.connect(data_dir() / "ytm.db"); dst = sqlite3.connect(data_dir() / "ytm.backup.db")
src.backup(dst); dst.close(); src.close()
EOF
        tar czf "$out" data/ytm.backup.db data/tokens config .env secrets 2>/dev/null || true
        rm -f data/ytm.backup.db
        chmod 600 "$out"
        echo "Backup written to $out (contains secrets - store it safely)" ;;
    help|-h|--help|*)
        sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//' ;;
esac

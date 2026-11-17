#!/usr/bin/env bash
# shellcheck disable=SC2015  # ok/warn always return 0, so 'A && ok || warn' is safe
# YouTube AI Manager - installer for Ubuntu 22.04 / 24.04 with an NVIDIA GPU (tuned for RTX 2070, 8 GB).
#
#   ./install.sh                 full install (run as your normal user; uses sudo when needed)
#   Works on Ubuntu Server and Ubuntu Desktop (desktop: also disables suspend so renders never stall).
#   ./install.sh --lan           also expose dashboard (:8765) and n8n (:5678) to your LAN via ufw
#   ./install.sh --llm-model llama3.1:8b --piper-voice en_US-lessac-medium
#   ./install.sh --skip-driver --skip-models --skip-n8n --cpu-only --no-services --allow-sleep --no-ai-images
#
# Steps: apt packages -> NVIDIA driver (reboot once if it was missing) -> NVENC check -> Ollama ->
# Python venv + CUDA 12 wheels -> .env with random API key -> LLM + Piper voice + font ->
# database -> Node 24 + n8n + workflow import -> systemd services -> health check.
# Safe to re-run: each step skips what is already done.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LLM_MODEL=""   # empty = keep the model already in config/settings.yaml (qwen2.5:7b-instruct on a new install)
PIPER_VOICE="en_US-ryan-high"
SKIP_DRIVER=0 SKIP_MODELS=0 SKIP_N8N=0 CPU_ONLY=0 LAN=0 NO_SERVICES=0 ALLOW_SLEEP=0 NO_AI_IMAGES=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --llm-model)   LLM_MODEL="$2"; shift 2 ;;
        --piper-voice) PIPER_VOICE="$2"; shift 2 ;;
        --skip-driver) SKIP_DRIVER=1; shift ;;
        --skip-models) SKIP_MODELS=1; shift ;;
        --skip-n8n)    SKIP_N8N=1; shift ;;
        --cpu-only)    CPU_ONLY=1; shift ;;
        --lan)         LAN=1; shift ;;
        --no-services) NO_SERVICES=1; shift ;;
        --allow-sleep) ALLOW_SLEEP=1; shift ;;
        --no-ai-images) NO_AI_IMAGES=1; shift ;;
        -h|--help)     sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32m[OK]\033[0m %s\n' "$*"; }
warn() { printf '    \033[33m[!!]\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
trap 'die "install failed at line $LINENO (see logs/install.log)"' ERR

[[ $EUID -eq 0 ]] && die "Run as your normal user, not root - the script uses sudo where needed."
[[ -f /etc/os-release ]] && . /etc/os-release
[[ "${ID:-}" == "ubuntu" ]] || warn "Tested on Ubuntu 22.04/24.04; detected '${PRETTY_NAME:-unknown}'"
RUN_USER="$(id -un)"; RUN_GROUP="$(id -gn)"
mkdir -p "$ROOT"/{logs,data,secrets,models/piper,assets/music,assets/fonts}
# your channel list: created once from the template, then yours to edit (never overwritten)
[[ -f "$ROOT/config/channels.yaml" ]] || cp "$ROOT/config/channels.example.yaml" "$ROOT/config/channels.yaml"
exec > >(tee -a "$ROOT/logs/install.log") 2>&1
cd "$ROOT"
sudo -v || die "sudo access is required"

# ---------------------------------------------------------------- apt
step "System packages"
sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3 python3-venv python3-pip python3-dev build-essential \
    ffmpeg curl ca-certificates git unzip fontconfig fonts-liberation fonts-dejavu-core \
    ubuntu-drivers-common pciutils openssh-server
PYV="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
python3 -c 'import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,13) else 1)' \
    || die "Python $PYV found; 3.10-3.13 required"
ok "Python $PYV, $(ffmpeg -version | head -1 | cut -d' ' -f1-3)"

sudo systemctl enable --now ssh >/dev/null 2>&1 || true
ok "OpenSSH server enabled (manage this PC from your laptop: ssh $(id -un)@$(hostname -I | awk '{print $1}'))"

# ---------------------------------------------------------------- desktop power
if dpkg -s ubuntu-desktop >/dev/null 2>&1 || dpkg -s ubuntu-desktop-minimal >/dev/null 2>&1 || dpkg -s gdm3 >/dev/null 2>&1; then
    step "Ubuntu Desktop detected - power settings"
    if [[ $ALLOW_SLEEP -eq 0 ]]; then
        # GNOME suspends after ~15-20 min idle, even at the login screen; a sleeping PC misses
        # n8n schedules and stalls renders. Masking the sleep targets is the reliable fix.
        sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null
        ok "Suspend/hibernate disabled (undo: sudo systemctl unmask sleep.target suspend.target hibernate.target hybrid-sleep.target)"
    else
        warn "--allow-sleep: the PC may suspend when idle - schedules and renders will pause while it sleeps"
    fi
    echo "    The desktop session uses ~0.3-0.7 GB of VRAM; close games/browsers with video while renders run."
fi

# ---------------------------------------------------------------- driver
step "NVIDIA driver"
if have nvidia-smi && nvidia-smi >/dev/null 2>&1; then
    ok "$(nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader)"
elif [[ $SKIP_DRIVER -eq 1 || $CPU_ONLY -eq 1 ]]; then
    warn "No working NVIDIA driver - continuing (CPU fallbacks will be used)"
else
    lspci | grep -qi nvidia || die "No NVIDIA GPU visible on the PCI bus"
    warn "Installing the recommended NVIDIA driver (full desktop driver - it includes the NVENC library)"
    sudo ubuntu-drivers install || sudo ubuntu-drivers autoinstall
    if mokutil --sb-state 2>/dev/null | grep -qi enabled; then
        warn "Secure Boot is ON: on reboot choose 'Enroll MOK' and enter the password you just set, or the driver won't load."
    fi
    printf '\n\033[1;33mDriver installed. Reboot now (sudo reboot), then run ./install.sh again.\033[0m\n'
    exit 0
fi

if ffmpeg -hide_banner -loglevel error -f lavfi -i color=c=black:s=256x256:d=0.2 -c:v h264_nvenc -f null - 2>/dev/null; then
    ok "NVENC hardware encoding works"
else
    warn "NVENC test failed - rendering will use libx264 (CPU). Use the full 'nvidia-driver-XXX' package, not '-headless'/'-server' compute-only variants."
fi

# ---------------------------------------------------------------- ollama
step "Ollama"
if ! have ollama; then
    curl -fsSL https://ollama.com/install.sh | sh
fi
sudo systemctl enable --now ollama >/dev/null 2>&1 || true
for _ in $(seq 1 30); do curl -fs http://127.0.0.1:11434/api/tags >/dev/null && break; sleep 1; done
curl -fs http://127.0.0.1:11434/api/tags >/dev/null && ok "Ollama $(ollama --version 2>/dev/null | awk '{print $NF}') running" \
    || warn "Ollama not responding on :11434 (sudo systemctl status ollama)"

# ---------------------------------------------------------------- python
step "Python virtual environment"
[[ -x .venv/bin/python ]] || python3 -m venv .venv
.venv/bin/python -m pip install --upgrade --quiet pip wheel
.venv/bin/python -m pip install -r requirements.txt
if [[ $CPU_ONLY -eq 0 ]]; then
    .venv/bin/python -m pip install -r requirements-gpu.txt && ok "CUDA 12 cuBLAS/cuDNN wheels installed" \
        || warn "CUDA wheels failed - Whisper will run on CPU"
fi
if [[ $NO_AI_IMAGES -eq 0 && $CPU_ONLY -eq 0 ]]; then
    .venv/bin/python -m pip install -r requirements-ai.txt && ok "AI image libraries (torch + diffusers) installed" \
        || warn "AI image libraries failed - videos will need clips in assets/broll or a stock API key"
fi
chmod +x ytm.sh scripts/*.sh
ok "Python packages installed"

# ---------------------------------------------------------------- config
step "Configuration"
if [[ ! -f .env ]]; then
    cp .env.example .env
    KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    sed -i "s/^YTM_API_KEY=.*/YTM_API_KEY=$KEY/" .env
    ok "Created .env with a new YTM_API_KEY"
else
    ok ".env exists (kept)"
fi
chmod 600 .env
chmod 700 secrets
API_KEY="$(grep -E '^YTM_API_KEY=' .env | cut -d= -f2-)"
TZ_NAME="$(.venv/bin/python -c 'import yaml; print(yaml.safe_load(open("config/settings.yaml"))["app"].get("timezone","UTC"))')"
# the shipped default is UTC: use this machine's timezone instead (publishing slots are in local time)
SYS_TZ="$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || true)"
if [[ "$TZ_NAME" == "UTC" && -n "$SYS_TZ" && "$SYS_TZ" != "UTC" && "$SYS_TZ" != "Etc/UTC" ]]; then
    sed -i "s|^  timezone: UTC |  timezone: $SYS_TZ |" config/settings.yaml
    TZ_NAME="$SYS_TZ"
    ok "Timezone set to $SYS_TZ (from this machine) in config/settings.yaml"
fi
APP_PORT="$(.venv/bin/python -c 'import yaml; print(yaml.safe_load(open("config/settings.yaml"))["app"].get("port",8765))')"
if [[ $LAN -eq 1 ]]; then
    sed -i -E 's/^(  host:)\s*127\.0\.0\.1/\1 0.0.0.0        /' config/settings.yaml
    ok "Dashboard will listen on all interfaces (LAN mode)"
fi

# ---------------------------------------------------------------- models
if [[ $SKIP_MODELS -eq 0 ]]; then
    step "AI models and fonts"
    if have ollama; then
        if [[ -n "$LLM_MODEL" ]]; then   # only change settings when --llm-model was given (re-runs keep your choice)
            sed -i -E "/^llm:/,/^[a-z]/ s|^(  model:)\s*[^ #]+|\1 $LLM_MODEL|" config/settings.yaml
        else
            LLM_MODEL="$(.venv/bin/python -c 'import yaml; print(yaml.safe_load(open("config/settings.yaml"))["llm"]["model"])')"
        fi
        ollama pull "$LLM_MODEL" && ok "LLM $LLM_MODEL ready" || warn "ollama pull $LLM_MODEL failed"
    fi
    IFS='-' read -r P_LOC P_NAME P_Q <<< "$PIPER_VOICE"
    P_LANG="${P_LOC%%_*}"
    BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/$P_LANG/$P_LOC/$P_NAME/$P_Q/$PIPER_VOICE"
    PIPER_OK=1
    for ext in .onnx .onnx.json; do
        f="models/piper/$PIPER_VOICE$ext"
        [[ -s "$f" ]] || curl -fL --retry 3 -o "$f" "$BASE$ext" || { rm -f "$f"; PIPER_OK=0; }
    done
    if [[ $PIPER_OK -eq 1 ]]; then
        ok "Piper voice $PIPER_VOICE (check its MODEL_CARD licence before monetising)"
    else
        warn "Piper voice download failed - get it from huggingface.co/rhasspy/piper-voices into models/piper/, or use the edge/elevenlabs voice"
    fi
    if [[ ! -s assets/fonts/Anton-Regular.ttf ]]; then
        curl -fsL --retry 3 -o assets/fonts/Anton-Regular.ttf \
            https://github.com/google/fonts/raw/main/ofl/anton/Anton-Regular.ttf \
            && ok "Thumbnail font Anton (SIL OFL)" || warn "Font download failed - thumbnails use Liberation Sans Bold"
    fi
    if [[ $NO_AI_IMAGES -eq 0 && $CPU_ONLY -eq 0 ]]; then
        HF_HOME="$ROOT/models/hf" .venv/bin/python -m app.cli fetch-models \
            && ok "AI models (SDXL images + LTX-Video animation) downloaded" || warn "AI model download failed - retry later: ./ytm.sh fetch-models"
    fi
    echo "    Whisper weights download automatically on first use."
fi

# ---------------------------------------------------------------- database
step "Database"
.venv/bin/python -m app.cli init-db

# ---------------------------------------------------------------- n8n
N8N_BIN=""
if [[ $SKIP_N8N -eq 0 ]]; then
    step "Node.js 24 + n8n"
    NODE_MAJOR="$(node -v 2>/dev/null | sed -E 's/^v([0-9]+).*/\1/' || echo 0)"
    if [[ "${NODE_MAJOR:-0}" -lt 24 ]]; then   # current n8n (2.x) requires Node.js 24+
        curl -fsSL https://deb.nodesource.com/setup_24.x | sudo -E bash -
        sudo apt-get install -y nodejs
    fi
    ok "Node $(node -v)"
    have n8n || sudo npm install -g n8n
    N8N_BIN="$(command -v n8n)"
    ok "n8n $("$N8N_BIN" --version 2>/dev/null | tail -1)"

    LISTEN=127.0.0.1; SECURE=true
    [[ $LAN -eq 1 ]] && { LISTEN=0.0.0.0; SECURE=false; }
    cat > config/n8n.env <<EOF
N8N_USER_FOLDER=$ROOT/data/n8n
N8N_LISTEN_ADDRESS=$LISTEN
N8N_PORT=5678
N8N_SECURE_COOKIE=$SECURE
GENERIC_TIMEZONE=$TZ_NAME
TZ=$TZ_NAME
N8N_DIAGNOSTICS_ENABLED=false
N8N_PERSONALIZATION_ENABLED=false
N8N_RUNNERS_ENABLED=true
EOF
    chmod 600 config/n8n.env

    sudo systemctl stop ytm-n8n 2>/dev/null || true
    TMP="$(mktemp -d)"
    for f in n8n/*.json; do
        sed -e "s/PASTE_YTM_API_KEY/$API_KEY/" -e "s#http://127.0.0.1:8765#http://127.0.0.1:$APP_PORT#" \
            -e "s#\"timezone\": \"UTC\"#\"timezone\": \"$TZ_NAME\"#" "$f" > "$TMP/$(basename "$f")"
    done
    if N8N_USER_FOLDER="$ROOT/data/n8n" "$N8N_BIN" import:workflow --separate --input="$TMP"; then
        ok "5 workflows imported"
        # turn them on: "publish" in n8n 2.x, "active" in n8n 1.x
        PUB_FAIL=0
        for wid in YtmResearch00001 YtmPipeTick00002 YtmReviewAlrt003 YtmAnalytics0004 YtmHealthWtch005; do
            N8N_USER_FOLDER="$ROOT/data/n8n" "$N8N_BIN" publish:workflow --id="$wid" >/dev/null 2>&1 \
                || N8N_USER_FOLDER="$ROOT/data/n8n" "$N8N_BIN" update:workflow --id="$wid" --active=true >/dev/null 2>&1 \
                || PUB_FAIL=1
        done
        if [[ $PUB_FAIL -eq 0 ]]; then ok "workflows published (running on their schedules)"
        else warn "could not publish every workflow - open n8n and click Publish on each"; fi
    else
        warn "workflow import failed - import n8n/*.json manually"
    fi
    rm -rf "$TMP"
fi

# ---------------------------------------------------------------- services
if [[ $NO_SERVICES -eq 0 ]]; then
    step "systemd services"
    render() {
        sed -e "s#@ROOT@#$ROOT#g" -e "s#@USER@#$RUN_USER#g" -e "s#@GROUP@#$RUN_GROUP#g" -e "s#@N8N@#$N8N_BIN#g" "$1" \
            | sudo tee "/etc/systemd/system/$(basename "$1")" >/dev/null
    }
    render deploy/systemd/ytm-api.service
    [[ -n "$N8N_BIN" ]] && render deploy/systemd/ytm-n8n.service
    sudo systemctl daemon-reload
    YTM_SERVICE_USER="$USER" "$ROOT/scripts/setup-power.sh" || warn "dashboard power controls not enabled (./scripts/setup-power.sh)"
    sudo systemctl enable --now ytm-api
    [[ -n "$N8N_BIN" ]] && sudo systemctl enable --now ytm-n8n
    ok "ytm-api$( [[ -n "$N8N_BIN" ]] && echo ' + ytm-n8n') enabled (start on boot)"

    if [[ $LAN -eq 1 ]] && have ufw && sudo ufw status | grep -q "Status: active"; then
        SUBNET="$(ip -o -f inet addr show scope global 2>/dev/null | awk '{print $4}' | head -1 || true)"
        if [[ -n "$SUBNET" ]]; then
            SUBNET="$(.venv/bin/python -c "import ipaddress,sys; print(ipaddress.ip_network(sys.argv[1], strict=False))" "$SUBNET")"
            sudo ufw allow from "$SUBNET" to any port "$APP_PORT" proto tcp >/dev/null
            sudo ufw allow from "$SUBNET" to any port 5678 proto tcp >/dev/null
            ok "ufw: opened $APP_PORT and 5678 to $SUBNET only"
        else
            warn "could not detect your LAN subnet - open ports yourself: sudo ufw allow from 192.168.1.0/24 to any port $APP_PORT"
        fi
    fi
fi

# ---------------------------------------------------------------- check
step "Health check"
sleep 3
./ytm.sh doctor || true

IP="$(hostname -I | awk '{print $1}')"
cat <<EOF

$(printf '\033[1;32mDone.\033[0m')

Next steps
  1. Copy your Google OAuth Desktop client JSON to  $ROOT/secrets/client_secret.json
     and fill YOUTUBE_API_KEY / PEXELS_API_KEY / PIXABAY_API_KEY in .env, then: ./ytm.sh restart
  2. Edit config/channels.yaml, then link each channel:  ./ytm.sh auth <slug>
  3. From your PC open the dashboard and n8n:
EOF
if [[ $LAN -eq 1 ]]; then
    echo "       http://$IP:$APP_PORT   and   http://$IP:5678"
else
    echo "       ssh -N -L $APP_PORT:localhost:$APP_PORT -L 5678:localhost:5678 $RUN_USER@$IP"
    echo "       then http://localhost:$APP_PORT  and  http://localhost:5678"
fi
cat <<EOF
     In n8n: create the owner account. The 5 workflows are already published (running);
     optionally paste a Discord/Slack/Teams webhook URL into each workflow's Config node.
  Dashboard API key: $API_KEY
EOF

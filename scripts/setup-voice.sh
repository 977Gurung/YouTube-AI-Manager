#!/usr/bin/env bash
# Install the expressive narration voice (Chatterbox by Resemble AI, MIT licence) into its own
# virtualenv, .venv-voice. It is kept separate because it needs older library versions than the
# image/animation models. Safe to re-run.
#   ./ytm.sh setup-voice            install + download the model + speak a test sentence
#   ./ytm.sh setup-voice --enable   ...and switch every channel in channels.yaml from piper to chatterbox
set -euo pipefail
# shellcheck source=scripts/env.sh
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
cd "$YTM_ROOT"
ENABLE=0
for a in "$@"; do [[ "$a" == "--enable" ]] && ENABLE=1; done
VENV="$YTM_ROOT/.venv-voice"
PKG="${YTM_VOICE_PACKAGE:-chatterbox-tts}"
say() { printf '\n==> %s\n' "$*"; }

free_gb=$(df -BG --output=avail "$YTM_ROOT" | tail -1 | tr -dc '0-9')
NEED_GB="${YTM_VOICE_MIN_GB:-12}"
if (( free_gb < NEED_GB )); then echo "!! Need about ${NEED_GB} GB free disk space, only ${free_gb} GB left."; exit 1; fi

PYBIN="${YTM_VOICE_PYTHON:-python3}"
pyver=$("$PYBIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
case "$pyver" in 3.10|3.11|3.12|3.13) ;; *) echo "!! Python $pyver is not supported by the voice model (needs 3.10-3.13)."; exit 1 ;; esac

if [[ ! -x "$VENV/bin/python" ]]; then
    say "Creating $VENV (Python $pyver)"
    "$PYBIN" -m venv "$VENV"
fi
rm -f "$VENV/.ready"
say "Installing $PKG (downloads about 3 GB of libraries the first time)"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet "$PKG"
# Chatterbox's watermark library imports pkg_resources, which Python 3.12+ virtualenvs no longer ship
# and setuptools 81+ removed. Without it the voice fails with "'NoneType' object is not callable".
"$VENV/bin/python" -m pip install --quiet "setuptools<81"

say "Downloading the voice model (~3 GB, once) and speaking a test sentence"
if out=$(HF_HOME="$YTM_ROOT/models/hf" "$VENV/bin/python" "$YTM_ROOT/scripts/voice_worker.py" --check 2>"$VENV/check.log" | tail -1); then
    echo "    $out"
    touch "$VENV/.ready"
else
    echo "!! Voice test failed: ${out:-see $VENV/check.log}"
    tail -5 "$VENV/check.log" 2>/dev/null || true
    echo "   Videos keep using the current voice. Fix the problem and run ./ytm.sh setup-voice again."
    exit 1
fi

if (( ENABLE )); then
    say "Switching channels to the expressive voice"
    cp config/channels.yaml "config/channels.yaml.bak.$(date +%Y%m%d-%H%M%S)"
    sed -i -E 's/^([[:space:]]*provider:[[:space:]]*)piper([[:space:]]*(#.*)?)$/\1chatterbox\2/' config/channels.yaml
    "$YTM_PY" -m app.cli sync-channels
    echo "    Done. Channels with 'provider: chatterbox':"
    grep -n "provider: chatterbox" config/channels.yaml || echo "    (none found - set voice.provider: chatterbox yourself)"
else
    cat <<'MSG'

Installed. To use it, set this for a channel in config/channels.yaml:
    voice:
      provider: chatterbox
      exaggeration: 0.6      # emotion strength, 0.3 calm ... 0.9 dramatic
then run:  ./ytm.sh sync-channels      (or re-run: ./ytm.sh setup-voice --enable)
MSG
fi
echo
echo "Hear it:  ./ytm.sh voice-sample <channel-slug>   ->  data/voice-sample-<slug>.wav"

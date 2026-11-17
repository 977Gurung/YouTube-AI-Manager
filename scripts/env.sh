# shellcheck shell=bash
# Sourced by ytm.sh and run-api.sh: project root, venv python, and the CUDA 12 libraries
# (cuBLAS/cuDNN pip wheels) that faster-whisper needs on LD_LIBRARY_PATH.
YTM_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
YTM_PY="$YTM_ROOT/.venv/bin/python"
export YTM_ROOT YTM_PY
export HF_HOME="${HF_HOME:-$YTM_ROOT/models/hf}"   # AI image model cache stays inside the project

if [[ -x "$YTM_PY" ]]; then
    _sp="$("$YTM_PY" -c 'import site; print(site.getsitepackages()[0])' 2>/dev/null || true)"
    for _d in "$_sp"/nvidia/*/lib; do
        [[ -d "$_d" ]] && LD_LIBRARY_PATH="$_d${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    done
    export LD_LIBRARY_PATH
    unset _sp _d
fi

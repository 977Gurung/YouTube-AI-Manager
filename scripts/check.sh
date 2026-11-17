#!/usr/bin/env bash
# Fast static checks (no GPU, no models, about 1 minute). Run locally or in CI:  ./scripts/check.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
PY="${PYTHON:-python3}"
fail=0
step() { printf '\n==> %s\n' "$*"; }

step "Python syntax"
"$PY" -m compileall -q app scripts tests >/dev/null && echo "ok"

step "Shell syntax"
for f in install.sh ytm.sh scripts/*.sh; do bash -n "$f" || fail=1; done && echo "ok"

step "JavaScript syntax"
if command -v node >/dev/null; then for f in app/dashboard/*.js; do node --check "$f" || fail=1; done; echo "ok"; else echo "skipped (node not installed)"; fi

step "YAML and JSON files parse"
"$PY" - <<'PY'
import json, pathlib, yaml
for p in ["config/settings.yaml", "config/channels.example.yaml"]:
    yaml.safe_load(open(p, encoding="utf-8"))
for p in list(pathlib.Path("n8n").glob("*.json")) + [pathlib.Path("app/dashboard/manifest.json")]:
    json.loads(p.read_text(encoding="utf-8"))
print("ok")
PY

step "Version is the same everywhere (VERSION, app/__init__.py, CHANGELOG.md)"
"$PY" - <<'PY'
import re, pathlib
v = pathlib.Path("VERSION").read_text().strip()
code = re.search(r'__version__ = "([^"]+)"', pathlib.Path("app/__init__.py").read_text()).group(1)
log = re.search(r"^## \[(\d+\.\d+\.\d+)\]", pathlib.Path("CHANGELOG.md").read_text(), re.M).group(1)
assert re.fullmatch(r"\d+\.\d+\.\d+", v) and v == code == log, (v, code, log)
print("ok", v)
PY

step "Scripts are executable in git (a commit from Windows can drop the flag)"
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    noexec=$(git ls-files -s install.sh ytm.sh 'scripts/*.sh' scripts/voice_worker.py tests/live/fake-nvidia-smi | awk '$1 != "100755" {print $4}')
    [[ -z "$noexec" ]] && echo "ok" || { echo "!! not executable in git - fix: git update-index --chmod=+x <file>"; echo "$noexec"; fail=1; }
else
    echo "skipped (not a git checkout)"
fi

step "No secrets or personal config committed"
bad=$(git ls-files 2>/dev/null | grep -E '(^|/)\.env$|client_secret.*\.json|(^|/)config/channels\.yaml$|(^|/)config/n8n\.env$|^secrets/[^.]|token.*\.json' || true)
[[ -z "$bad" ]] && echo "ok" || { echo "!! these must not be in git:"; echo "$bad"; fail=1; }
KEYS="(AIza[0-9A-Za-z_-]{30,}|GOCSPX-[0-9A-Za-z_-]{10,}|ya29\.[0-9A-Za-z_-]{20,}|sk-[A-Za-z0-9]{32,}|ghp_[A-Za-z0-9]{30,}|hf_[A-Za-z0-9]{30,}|xox[bp]-[0-9A-Za-z-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    # only what git would publish (tracked + new, not ignored): your real .env / secrets on a server are fine
    hits=$( { git ls-files -z; git ls-files -z --others --exclude-standard; } | xargs -0 -r grep -nIE "$KEYS" 2>/dev/null || true)
else
    hits=$(grep -rnIE "$KEYS" --exclude=.env --exclude-dir=.git --exclude-dir=.venv --exclude-dir=.venv-voice \
           --exclude-dir=data --exclude-dir=models --exclude-dir=secrets --exclude-dir=node_modules . || true)
fi
[[ -z "$hits" ]] && echo "ok (no key patterns)" || { echo "!! possible secrets:"; echo "$hits"; fail=1; }

step "Fast unit tests (parsers, captions, scheduling helpers)"
if "$PY" -c "import pytest, fastapi" 2>/dev/null; then
    YTM_API_KEY= "$PY" -m pytest -q -p no:cacheprovider \
        "tests/test_sysmon.py::test_cpu_parsing_and_percent" "tests/test_sysmon.py::test_memory_parsing" \
        "tests/test_sysmon.py::test_network_and_disk_parsing" "tests/test_sysmon.py::test_gpu_parsing" \
        "tests/test_sysmon.py::test_secrets_are_redacted_from_command_lines" \
        "tests/test_voice_expressive.py::test_split_text_sentences_and_limits" || fail=1
else
    echo "skipped (pip install -r requirements.txt -r requirements-dev.txt)"
fi

[[ $fail -eq 0 ]] && printf '\nAll checks passed.\n' || { printf '\nSome checks FAILED.\n'; exit 1; }

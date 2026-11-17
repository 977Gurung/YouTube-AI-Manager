#!/usr/bin/env bash
# ExecStart for the ytm-api systemd service: API + dashboard + GPU worker.
set -euo pipefail
# shellcheck source=scripts/env.sh
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
cd "$YTM_ROOT"
# fewer malloc arenas = less memory kept reserved by the many worker threads
export MALLOC_ARENA_MAX="${MALLOC_ARENA_MAX:-2}"
exec "$YTM_PY" -m app.server

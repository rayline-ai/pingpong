#!/usr/bin/env bash
# `pingpong model` — point the reviewer and the coder at endpoints and models.
#
#   pingpong model                          choose interactively
#   pingpong model coder openai-direct gpt-5.6
#   pingpong model --show
#
# A launcher, nothing more: src/models.py is the whole command. Host-side like
# accounts and onboard, because it writes `.env` and rayline/pingpong.json, and
# because it has to work before `up` — the point of it is choosing what the
# agents run on, and an editor that lives only inside the agents is no use for
# that.
set -euo pipefail

cd "$(dirname "$0")"

# Run one, do not look one up. On Windows `python3` and `python` are Store stubs
# on PATH that `command -v` finds happily and that exit 49 without running
# anything, so the only test that means something is whether it executes.
PY=''
for candidate in python3 python py; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 7) else 1)' \
       >/dev/null 2>&1; then
        PY=$candidate
        break
    fi
done

if [ -z "$PY" ]; then
    cat >&2 <<'EOF'
model: no working python3 on this host, and this command runs here rather than
in a container (it writes .env, and it has to work before `up`).

Install Python 3, or make the same edit by hand: rayline/pingpong.json holds one
entry per role under routes.model_routes, and its own comment says what each
endpoint needs. Then `docker compose up -d reviewer coder`.
EOF
    exit 1
fi

exec "$PY" -m src.models "$@"

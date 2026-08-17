#!/usr/bin/env bash
# Bring up this container's Rayline router, write the Hermes config that points at
# it, then idle so the API can `docker exec hermes -z ...` per round.
set -euo pipefail

: "${HERMES_MODEL_ALIAS:?HERMES_MODEL_ALIAS must be set (reviewer-brain|coder-brain)}"
: "${RAYLINE_ROUTER_API_KEY:?RAYLINE_ROUTER_API_KEY must be set}"

RAYLINE_CONFIG=${RAYLINE_CONFIG:-/etc/pingpong/rayline.json}
INJECTOR=${INJECTOR:-http://127.0.0.1:20809}

# ANTHROPIC_BASE_URL, set as image ENV, is what actually redirects `hermes -z` —
# config.yaml's base_url alone is ignored on that path (see agent.Dockerfile). An
# exported var here would not help: `docker exec` does not inherit this shell.
# So if someone overrides INJECTOR, the two silently disagree. Fail loudly.
if [ "${ANTHROPIC_BASE_URL:-}" != "${INJECTOR}" ]; then
    echo "[agent] FATAL: ANTHROPIC_BASE_URL='${ANTHROPIC_BASE_URL:-}' != INJECTOR='${INJECTOR}'." >&2
    echo "[agent] hermes would bypass the router. Set both or neither." >&2
    exit 1
fi

echo "[agent] role alias: ${HERMES_MODEL_ALIAS}"
echo "[agent] rayline config: ${RAYLINE_CONFIG}"

rayline router start --config "${RAYLINE_CONFIG}" &

# The injector is the direct Anthropic endpoint. :20810 is a CONNECT-style MITM
# proxy — a plain POST to it returns 405 — so we wait on and use :20809.
for _ in $(seq 1 60); do
    if curl -sf -o /dev/null "${INJECTOR}/v1/models" \
       || curl -s -o /dev/null -w '%{http_code}' "${INJECTOR}/" | grep -qv '^000$'; then
        echo "[agent] injector up at ${INJECTOR}"
        break
    fi
    sleep 1
done

mkdir -p /root/.hermes
cat > /root/.hermes/config.yaml <<YAML
model:
  provider: anthropic
  base_url: ${INJECTOR}
  # An arbitrary alias, not a model id. Rayline resolves it via routes.model_routes.
  default: ${HERMES_MODEL_ALIAS}

agent:
  max_turns: 60

# Each round must be a function of the diff it is given and nothing else. Memory
# and a user profile would let round N be shaped by round N-1 through a channel
# that never appears in the transcript.
memory:
  memory_enabled: false
  user_profile_enabled: false

approvals:
  mode: auto

display:
  file_mutation_verifier: true
YAML

echo "[agent] ready"
exec sleep infinity

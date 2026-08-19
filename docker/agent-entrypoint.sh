#!/usr/bin/env bash
# Bring up this container's Rayline router, write the Hermes config that points at
# it, then idle so the API can `docker exec hermes -z ...` per round.
set -euo pipefail

: "${HERMES_MODEL_ALIAS:?HERMES_MODEL_ALIAS must be set (reviewer-brain|coder-brain)}"

RAYLINE_CONFIG=${RAYLINE_CONFIG:-/etc/pingpong/rayline.json}
INJECTOR=${INJECTOR:-http://127.0.0.1:20809}

# ANTHROPIC_API_KEY is the placeholder Hermes sends to the injector, baked into
# the image. A real key here is not a stronger version of that — it is the one
# thing that turns "the router got bypassed" from an instant 401 into a working
# round that ignores every route in the config and bills the provider directly.
# Router credentials are RAYLINE_-prefixed for exactly this reason.
case "${ANTHROPIC_API_KEY:-}" in
    sk-ant-rayline-injector*) ;;
    *)  echo "[agent] FATAL: ANTHROPIC_API_KEY has been overridden." >&2
        echo "[agent] That name belongs to Hermes' injector placeholder. Put an" >&2
        echo "[agent] Anthropic key in .env as ANTHROPIC_API_KEY and let compose" >&2
        echo "[agent] pass it as RAYLINE_ANTHROPIC_API_KEY instead." >&2
        exit 1 ;;
esac

# Which credential this role needs is not fixed — it is whatever the endpoint
# behind its alias names, and `ollama-local` names none. Ask the config rather
# than requiring a Rayline key that a role on OpenAI would never use. Empty is
# reported here, at start, rather than as a failed round half an hour later.
KEY_ENV=$(jq -r --arg alias "${HERMES_MODEL_ALIAS}" '
    .routes.model_routes[$alias].endpoint as $id
    | (.endpoints[] | select(.id == $id) | .api_key_env) // empty
' "${RAYLINE_CONFIG}" 2>/dev/null || true)

if [ -z "${KEY_ENV}" ]; then
    echo "[agent] ${HERMES_MODEL_ALIAS} needs no credential (local endpoint)"
elif [ -z "${!KEY_ENV:-}" ]; then
    # Report the name the operator can act on. docker-compose.yml maps .env's
    # ANTHROPIC_API_KEY to the container's RAYLINE_ANTHROPIC_API_KEY, so naming
    # the container variable would send them looking for something not in .env.
    DOTENV_NAME=$(printf '%s' "${KEY_ENV}" | sed 's/^RAYLINE_ANTHROPIC_/ANTHROPIC_/; s/^RAYLINE_OPENAI_/OPENAI_/; s/^RAYLINE_OPENROUTER_/OPENROUTER_/')
    echo "[agent] FATAL: ${HERMES_MODEL_ALIAS} routes to an endpoint that needs" >&2
    echo "[agent] ${DOTENV_NAME}, and it is empty. Set it in .env and re-run" >&2
    echo "[agent] ./pingpong up, or point the alias at another endpoint in" >&2
    echo "[agent] rayline/pingpong.json." >&2
    exit 1
else
    echo "[agent] credential: ${KEY_ENV} is set"
fi

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

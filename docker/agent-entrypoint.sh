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

# What this role needs is not fixed — it is whatever the endpoint behind its
# alias declares. Ask the config rather than requiring a Rayline key that a role
# on OpenAI or on ollama would never use. Whatever is missing is reported here,
# at start, rather than as a round that dies half an hour in.
ROUTE=$(jq -r --arg alias "${HERMES_MODEL_ALIAS}" '
    .routes.model_routes[$alias] as $r
    | if $r == null then ""
      elif ($r.endpoint // "") == "" then "UNCHOSEN"
      else (.endpoints[] | select(.id == $r.endpoint)) as $e
           | [$r.endpoint, $r.model, ($e.api_key_env // ""), ($e.base_url // "")] | .[]
      end
' "${RAYLINE_CONFIG}" 2>/dev/null || true)

# Blank is what ships, so it is worth its own message: nothing is broken, a step
# has not been run. `./pingpong up` checks this before compose and never gets
# here — this is the path for someone driving `docker compose` directly.
if [ "${ROUTE}" = "UNCHOSEN" ]; then
    echo "[agent] FATAL: no brain chosen for ${HERMES_MODEL_ALIAS}. Nothing ships" >&2
    echo "[agent] chosen — run ./pingpong model on the host, then ./pingpong up." >&2
    exit 1
fi
if [ -z "${ROUTE}" ]; then
    echo "[agent] FATAL: ${HERMES_MODEL_ALIAS} names no endpoint that exists in" >&2
    echo "[agent] ${RAYLINE_CONFIG}. Pick one with ./pingpong model." >&2
    exit 1
fi
# One field per line, not a delimited row: an endpoint with no api_key_env has an
# empty field in the middle, and `read` collapses runs of IFS *whitespace* — so a
# tab-separated row would silently shift base_url into KEY_ENV.
{ read -r ENDPOINT; read -r MODEL; read -r KEY_ENV; read -r BASE_URL; } <<<"${ROUTE}"
echo "[agent] ${HERMES_MODEL_ALIAS} -> ${ENDPOINT} / ${MODEL}"

if [ -n "${KEY_ENV}" ]; then
    if [ -z "${!KEY_ENV:-}" ]; then
        # Report the name the operator can act on. docker-compose.yml maps .env's
        # ANTHROPIC_API_KEY to the container's RAYLINE_ANTHROPIC_API_KEY, so naming
        # the container variable would send them looking for something not in .env.
        DOTENV_NAME=$(printf '%s' "${KEY_ENV}" | sed 's/^RAYLINE_ANTHROPIC_/ANTHROPIC_/; s/^RAYLINE_OPENAI_/OPENAI_/; s/^RAYLINE_OPENROUTER_/OPENROUTER_/')
        echo "[agent] FATAL: ${ENDPOINT} needs ${DOTENV_NAME}, and it is empty." >&2
        echo "[agent] Set it in .env and re-run ./pingpong up, or choose another" >&2
        echo "[agent] endpoint with ./pingpong model." >&2
        exit 1
    fi
    echo "[agent] credential: ${KEY_ENV} is set"
else
    # A keyless endpoint is a local one, and "no key to check" must not become
    # "nothing to check" — that is how the default install fails on a host with
    # no ollama: silently, at the first request of the first round.
    echo "[agent] ${ENDPOINT} needs no credential; checking it answers instead"
    if ! curl -s -o /dev/null --max-time 5 "${BASE_URL}"; then
        echo "[agent] FATAL: nothing answers at ${BASE_URL}." >&2
        echo "[agent] ${ENDPOINT} is a model on this machine, not a service in the" >&2
        echo "[agent] stack: install ollama (https://ollama.com), start it, and" >&2
        echo "[agent] pull the model — or switch to a hosted provider with" >&2
        echo "[agent] ./pingpong model." >&2
        exit 1
    fi
    # /api/tags is ollama's. A keyless endpoint that is something else answers
    # differently, and then there is nothing more to check here.
    TAGS=$(curl -s --max-time 5 "${BASE_URL}/api/tags" 2>/dev/null || true)
    if printf '%s' "${TAGS}" | jq -e '.models' >/dev/null 2>&1; then
        if ! printf '%s' "${TAGS}" | jq -e --arg m "${MODEL}" \
             '.models[] | select(.name == $m or .model == $m)' >/dev/null 2>&1; then
            echo "[agent] FATAL: ollama answers, but has no model '${MODEL}'." >&2
            echo "[agent] A stock tag is not enough — the context window has to be" >&2
            echo "[agent] pinned or Hermes' tool definitions are truncated out of" >&2
            echo "[agent] the prompt and the model narrates commands instead of" >&2
            echo "[agent] calling them:" >&2
            echo "[agent]   printf 'FROM <base>\\nPARAMETER num_ctx 32768\\n' > Modelfile" >&2
            echo "[agent]   ollama create ${MODEL} -f Modelfile" >&2
            exit 1
        fi
        echo "[agent] ollama has ${MODEL}"
    fi
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

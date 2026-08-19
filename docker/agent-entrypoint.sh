#!/usr/bin/env bash
# Give this container a brain, write the Hermes config that reaches it, then idle
# so the API can `docker exec hermes-run -z ...` per round.
#
# Three ways to have a brain, and they share nothing but the idle at the end.
# AGENT_MODE, not REVIEWER_MODE or CODER_MODE: this container does not know which
# role it is running, and compose renames the role's setting on the way in. That
# is what lets the two roles be on different modes without two of everything
# here.
#
#   router      Rayline routes every request. rayline/pingpong.json picks the
#               model per role; credentials belong to the router, not to Hermes.
#   claude-sub  No router. Hermes uses this host's own Claude login, mounted at
#               /credentials, and SUBSCRIPTION_MODEL names the model directly.
#   codex-sub   No router either, but the session is Hermes' own, made once by
#               `./pingpong login` and kept in the volume at HERMES_HOME.
#
# The subscription modes are not a Rayline feature: `rayline router start`
# accepts a `subscription` main route only by deleting it, after which the router
# falls back to a keyed endpoint and bills it. Hermes speaks to both providers
# itself, so the honest way to run on a subscription is with no router at all.
set -euo pipefail

: "${HERMES_MODEL_ALIAS:?HERMES_MODEL_ALIAS must be set (reviewer-brain|coder-brain)}"

AGENT_MODE=${AGENT_MODE:-router}
RAYLINE_CONFIG=${RAYLINE_CONFIG:-/etc/pingpong/rayline.json}
INJECTOR=${INJECTOR:-http://127.0.0.1:20809}
CREDENTIALS=${CREDENTIALS:-/credentials}
# Where Hermes keeps config.yaml and, in codex-sub, its OAuth session. The
# default is the image's own populated home; compose/<role>.codex-sub.yml points
# it at a volume instead, and says there why that is a separate directory rather
# than a mount over this one.
HERMES_HOME=${HERMES_HOME:-/root/.hermes}

# ANTHROPIC_API_KEY is the placeholder Hermes sends to the injector, baked into
# the image. A real key here is not a stronger version of that — in router mode
# it is the one thing that turns "the router got bypassed" from an instant 401
# into a working round that ignores every route in the config and bills the
# provider directly, and in a subscription mode it outranks the credential file
# in Hermes' own resolver and quietly buys API tokens instead. Router credentials
# are RAYLINE_-prefixed for exactly this reason.
case "${ANTHROPIC_API_KEY:-}" in
    sk-ant-rayline-injector*) ;;
    *)  echo "[agent] FATAL: ANTHROPIC_API_KEY has been overridden." >&2
        echo "[agent] That name belongs to Hermes' injector placeholder. Put an" >&2
        echo "[agent] Anthropic key in .env as ANTHROPIC_API_KEY and let compose" >&2
        echo "[agent] pass it as RAYLINE_ANTHROPIC_API_KEY instead." >&2
        exit 1 ;;
esac

mkdir -p "${HERMES_HOME}"


# ---------------------------------------------------------------------------
# claude-sub / codex-sub
# ---------------------------------------------------------------------------

# The role's own settings in .env, for messages. This container is deliberately
# told only AGENT_MODE and SUBSCRIPTION_MODEL — it does not know which role it is
# — but the operator has to edit REVIEWER_* or CODER_*, so naming the wrong one
# would send them looking for a line that is not there. The alias is the one
# thing here that does say which role this is.
env_prefix() {
    case "${HERMES_MODEL_ALIAS}" in
        coder-*) echo CODER ;;
        *)       echo REVIEWER ;;
    esac
}

# Both subscription modes need a real model id, and neither has a router to get
# one from: routes.model_routes is Rayline's, and Rayline is not in this path.
# Without this Hermes asks the provider for a model literally called
# "reviewer-brain".
require_model() {
    local prefix
    prefix=$(env_prefix)
    if [ -z "${SUBSCRIPTION_MODEL:-}" ]; then
        echo "[agent] FATAL: ${prefix}_MODE=${AGENT_MODE} but ${prefix}_MODEL is empty." >&2
        echo "[agent] Nothing resolves a role alias without a router. Set the real" >&2
        echo "[agent] model id in .env and re-run ./pingpong up." >&2
        exit 1
    fi
}

# No base_url. That is the whole difference from router mode: the request goes to
# the provider, authenticated as a subscriber, and rayline/pingpong.json is not
# consulted by anything here.
write_subscription_config() {
    local provider=$1
    echo "[agent] ${HERMES_MODEL_ALIAS} -> ${provider} / ${SUBSCRIPTION_MODEL} (subscription)"
    cat > "${HERMES_HOME}/config.yaml" <<YAML
model:
  provider: ${provider}
  default: ${SUBSCRIPTION_MODEL}

$(hermes_common)
YAML
}

start_claude_sub() {
    local credential=/root/.claude/.credentials.json
    local token expires_at now_ms

    require_model

    if [ ! -d "${CREDENTIALS}" ]; then
        echo "[agent] FATAL: nothing is mounted at ${CREDENTIALS}." >&2
        echo "[agent] $(env_prefix)_MODE=claude-sub needs this host's Claude login." >&2
        echo "[agent] Start with ./pingpong up, which adds the overlay for this role" >&2
        echo "[agent] and fills CREDENTIALS_DIR in .env." >&2
        exit 1
    fi

    # A link, not a copy. The token expires and Hermes refreshes it in place; a
    # copy would work until the first refresh and then diverge from the host,
    # leaving two credentials that each go stale in their own time.
    ln -sfn "${CREDENTIALS}" /root/.claude

    if [ ! -f "${credential}" ]; then
        echo "[agent] FATAL: ${CREDENTIALS} has no .credentials.json." >&2
        echo "[agent] That file is what a login writes, so this is a host that has" >&2
        echo "[agent] not signed in — or CREDENTIALS_DIR points at the wrong one." >&2
        exit 1
    fi

    # The exact field Hermes reads, not merely "the file exists": a directory
    # from an abandoned or partial login has the file and no token in it, and
    # that failure would otherwise surface as a 401 mid-round.
    token=$(jq -r '.claudeAiOauth.accessToken // ""' "${credential}" 2>/dev/null || true)
    if [ -z "${token}" ]; then
        echo "[agent] FATAL: .credentials.json holds no access token." >&2
        echo "[agent] Sign in again on the host, then ./pingpong up." >&2
        exit 1
    fi
    echo "[agent] claude-sub: .credentials.json has a token"

    # Expiry is a warning, not a failure. The file carries a refresh token and
    # Hermes uses it; refusing to start on an expired access token would refuse
    # in exactly the case the design already handles.
    expires_at=$(jq -r '.claudeAiOauth.expiresAt // 0' "${credential}" 2>/dev/null || echo 0)
    now_ms=$(( $(date +%s) * 1000 ))
    if [ "${expires_at}" != "0" ] && [ "${expires_at}" -lt "${now_ms}" ]; then
        echo "[agent] note: the access token has expired; Hermes will refresh it"
    fi

    write_subscription_config anthropic
}

start_codex_sub() {
    require_model

    # Nothing from the host, and nothing to link. Codex rotates its refresh token
    # on every refresh and Hermes does not write the new one back to ~/.codex, so
    # borrowing that file would work once and then revoke the operator's own
    # `codex` CLI. This agent holds a session of its own instead.
    if [ "${HERMES_HOME}" = "/root/.hermes" ]; then
        echo "[agent] FATAL: codex-sub needs HERMES_HOME on a volume, and it is" >&2
        echo "[agent] the image's own home. The session would be lost on every" >&2
        echo "[agent] restart and ./pingpong login would have to be run again each" >&2
        echo "[agent] time. Start with ./pingpong up, which adds the overlay this" >&2
        echo "[agent] role's $(env_prefix)_MODE calls for." >&2
        exit 1
    fi

    # Not fatal, deliberately: the login happens THROUGH this container, so
    # refusing to start would make it impossible to fix. The agent idles with a
    # brain that answers 'no credentials' until `./pingpong login` runs, and
    # `pingpong doctor` reports the same thing from outside.
    #
    # Both stores, because a session can be in either: `hermes auth add` writes a
    # credential_pool entry — which is what the runtime selects from — while the
    # older singleton at providers.openai-codex.tokens is what an import leaves.
    if jq -e '((.credential_pool["openai-codex"] // []) | length) > 0
              or ((.providers["openai-codex"].tokens.access_token // "") != "")' \
         "${HERMES_HOME}/auth.json" >/dev/null 2>&1; then
        echo "[agent] codex-sub: ${HERMES_HOME} has a ChatGPT session"
    else
        echo "[agent] codex-sub: no ChatGPT session in ${HERMES_HOME}."
        echo "[agent] Run ./pingpong login on the host — it opens a device-code"
        echo "[agent] sign-in for each agent on this mode. Rounds fail until it has."
    fi

    write_subscription_config openai-codex
}


# ---------------------------------------------------------------------------
# router
# ---------------------------------------------------------------------------

start_router() {
    # What this role needs is not fixed — it is whatever the endpoint behind its
    # alias declares. Ask the config rather than requiring a Rayline key that a
    # role on OpenAI or on ollama would never use. Whatever is missing is
    # reported here, at start, rather than as a round that dies half an hour in.
    local ROUTE ENDPOINT MODEL KEY_ENV BASE_URL DOTENV_NAME TAGS
    ROUTE=$(jq -r --arg alias "${HERMES_MODEL_ALIAS}" '
        .routes.model_routes[$alias] as $r
        | if $r == null then ""
          elif ($r.endpoint // "") == "" then "UNCHOSEN"
          else (.endpoints[] | select(.id == $r.endpoint)) as $e
               | [$r.endpoint, $r.model, ($e.api_key_env // ""), ($e.base_url // "")] | .[]
          end
    ' "${RAYLINE_CONFIG}" 2>/dev/null || true)

    # Blank is what ships, so it is worth its own message: nothing is broken, a
    # step has not been run. `./pingpong up` checks this before compose and never
    # gets here — this is the path for someone driving `docker compose` directly.
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
    # One field per line, not a delimited row: an endpoint with no api_key_env has
    # an empty field in the middle, and `read` collapses runs of IFS *whitespace* —
    # so a tab-separated row would silently shift base_url into KEY_ENV.
    { read -r ENDPOINT; read -r MODEL; read -r KEY_ENV; read -r BASE_URL; } <<<"${ROUTE}"
    echo "[agent] ${HERMES_MODEL_ALIAS} -> ${ENDPOINT} / ${MODEL}"

    if [ -n "${KEY_ENV}" ]; then
        if [ -z "${!KEY_ENV:-}" ]; then
            # Report the name the operator can act on. docker-compose.yml maps
            # .env's ANTHROPIC_API_KEY to the container's
            # RAYLINE_ANTHROPIC_API_KEY, so naming the container variable would
            # send them looking for something not in .env.
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

    # ANTHROPIC_BASE_URL, set as image ENV, is what actually redirects `hermes -z`
    # — config.yaml's base_url alone is ignored on that path (see
    # agent.Dockerfile). An exported var here would not help: `docker exec` does
    # not inherit this shell. So if someone overrides INJECTOR, the two silently
    # disagree. Fail loudly.
    if [ "${ANTHROPIC_BASE_URL:-}" != "${INJECTOR}" ]; then
        echo "[agent] FATAL: ANTHROPIC_BASE_URL='${ANTHROPIC_BASE_URL:-}' != INJECTOR='${INJECTOR}'." >&2
        echo "[agent] hermes would bypass the router. Set both or neither." >&2
        exit 1
    fi

    echo "[agent] rayline config: ${RAYLINE_CONFIG}"
    rayline router start --config "${RAYLINE_CONFIG}" &

    # The injector is the direct Anthropic endpoint. :20810 is a CONNECT-style
    # MITM proxy — a plain POST to it returns 405 — so we wait on and use :20809.
    for _ in $(seq 1 60); do
        if curl -sf -o /dev/null "${INJECTOR}/v1/models" \
           || curl -s -o /dev/null -w '%{http_code}' "${INJECTOR}/" | grep -qv '^000$'; then
            echo "[agent] injector up at ${INJECTOR}"
            break
        fi
        sleep 1
    done

    cat > "${HERMES_HOME}/config.yaml" <<YAML
model:
  provider: anthropic
  base_url: ${INJECTOR}
  # An arbitrary alias, not a model id. Rayline resolves it via routes.model_routes.
  default: ${HERMES_MODEL_ALIAS}

$(hermes_common)
YAML
}


# Everything the two modes agree on. Kept in one place so a change to the loop's
# behaviour cannot land in one mode and not the other.
hermes_common() {
    cat <<'YAML'
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
}


echo "[agent] role alias: ${HERMES_MODEL_ALIAS}"
echo "[agent] mode: ${AGENT_MODE}"

case "${AGENT_MODE}" in
    router)      start_router ;;
    claude-sub)  start_claude_sub ;;
    codex-sub)   start_codex_sub ;;
    *)  echo "[agent] FATAL: $(env_prefix)_MODE='${AGENT_MODE}' is not a mode." >&2
        echo "[agent] There is router, claude-sub and codex-sub." >&2
        exit 1 ;;
esac

echo "[agent] ready"
exec sleep infinity

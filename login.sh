#!/usr/bin/env bash
# `pingpong login` — give an agent its own ChatGPT session, for a role whose
# REVIEWER_MODE / CODER_MODE is codex-sub.
#
#   pingpong login              sign in whichever codex-sub agents have none yet
#   pingpong login --force      sign in again even if they have one
#   pingpong login reviewer     just that one
#
# One sign-in per agent, and that is the point rather than an oversight. Codex
# rotates its refresh token on every refresh, so two agents sharing one grant
# would race: whichever refreshed second would find its copy revoked. Two grants
# never touch each other.
#
# Nothing here reads ~/.codex either. Borrowing the Codex CLI's token would work
# until the first refresh and then leave YOUR `codex` command signed out — a
# failure that lands on your terminal rather than in this stack's logs.
#
# Host-side because it needs a terminal: the device-code flow prints a URL and a
# code and then waits for you, which is not something the API container can do on
# your behalf.
set -euo pipefail

cd "$(dirname "$0")"

die()  { printf 'login: %s\n' "$*" >&2; exit 1; }
step() { printf '\n== %s\n' "$*"; }
have() { printf '   already %s\n' "$*"; }

FORCE=0
SERVICES=()

while [ $# -gt 0 ]; do
    case "$1" in
        --force)         FORCE=1; shift ;;
        reviewer|coder)  SERVICES+=("$1"); shift ;;
        -h|--help)
            cat >&2 <<'EOF'
usage: pingpong login [--force] [reviewer|coder]

  Signs an agent in to ChatGPT with a device code, for a role whose mode is
  codex-sub. With no arguments it does every such role, skipping any that
  already have a session. --force signs in again regardless.
EOF
            exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

# Only the assignment is read; the rest of .env is prose and secrets. Same
# reading as the `pingpong` wrapper's, kept deliberately simple for that reason.
role_mode() {
    local value=""
    if [ -f .env ]; then
        value=$(sed -n "s/^$(echo "$1" | tr a-z A-Z)_MODE=[[:space:]]*//p" .env \
                | tail -1 | tr -d "\"'\r")
    fi
    echo "${value:-router}"
}

# Per role, because the roles need not be on the same mode: signing in the coder
# is no help to a reviewer on the router, and doing it silently would look like
# the reviewer had been dealt with.
explain() {
    case "$(role_mode "$1")" in
        claude-sub)
            die "the $1 is on claude-sub and needs no login here — it reads the
     Claude login this host already has. Run \`claude\` and sign in if it
     cannot find one." ;;
        *)
            die "the $1 routes through Rayline and signs in with a key, not a
     subscription. Set $(echo "$1" | tr a-z A-Z)_MODE=codex-sub in .env first, or use
     ./pingpong model to choose its key-based brain." ;;
    esac
}

if [ ${#SERVICES[@]} -gt 0 ]; then
    # Named explicitly: say why that one is not eligible rather than skipping it.
    for service in "${SERVICES[@]}"; do
        [ "$(role_mode "${service}")" = "codex-sub" ] || explain "${service}"
    done
else
    for service in reviewer coder; do
        if [ "$(role_mode "${service}")" = "codex-sub" ]; then
            SERVICES+=("${service}")
        fi
    done
    [ ${#SERVICES[@]} -gt 0 ] || die "no role is on codex-sub. Set REVIEWER_MODE=codex-sub or
     CODER_MODE=codex-sub in .env, run ./pingpong up, then try again."
fi

# Only the overlays for the roles being signed in — the codex-sub file for a role
# that is not on it would move that agent's Hermes home onto an empty volume.
COMPOSE=(docker compose -f docker-compose.yml)
for service in "${SERVICES[@]}"; do
    COMPOSE+=(-f "compose/${service}.codex-sub.yml")
done

# Whether this agent already holds a grant. The credential_pool is what the
# runtime selects from, so that is what counts as "signed in" — the older
# singleton is checked too because an imported session lands there instead.
signed_in() {
    "${COMPOSE[@]}" exec -T "$1" bash -lc '
        jq -e "((.credential_pool[\"openai-codex\"] // []) | length) > 0
               or ((.providers[\"openai-codex\"].tokens.access_token // \"\") != \"\")" \
           "${HERMES_HOME:-/root/.hermes}/auth.json"' >/dev/null 2>&1
}

# .env says codex-sub, but the container that is actually running may predate
# that edit — `up` has to have replaced it for the volume to be there. Signing in
# to a container whose Hermes home is the image's own would appear to work and
# then lose the session at the next restart, which is the one failure a one-time
# interactive step must not have.
home_is_a_volume() {
    [ "$("${COMPOSE[@]}" exec -T "$1" printenv HERMES_HOME 2>/dev/null | tr -d '\r')" \
      = "/hermes" ]
}

for service in "${SERVICES[@]}"; do
    step "${service}"

    "${COMPOSE[@]}" ps --status running --services 2>/dev/null | grep -qx "${service}" \
        || die "the ${service} container is not running. Start the stack with
     ./pingpong up first — the sign-in happens inside it, so that its
     session lands in the volume that survives a restart."

    home_is_a_volume "${service}" \
        || die "the ${service} container is not running in codex-sub: its Hermes
     home is the image's own, so a session made now would be gone at the
     next restart. Run ./pingpong up to replace it, then try again."

    if [ "${FORCE}" -eq 0 ] && signed_in "${service}"; then
        have "signed in — pass --force to replace the session"
        continue
    fi

    printf '   Open the URL below, enter the code, and come back.\n\n'

    # Not -T: the flow polls until you finish, and without a terminal it cannot
    # be interrupted with Ctrl-C. --no-browser because there is no browser in
    # the container to open, and it would print nothing if it thought there was.
    "${COMPOSE[@]}" exec "${service}" \
        hermes auth add openai-codex --type oauth --no-browser \
        || die "sign-in failed or was cancelled for ${service}. Nothing was
     changed; run ./pingpong login again when ready."
done

step 'done'
printf '   ./pingpong doctor to confirm, then open a pull request.\n'

#!/usr/bin/env bash
# `pingpong onboard <path>` — put a folder that already has history onto the
# instance: create the repository, add the bots, seed main, install AGENTS.md,
# register the webhook.
#
# Host-side on purpose. The API container can see neither the operator's folder
# nor their ~/.netrc, and this needs `docker compose exec forgejo` besides.
#
# Every step is idempotent: it checks the instance for what it is about to do
# and reports "already" rather than failing, so a run interrupted halfway can
# simply be repeated.
set -euo pipefail

HOOK_TARGET='http://api:8080/webhook'   # inside the compose network; never API_PORT

die()  { printf 'onboard: %s\n' "$*" >&2; exit 1; }
step() { printf '\n== %s\n' "$*"; }
ok()   { printf '   ok      %s\n' "$*"; }
have() { printf '   already %s\n' "$*"; }

usage() {
    cat >&2 <<'EOF'
usage: pingpong onboard <path-to-repo> [--owner <login>] [--repo <name>]

  <path>    a git repository on this machine, with at least one commit
  --owner   Forgejo account to own it (default: matched from the repo's
            git config user.email)
  --repo    name on the instance (default: the folder's name)
EOF
    exit 2
}

TARGET=''; OWNER=''; REPO=''
while [ $# -gt 0 ]; do
    case "$1" in
        --owner) OWNER="${2:-}"; shift 2 ;;
        --repo)  REPO="${2:-}";  shift 2 ;;
        -h|--help) usage ;;
        -*) die "unknown option $1" ;;
        *)  [ -n "$TARGET" ] && die "one path at a time"; TARGET="$1"; shift ;;
    esac
done
[ -n "$TARGET" ] || usage

# Resolve the path before moving to the compose directory, since it was given
# relative to wherever the operator ran this from.
RESOLVED=$(cd "$TARGET" 2>/dev/null && pwd) || die "no such directory: $TARGET"
TARGET=$RESOLVED
cd "$(dirname "$0")"

[ -f .env ] || die ".env is missing — cp .env.sample .env and fill it in first"
set -a; . ./.env; set +a

: "${FORGEJO_ROOT_URL:?FORGEJO_ROOT_URL is unset in .env}"
F=${FORGEJO_ROOT_URL%/}                       # no trailing slash; carries its port
HOST=${F#*://}; HOST=${HOST%%:*}; HOST=${HOST%%/*}
API=http://$HOST:${API_PORT:-23080}
REVIEWER=${REVIEWER_LOGIN:-pingpong-reviewer}
CODER=${CODER_LOGIN:-pingpong-coder}

git -C "$TARGET" rev-parse --git-dir >/dev/null 2>&1 \
    || die "$TARGET is not a git repository"
git -C "$TARGET" rev-parse HEAD >/dev/null 2>&1 \
    || die "$TARGET has no commits — there would be nothing to push as main"

[ -n "$REPO" ] || REPO=$(basename "$TARGET")

fj_admin() { docker compose exec -T -u git forgejo forgejo admin "$@"; }

# ---------------------------------------------------------------------------
# Who owns it
# ---------------------------------------------------------------------------
step "owner"
if [ -z "$OWNER" ]; then
    EMAIL=$(git -C "$TARGET" config user.email || true)
    [ -n "$EMAIL" ] || die "$TARGET has no git user.email, so there is nothing to
match an account against — pass --owner <login>"
    # Forgejo links a commit to an account by the author's email, so the account
    # that should own this is the one carrying the address it commits with.
    OWNER=$(fj_admin user list 2>/dev/null \
            | awk -v e="$EMAIL" '$3 == e { print $2; exit }' | tr -d '\r')
    [ -n "$OWNER" ] || die "no Forgejo account has the address $TARGET commits with
($EMAIL). Create one — README step 2 — or pass --owner <login>. Do not onboard
under someone else's account: the PR would stop saying who wrote what."
    ok "$OWNER (matched on $EMAIL)"
else
    fj_admin user list 2>/dev/null | awk -v u="$OWNER" '$2 == u { found = 1 }
        END { exit !found }' || die "no Forgejo account named $OWNER"
    ok "$OWNER (given)"
fi

# ---------------------------------------------------------------------------
# The credential
# ---------------------------------------------------------------------------
NETRC=${NETRC:-$HOME/.netrc}

netrc_token() {
    # Walks the whitespace-separated token stream, so both the one-line and the
    # indented multi-line netrc layouts parse the same way.
    [ -f "$NETRC" ] || return 0
    awk -v host="$HOST" '
        { for (i = 1; i <= NF; i++) w[++n] = $i }
        END {
            for (i = 1; i <= n; i++)
                if (w[i] == "machine" && w[i+1] == host)
                    for (j = i + 2; j <= n; j++) {
                        if (w[j] == "machine") break
                        if (w[j] == "password") { print w[j+1]; exit }
                    }
        }' "$NETRC"
}

netrc_write() {
    # Drops any existing block for this host and appends a fresh one, keeping
    # every other machine's entry byte-for-byte.
    local token=$1 backup="$NETRC.pingpong-$(date +%Y%m%d%H%M%S)"
    if [ -f "$NETRC" ]; then
        cp "$NETRC" "$backup"
        awk -v host="$HOST" '
            { for (i = 1; i <= NF; i++) w[++n] = $i }
            END {
                for (i = 1; i <= n; ) {
                    if (w[i] != "machine") { i++; continue }
                    keep = (w[i+1] != host); line = ""
                    for (j = i; j <= n; j++) {
                        if (j > i && w[j] == "machine") break
                        line = line (line == "" ? "" : " ") w[j]
                    }
                    if (keep) print line
                    i = j
                }
            }' "$backup" > "$NETRC"
        printf 'old %s kept at %s\n' "$(basename "$NETRC")" "$backup"
    fi
    printf 'machine %s login %s password %s\n' "$HOST" "$OWNER" "$token" >> "$NETRC"
    chmod 600 "$NETRC" 2>/dev/null || true
}

mint_token() {
    # Scopes cannot be widened after the fact, so a token that turns out to be
    # too narrow is replaced rather than supplemented. The stale one stays valid
    # until its owner deletes it: revoking needs the account password, which
    # nothing on this host has.
    local name=workstation raw
    raw=$(fj_admin user generate-access-token --username "$OWNER" \
              --token-name "$name" --raw \
              --scopes write:repository,write:user,read:user 2>&1 | tr -d '\r') \
        || {
            name=workstation-$(date +%Y%m%d%H%M%S)
            raw=$(fj_admin user generate-access-token --username "$OWNER" \
                      --token-name "$name" --raw \
                      --scopes write:repository,write:user,read:user 2>&1 \
                  | tr -d '\r') \
                || die "could not mint a token for $OWNER:
$raw"
        }
    TOKEN=$(printf '%s' "$raw" | tail -n 1)
    [ -n "$TOKEN" ] || die "generate-access-token printed nothing"
    printf '   minted  token %s for %s\n' "$name" "$OWNER"
    printf '   NOTE    only %s can delete it, in Settings -> Applications\n' "$OWNER"
    netrc_write "$TOKEN"
}

step "credential"
TOKEN=$(netrc_token || true)
if [ -n "$TOKEN" ]; then
    have "token for $HOST in $(basename "$NETRC")"
else
    printf '   no entry for %s in %s\n' "$HOST" "$NETRC"
    mint_token
fi
AUTH="Authorization: token $TOKEN"

# Sets $STATUS and $BODY rather than printing, because `$(api ...)` would run in
# a subshell and the status would never come back out of it.
STATUS=''; BODY=''
api() {
    local method=$1 path=$2 payload=${3:-} out
    if [ -n "$payload" ]; then
        out=$(curl -s -m 30 -w '\n%{http_code}' -X "$method" "$F/api/v1$path" \
                  -H "$AUTH" -H 'Content-Type: application/json' -d "$payload") \
            || out=$'\n000'
    else
        out=$(curl -s -m 30 -w '\n%{http_code}' -X "$method" "$F/api/v1$path" \
                  -H "$AUTH") || out=$'\n000'
    fi
    STATUS=${out##*$'\n'}
    BODY=${out%$'\n'*}
}

api GET /user
case "$STATUS" in
    200) : ;;
    401|403) die "the token in $NETRC is not usable: $BODY
If the account has never logged in, Forgejo refuses its API calls until the
forced password change is done — README step 2." ;;
    *) die "$F is not answering ($STATUS). ./pingpong up, then try again." ;;
esac

# ---------------------------------------------------------------------------
# The repository
# ---------------------------------------------------------------------------
step "repository $OWNER/$REPO"
CREATE="{\"name\":\"$REPO\",\"auto_init\":false,\"default_branch\":\"main\"}"
api GET "/repos/$OWNER/$REPO"
if [ "$STATUS" = 200 ]; then
    have "exists on $F"
else
    # auto_init false on purpose: an initialised repository holds a commit of its
    # own, and pushing real history at it is then a non-fast-forward that reads
    # as a permissions problem.
    api POST /user/repos "$CREATE"
    if [ "$STATUS" = 403 ] && printf '%s' "$BODY" | grep -q 'write:user'; then
        printf '   403     %s\n' "$BODY"
        printf '   the token predates the scopes this call needs; replacing it\n'
        mint_token
        AUTH="Authorization: token $TOKEN"
        api POST /user/repos "$CREATE"
    fi
    case "$STATUS" in
        201) ok "created" ;;
        409) have "exists" ;;
        *)   die "could not create $OWNER/$REPO ($STATUS): $BODY" ;;
    esac
fi

step "collaborators"
for u in "$REVIEWER" "$CODER"; do
    api PUT "/repos/$OWNER/$REPO/collaborators/$u" '{"permission":"write"}'
    case "$STATUS" in
        # Without write the reviewer cannot post a review and the coder cannot
        # push, and the round then fails partway instead of at the start.
        204|201) ok "$u can write" ;;
        404|422) die "no account named $u — create the bots first, README step 1" ;;
        *)       die "could not add $u ($STATUS): $BODY" ;;
    esac
done

# ---------------------------------------------------------------------------
# The folder
# ---------------------------------------------------------------------------
step "remote and main"
REMOTE_URL="$F/$OWNER/$REPO.git"        # no credential in the URL, ever
if git -C "$TARGET" remote get-url forgejo >/dev/null 2>&1; then
    git -C "$TARGET" remote set-url forgejo "$REMOTE_URL"
    have "remote forgejo -> $REMOTE_URL"
else
    git -C "$TARGET" remote add forgejo "$REMOTE_URL"
    ok "remote forgejo -> $REMOTE_URL"
fi

# main has to exist before any PR can be opened against it.
if git -C "$TARGET" ls-remote --exit-code --heads forgejo main >/dev/null 2>&1; then
    have "main is on the instance"
else
    git -C "$TARGET" push -q forgejo HEAD:refs/heads/main \
        || die "push failed. The credential comes from $NETRC and nowhere else —
check its entry for $HOST names $OWNER."
    ok "pushed HEAD to main"
fi

# On a different port from Forgejo, so it cannot be derived from the remote; and
# it stays in git config rather than in the repository, which is a rule the
# reviewed repo's AGENTS.md states and expects to hold.
git -C "$TARGET" config pingpong.api "$API"
ok "pingpong.api = $API"

step "AGENTS.md"
if [ -e "$TARGET/AGENTS.md" ]; then
    have "present — left alone"
else
    cp templates/AGENTS.md "$TARGET/AGENTS.md"
    ok "copied from templates/"
    printf '   TODO    edit the four places it marks "decide this per repo"\n'
fi

# ---------------------------------------------------------------------------
# The webhook
# ---------------------------------------------------------------------------
step "webhook"
api GET "/repos/$OWNER/$REPO/hooks"
if printf '%s' "$BODY" | grep -q "api:8080/webhook"; then
    have "delivering to $HOOK_TARGET"
else
    # An empty secret is the worst outcome available: Forgejo answers 201, the
    # hook looks right in the UI, and every delivery then fails its signature
    # check — indistinguishable from a webhook that never arrived.
    [ -n "${PINGPONG_WEBHOOK_SECRET:-}" ] \
        || die "PINGPONG_WEBHOOK_SECRET is empty in .env. Set it to any long
random string and run ./pingpong up before onboarding, or this hook would be
created with no secret and silently never fire."
    # Forgejo stores these expanded — pull_request_review becomes its _approved,
    # _rejected and _comment variants — which is what actually arrives.
    api POST "/repos/$OWNER/$REPO/hooks" \
        "{\"type\":\"forgejo\",\"active\":true,
          \"events\":[\"pull_request\",\"pull_request_review\",\"issue_comment\"],
          \"config\":{\"url\":\"$HOOK_TARGET\",\"content_type\":\"json\",
                      \"secret\":\"$PINGPONG_WEBHOOK_SECRET\"}}"
    case "$STATUS" in
        201) ok "created, delivering to $HOOK_TARGET" ;;
        *)   die "could not create the webhook ($STATUS): $BODY" ;;
    esac
fi

cat <<EOF

$OWNER/$REPO is on the instance.

  $F/$OWNER/$REPO

Next:  ./pingpong doctor
       then open a PR from $TARGET — AGENTS.md there says how.
EOF

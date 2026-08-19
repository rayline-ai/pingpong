#!/usr/bin/env bash
# `pingpong accounts` — create the accounts the instance runs on, and fill the
# `.env` values that can only exist once Forgejo has booted.
#
#   pingpong accounts                    the setup pass: admin, the two bots, you
#   pingpong accounts --user <email>      add one person afterwards
#
# Host-side for the same reason as onboard: this is `docker compose exec forgejo`
# and it writes the operator's `.env`, neither of which the API container can do.
#
# Idempotent. Every account is looked up before it is created and every `.env`
# value is written only if it is empty, so a run interrupted halfway is repeated
# rather than unpicked.
#
# What it deliberately does NOT do is mint a token for a person. Tokens cannot be
# revoked from here — Forgejo wants the account's own password for that — so one
# is minted only at the point where something will store it: `onboard` for this
# machine's netrc, or `--token` below for a person to carry to theirs.
set -euo pipefail

die()  { printf 'accounts: %s\n' "$*" >&2; exit 1; }
step() { printf '\n== %s\n' "$*"; }
ok()   { printf '   ok      %s\n' "$*"; }
have() { printf '   already %s\n' "$*"; }
warn() { printf '   note    %s\n' "$*"; }
tell() { printf '   >>      %s\n' "$*"; }

usage() {
    cat >&2 <<'EOF'
usage: pingpong accounts [--user <email>] [--login <name>] [--token]

  (no arguments)   create pingpong-admin, the two bot accounts with their
                   tokens, and an account for whoever this checkout commits as
  --user <email>   create one account for that address instead, for someone
                   joining after setup
  --login <name>   login for --user (default: the local part of the address)
  --token          also mint that person a token and print it, for someone on
                   another machine. Without this no token is minted: `onboard`
                   mints one for whoever works on THIS machine, into ~/.netrc.
EOF
    exit 2
}

USER_EMAIL=''; USER_LOGIN=''; WANT_TOKEN=no
while [ $# -gt 0 ]; do
    case "$1" in
        --user)  USER_EMAIL="${2:-}"; shift 2 ;;
        --login) USER_LOGIN="${2:-}"; shift 2 ;;
        --token) WANT_TOKEN=yes; shift ;;
        -h|--help) usage ;;
        *) die "unknown argument $1" ;;
    esac
done
if [ -z "$USER_EMAIL" ] && { [ -n "$USER_LOGIN" ] || [ $WANT_TOKEN = yes ]; }; then
    die "--login and --token only mean something with --user"
fi

cd "$(dirname "$0")"

[ -f .env ] || die ".env is missing — cp .env.sample .env and fill it in first"
set -a; . ./.env; set +a

REVIEWER=${REVIEWER_LOGIN:-pingpong-reviewer}
CODER=${CODER_LOGIN:-pingpong-coder}
ADMIN=pingpong-admin

fj_admin() { docker compose exec -T -u git forgejo forgejo admin "$@"; }

docker compose ps --status running --services 2>/dev/null | grep -qx forgejo \
    || die "Forgejo is not running, and accounts only exist once it has booted.
./pingpong up, then run this again."

# ---------------------------------------------------------------------------
# .env
# ---------------------------------------------------------------------------
# Rewrites one assignment in place, keeping every other line — comments included
# — byte-for-byte, because this file is the operator's and most of it is prose
# they may have edited. A missing key is appended rather than silently dropped.
ENV_BACKUP=.env.accounts-$(date +%Y%m%d%H%M%S)
env_set() {
    local key=$1 value=$2 tmp=.env.accounts-partial
    # One backup per run, not per value: three writes would otherwise leave three
    # copies of a file that holds tokens.
    if [ ! -f "$ENV_BACKUP" ]; then
        cp .env "$ENV_BACKUP"
        printf '   original .env kept at %s\n' "$ENV_BACKUP"
    fi
    # awk reads a copy of the file as it stands now, not the backup: reading the
    # backup each time would undo whatever the previous call wrote.
    cp .env "$tmp"
    awk -v key="$key" -v value="$value" '
        $0 ~ "^" key "=" && !done { print key "=" value; done = 1; next }
        { print }
        END { if (!done) print key "=" value }
    ' "$tmp" > .env
    rm -f "$tmp"
    chmod 600 .env 2>/dev/null || true
}

# Reads the file rather than the environment: a variable exported by a previous
# `. ./.env` in this same shell would make an empty slot look filled.
env_value() {
    [ -f .env ] || return 0
    awk -v key="$1" '$0 ~ "^" key "=" { sub("^" key "=", ""); print; exit }' .env
}

account_exists() {
    fj_admin user list 2>/dev/null | awk -v u="$1" '$2 == u { found = 1 }
        END { exit !found }'
}

# `user create` prints a generated password and only THEN fails on a name that is
# taken, so the existence check has to come first — otherwise a re-run hands you
# a password for an account it did not create.
create_account() {
    local login=$1 email=$2 kind=$3 out
    PASSWORD=''            # never let a previous account's password be reported
    if account_exists "$login"; then
        have "$login exists"
        return 1
    fi
    local args=(user create --username "$login" --email "$email" --random-password)
    case $kind in
        admin) args+=(--admin) ;;
        # Forgejo enforces the first-login password change on the API too, and
        # nothing ever logs in as a bot, so without this every call its token
        # makes comes back 403 "You must change your password".
        bot)   args+=(--must-change-password=false) ;;
        # A person keeps the forced change: that is what makes the printed
        # password a one-shot rather than a password chosen on their behalf.
        person) ;;
    esac
    out=$(fj_admin "${args[@]}" 2>&1 | tr -d '\r') \
        || die "could not create $login:
$out"
    ok "$login created"
    PASSWORD=$(printf '%s' "$out" | sed -n "s/.*generated random password is '\(.*\)'.*/\1/p")
    return 0
}

# Printed once and stored only as a hash, so nothing can show it again.
announce_password() {
    local who=$1
    [ -n "${PASSWORD:-}" ] || { warn "no password in Forgejo's output — reset it with
   forgejo admin user change-password --username $who"; return; }
    tell "$who password: $PASSWORD"
    tell "written down nowhere else. Forgejo stores only a hash."
}

mint_bot_token() {
    local login=$1 var=$2 raw
    if [ -n "$(env_value "$var")" ]; then
        have "$var is set — left alone"
        # Minting a second one would leave the first live forever: revoking needs
        # the account's own password, which nothing here has.
        return 0
    fi
    raw=$(fj_admin user generate-access-token --username "$login" \
              --token-name pingpong --raw \
              --scopes write:repository,write:issue 2>&1 | tr -d '\r') \
        || die "could not mint a token for $login:
$raw"
    local token
    token=$(printf '%s' "$raw" | tail -n 1)
    [ -n "$token" ] || die "generate-access-token printed nothing for $login"
    env_set "$var" "$token"
    ok "$var written to .env"
}

# ---------------------------------------------------------------------------
# One person, after setup
# ---------------------------------------------------------------------------
if [ -n "$USER_EMAIL" ]; then
    case $USER_EMAIL in *@*) ;; *) die "--user wants an email address, not $USER_EMAIL.
Forgejo links a commit to an account by the author's email, so this has to be the
address that person's machine commits with — ask them for \`git config user.email\`." ;;
    esac
    [ -n "$USER_LOGIN" ] || USER_LOGIN=${USER_EMAIL%%@*}

    step "$USER_LOGIN"
    if create_account "$USER_LOGIN" "$USER_EMAIL" person; then
        announce_password "$USER_LOGIN"
        tell "they sign in at ${FORGEJO_ROOT_URL:-the instance} and Forgejo makes"
        tell "them choose a new one. Their token only works after that."
    fi

    if [ $WANT_TOKEN = yes ]; then
        step "token for $USER_LOGIN"
        raw=$(fj_admin user generate-access-token --username "$USER_LOGIN" \
                  --token-name workstation --raw \
                  --scopes write:repository,write:user,read:user 2>&1 | tr -d '\r') \
            || die "could not mint a token for $USER_LOGIN. If the name
\`workstation\` is taken they already have one, and a second cannot be revoked
from here — have them look under Settings → Applications:
$raw"
        TOKEN=$(printf '%s' "$raw" | tail -n 1)
        tell "token: $TOKEN"
        tell "goes in THEIR ~/.netrc, host only and no port:"
        tell "machine <host> login $USER_LOGIN password <that token>"
        warn "mint it after their first login, or Forgejo answers 403 to every call."
    else
        tell "no token minted. On this machine \`onboard\` mints one into ~/.netrc;"
        tell "for another machine, re-run with --token and hand it over."
    fi
    printf '\n%s exists. Onboard a folder they commit in and the PR will name them.\n' "$USER_LOGIN"
    exit 0
fi

# ---------------------------------------------------------------------------
# The setup pass
# ---------------------------------------------------------------------------
# An ops account: it creates the others and administers the instance, and owns no
# repository and authors nothing, so no repository's fate is tied to it. Nothing
# uses an admin *token* — this account is for the CLI and the web UI.
step "$ADMIN"
if create_account "$ADMIN" "$ADMIN@local" admin; then
    announce_password "$ADMIN"
    tell "log in with it at ${FORGEJO_ROOT_URL:-the instance} and change it."
fi

# One account per role, because Forgejo credits an "added N commits" event to
# whoever pushed rather than to the commit's author: share one token and the
# reviewer appears to have written the fixes.
step "$REVIEWER"
create_account "$REVIEWER" "$REVIEWER@local" bot || true
mint_bot_token "$REVIEWER" FORGEJO_REVIEWER_TOKEN

step "$CODER"
# BOT_EMAIL is what links the coder's commits to this account, and rounds are
# counted off it. The reviewer must not share it or its own commits would count.
CODER_EMAIL=${BOT_EMAIL:-$CODER@local}
create_account "$CODER" "$CODER_EMAIL" bot || true
mint_bot_token "$CODER" FORGEJO_CODER_TOKEN
if [ "$CODER_EMAIL" != "${BOT_EMAIL:-}" ]; then
    warn "BOT_EMAIL is unset in .env; this account carries $CODER_EMAIL."
    warn "Set BOT_EMAIL to that address or rounds will not be counted."
fi
# Checked even when the account already existed, because this is the one address
# in the system that is load-bearing: rounds are counted by matching commit
# authors against BOT_EMAIL, and a mismatch means the counter never advances and
# MAX_ROUNDS never bites.
ACTUAL=$(fj_admin user list 2>/dev/null | awk -v u="$CODER" '$2 == u { print $3; exit }' | tr -d '\r')
if [ -n "$ACTUAL" ] && [ "$ACTUAL" != "$CODER_EMAIL" ]; then
    warn "$CODER carries $ACTUAL but BOT_EMAIL says $CODER_EMAIL."
    warn "Rounds are counted off BOT_EMAIL, so they would never be counted."
    warn "Change one to match: BOT_EMAIL in .env, or the account's Settings → Emails."
fi

step "webhook secret"
if [ -n "$(env_value PINGPONG_WEBHOOK_SECRET)" ]; then
    have "PINGPONG_WEBHOOK_SECRET is set — left alone"
else
    # 32 bytes of hex. Any long random string does; what matters is that it is
    # never empty, since Forgejo accepts an empty one and then every delivery
    # fails its signature check.
    SECRET=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
    [ ${#SECRET} -eq 64 ] || die "could not generate a random secret"
    env_set PINGPONG_WEBHOOK_SECRET "$SECRET"
    ok "PINGPONG_WEBHOOK_SECRET written to .env"
fi

# The person whose workstation this is. Forgejo links a commit to an account by
# the author's email, so the account that should exist is the one carrying the
# address this checkout commits with.
step "you"
EMAIL=$(git config user.email || true)
if [ -z "$EMAIL" ]; then
    warn "no git user.email here, so there is no address to create an account for."
    warn "Set one, or add people with: ./pingpong accounts --user <email>"
else
    LOGIN=${EMAIL%%@*}
    if create_account "$LOGIN" "$EMAIL" person; then
        announce_password "$LOGIN"
        tell "sign in with it at ${FORGEJO_ROOT_URL:-the instance} and change it;"
        tell "a token only works after that. \`onboard\` mints yours."
    fi
fi

cat <<EOF

The instance has its accounts.

Next:  ./pingpong up          reload .env, so the engine picks up the tokens
       ./pingpong doctor
       ./pingpong onboard ../some-repo

Left to a human: the first login for each account above, which is what clears
Forgejo's forced password change. Nothing can do that for them.
EOF

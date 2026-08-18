#!/usr/bin/env bash
# Live progress for an in-flight round. Not part of PingPong — a test aid.
#
# The API only logs at round boundaries ("reviewing", "fixing", "verdict"), so a
# long fix looks identical to a hung one. The agent's Rayline log is the finer
# signal: one `POST /v1/messages` per model turn. Turn count climbing = alive.
set -uo pipefail
cd "$(dirname "$0")"

strip() { sed 's/\x1b\[[0-9;]*m//g'; }

once="${1:-}"

while true; do
    printf '\n===== %s =====\n' "$(date -u +%H:%M:%S)"

    echo "--- api (round boundaries) ---"
    docker compose logs api --no-log-prefix --tail 6 2>/dev/null | grep -v '^http:'

    for role in reviewer coder; do
        turns=$(docker compose exec -T "$role" sh -c \
            'grep -c "POST /v1/messages" /root/.rayline/rld/rl-rld.log 2>/dev/null' 2>/dev/null | tr -d '\r')
        last=$(docker compose exec -T "$role" sh -c \
            'grep "POST /v1/messages" /root/.rayline/rld/rl-rld.log 2>/dev/null | tail -1' 2>/dev/null \
            | strip | grep -o '^[0-9T:-]*' )
        printf -- '--- %-8s turns=%-5s last=%s\n' "$role" "${turns:-0}" "${last:-none}"
    done

    echo "--- ollama ---"
    ollama ps 2>/dev/null | tail -n +2

    [ "$once" = "once" ] && break
    sleep 20
done

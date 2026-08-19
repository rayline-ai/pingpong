#!/usr/bin/env bash
# How the engine invokes Hermes. One line of indirection, and it exists to hold
# one difference between the modes.
#
# agent.Dockerfile bakes ANTHROPIC_API_KEY and ANTHROPIC_BASE_URL as *image* ENV
# so they reach `docker exec`, which does not inherit the entrypoint's shell.
# That is exactly right for router mode and exactly wrong for a subscription:
#
#   ANTHROPIC_API_KEY   sits at priority 3 in Hermes' resolver, above the
#                       ~/.claude credential file at 4 — so the placeholder wins
#                       and the subscription is never read.
#   ANTHROPIC_BASE_URL  would send the OAuth bearer to the local injector rather
#                       than to Anthropic.
#
# The entrypoint cannot unset them for a later `docker exec`, and neither can
# compose. Here is the only place that can, so the mode is read here rather than
# in the engine: `src/agents.py` calls this unconditionally and knows nothing
# about modes. AGENT_MODE is compose environment, which `docker exec` does
# inherit (unlike the entrypoint's own shell).
case "${AGENT_MODE:-router}" in
    claude-sub|codex-sub)
        exec env -u ANTHROPIC_API_KEY -u ANTHROPIC_BASE_URL hermes "$@" ;;
    *)
        exec hermes "$@" ;;
esac

# One image, two roles. The role is HERMES_MODEL_ALIAS at runtime — nothing about
# reviewer-vs-coder is baked in here.
#
# trixie, not bookworm: the Rayline binaries need GLIBC_2.38/2.39 and bookworm
# ships 2.36, so `rayline` and `rld` will not start there at all.
FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive

# The Hermes installer downloads and runs a Node tarball even under
# --skip-browser, so the image needs what that Node needs:
#   xz-utils  — debian-slim ships tar without xz, so the .tar.xz will not unpack
#   libatomic1 — node links libatomic.so.1; without it node exits 127 and the
#                installer dies immediately after extraction
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git jq procps xz-utils libatomic1 \
    && rm -rf /var/lib/apt/lists/*

# Hermes. --skip-setup/--non-interactive because the config is written by the
# entrypoint; --skip-browser because a headless agent has no use for one.
#
# --skip-browser only skips Playwright/Chromium — the installer still runs
# `npm install`, and that hard-fails the whole install if it errors. One of the
# deps (node-pty) is a native module, so a C++ toolchain has to be present at
# install time. It is purged in the same layer: node-pty is already compiled by
# then, and keeping gcc in an image that runs model-authored code is not free.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential python3 \
    && curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh \
        -o /tmp/hermes-install.sh \
    && bash /tmp/hermes-install.sh --skip-browser --skip-setup --non-interactive \
    && rm -f /tmp/hermes-install.sh \
    && apt-get purge -y build-essential \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* /root/.npm /root/.cache

# Rayline provides `rayline` + `rld`, started by the entrypoint against the
# mounted config. get.rayline.ai is the official channel — the GitHub repo's
# releases are a stale mirror whose newest tag predates `router start`.
#
# Pinned rather than `latest`: `router start --config` is the entry point this
# whole design rests on, so the version that provides it is not something to
# pick up implicitly. The build fails here if the pin ever loses it.
ARG RAYLINE_VERSION=0.2.6+7bd2849c99d2
RUN curl -fsSL https://get.rayline.ai/install.sh | bash -s -- "${RAYLINE_VERSION}" \
    && /root/.rayline/bin/rayline --version \
    && /root/.rayline/bin/rayline router --help >/dev/null

ENV PATH="/root/.local/bin:/root/.rayline/bin:${PATH}"

# Hermes refuses to start without an Anthropic credential, but the injector is
# what actually authenticates — it strips this and substitutes the rlk- key. So
# this is a placeholder Hermes only needs in order to have something to send as
# x-api-key, and the real credential never enters the agent's environment. Set
# as image ENV, not compose env, so it survives `docker exec`.
ENV ANTHROPIC_API_KEY="sk-ant-rayline-injector-placeholder"

# `model.base_url` in config.yaml is NOT enough on the `hermes -z` path: with it
# set and pointing at a dead port the error was byte-identical to leaving it out,
# i.e. the one-shot path ignores it and calls api.anthropic.com directly, which
# then 401s on the placeholder above. The env var is what that path honours
# (hermes_cli/model_switch.py: "Base URL precedence: LM_BASE_URL env var >
# active config's base_url"), and it is what `rld` itself tells clients to set.
# ENV, not compose env, for the same reason as the key: `docker exec` inherits it.
ENV ANTHROPIC_BASE_URL="http://127.0.0.1:20809"

# Both ENV lines above are right for router mode and wrong for a subscription,
# where the key outranks the credential file in Hermes' own resolver and the base
# URL points the OAuth bearer at the injector. Nothing that runs inside the
# container can unset them for a later `docker exec` — so the engine calls
# `hermes-run` rather than `hermes`, and that script is the one place the
# difference lives.
COPY docker/agent-entrypoint.sh /usr/local/bin/agent-entrypoint.sh
COPY docker/hermes-run.sh /usr/local/bin/hermes-run
RUN chmod +x /usr/local/bin/agent-entrypoint.sh /usr/local/bin/hermes-run

WORKDIR /work
ENTRYPOINT ["/usr/local/bin/agent-entrypoint.sh"]

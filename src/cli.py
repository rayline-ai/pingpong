"""pingpong CLI — run the webhook listener, or drive one round by hand."""
import argparse
import re
import sys

from . import agents, config, forgejo, gitops, loop, models, subscription, webhook

PR_RE = re.compile(r"^([^/\s]+)/([^#\s]+)#(\d+)$")

# Where docker-compose.yml mounts rayline/pingpong.json in this container. The
# same path the agents see, and read-only in both.
ROUTING_CONFIG = "/etc/pingpong/rayline.json"


def log(msg):
    print(msg, flush=True)


def _force_utf8():
    """Windows consoles default to cp1252, which mangles any non-Latin-1
    character an agent emits in a review."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _parse_pr(text):
    match = PR_RE.match(text.strip())
    if not match:
        sys.exit("expected owner/repo#123, got %r" % text)
    return match.group(1), match.group(2), int(match.group(3))


def cmd_serve(args, cfg):
    return webhook.main()


def cmd_round(args, cfg):
    problems = cfg.validate()
    if problems:
        sys.exit("Configuration problems:\n" + "\n".join("  - " + p for p in problems))

    owner, repo, index = _parse_pr(args.pr)
    fj = forgejo.Forgejo(cfg.forgejo_url, cfg.reviewer_token)
    result = loop.run_round(cfg, fj, owner, repo, index, log=log)

    log("")
    log("=" * 60)
    for key in ("action", "reason", "round", "sha", "pushed"):
        if key in result:
            log("%-9s %s" % (key + ":", result[key]))
    log("=" * 60)

    # Non-zero unless the PR ended approved, so this can gate a script.
    return 0 if result.get("action") == "approved" else 1


def _brain(role):
    """What a role's alias resolves to, read from the mounted routing config.
    Best-effort: it is a report, and a config this cannot parse is Rayline's
    business, not something to fail `doctor` over."""
    # In a subscription mode there is no router and the mounted config is not
    # consulted by anything. Reading it anyway would report a model this agent is
    # demonstrably not using — the worst possible answer to this question. Per
    # role, because the other role may well be on the router.
    mode = subscription.mode(role)
    if subscription.is_subscription(mode):
        model = subscription.model(role)
        return "%s / %s (subscription, no router)" % (
            mode, model or "%s_MODEL is empty" % role.upper())
    try:
        cfg = models.load(ROUTING_CONFIG)
        endpoint_id, model = models.route(cfg, role)
        if not endpoint_id:
            return "not chosen — run `./pingpong model` on the host"
        return "%s / %s" % (endpoint_id, model)
    except (models.ModelError, KeyError):
        return "unknown (%s not readable here)" % ROUTING_CONFIG


def cmd_doctor(args, cfg):
    ok = True
    log("forgejo:   %s" % cfg.forgejo_url)
    log("reviewer:  %s (alias reviewer-brain)" % cfg.reviewer_container)
    log("  mode:    %s" % subscription.mode("reviewer"))
    log("  brain:   %s" % _brain("reviewer"))
    log("coder:     %s (alias coder-brain)" % cfg.coder_container)
    log("  mode:    %s" % subscription.mode("coder"))
    log("  brain:   %s" % _brain("coder"))
    log("rounds:    %d" % cfg.max_rounds)
    log("work root: %s" % cfg.work_root)

    for problem in cfg.validate():
        log("  ! " + problem)
        ok = False

    for label, container in (("reviewer", cfg.reviewer_container),
                             ("coder", cfg.coder_container)):
        if agents.container_running(container):
            log("  %s container running" % label)
        else:
            log("  ! %s container %r is not running" % (label, container))
            ok = False
            continue
        # The one credential this project cannot check from the host: it is made
        # by `pingpong login` and lives in that agent's own volume. Asked per
        # role, since only the roles on codex-sub have one to check.
        if subscription.mode(label) == "codex-sub":
            if agents.codex_signed_in(container):
                log("  %s signed in to ChatGPT" % label)
            else:
                log("  ! %s has no ChatGPT session — run ./pingpong login" % label)
                ok = False

    if cfg.reviewer_token:
        try:
            forgejo.Forgejo(cfg.forgejo_url, cfg.reviewer_token)._request("GET", "/version")
            log("  forgejo reachable")
        except forgejo.ForgejoError as exc:
            log("  ! forgejo unreachable: %s" % exc)
            ok = False

    log("")
    log("OK" if ok else "Not ready — fix the items above.")
    return 0 if ok else 1


def main(argv=None):
    _force_utf8()
    config.load_env()
    cfg = config.Config()

    parser = argparse.ArgumentParser(
        prog="pingpong",
        description="Review/fix ping-pong over Forgejo pull requests. Two Hermes "
                    "agents on different Rayline-routed models; state lives on "
                    "the PR.")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the webhook listener (default in compose)")
    serve.set_defaults(func=cmd_serve)

    one = sub.add_parser("round", help="run a single round against one PR, by hand")
    one.add_argument("pr", help="pull request, as owner/repo#123")
    one.set_defaults(func=cmd_round)

    doctor = sub.add_parser("doctor", help="check config, containers and Forgejo")
    doctor.set_defaults(func=cmd_doctor)

    args = parser.parse_args(argv)
    try:
        return args.func(args, cfg)
    except (gitops.GitError, agents.AgentError, forgejo.ForgejoError) as exc:
        sys.exit("error: %s" % exc)
    except KeyboardInterrupt:
        sys.exit("interrupted")


if __name__ == "__main__":
    sys.exit(main())

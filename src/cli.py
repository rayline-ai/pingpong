"""pingpong CLI — run the webhook listener, or drive one round by hand."""
import argparse
import re
import sys

from . import agents, config, forgejo, gitops, loop, webhook

PR_RE = re.compile(r"^([^/\s]+)/([^#\s]+)#(\d+)$")


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


def cmd_doctor(args, cfg):
    ok = True
    log("forgejo:   %s" % cfg.forgejo_url)
    log("reviewer:  %s (alias reviewer-brain)" % cfg.reviewer_container)
    log("coder:     %s (alias coder-brain)" % cfg.coder_container)
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

"""Webhook listener — the loop's trigger.

Forgejo fires, a round runs, the coder pushes, Forgejo fires again. The cycle
terminates because rounds are counted from the coder's own commits and capped;
see loop.run_round.

stdlib http.server, single small surface, no framework.
"""
import hashlib
import hmac
import json
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import agents, config, forgejo, loop

# Rounds for one PR are serialized. Two rounds racing on the same clone would
# fight over the working tree and could push conflicting commits.
_PR_LOCKS = {}
_PR_LOCKS_GUARD = threading.Lock()

TRIGGER_ACTIONS = {"opened", "reopened", "synchronize", "synchronized"}

# A human asking for another run. Any of the three bot names works, because
# Forgejo's mention autocomplete offers whichever ones are collaborators and
# nobody should have to remember which. Matched as literal text — Forgejo has no
# command syntax of its own, and the comment is never shown to a model.
COMMAND_MENTION = "@pingpong"
COMMAND_REASON = "comment command"


def _pr_lock(key):
    with _PR_LOCKS_GUARD:
        return _PR_LOCKS.setdefault(key, threading.Lock())


def log(msg):
    print(msg, flush=True)


def verify(secret, signature, body):
    if not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


def should_run(event, payload, bot_name, bot_logins=()):
    """Decide whether this delivery is a round trigger.

    Returns (run, reason).
    """
    action = (payload.get("action") or "").lower()
    sender = ((payload.get("sender") or {}).get("login") or "")

    if event == "pull_request":
        if action not in TRIGGER_ACTIONS:
            return False, "pull_request action %r is not a trigger" % action
        return True, "pull_request %s" % action

    if event == "issue_comment":
        if action != "created":
            return False, "issue_comment action %r is not a trigger" % action
        # `is None` rather than falsy: Forgejo sends null for a plain issue, but
        # an empty object is still a pull request.
        if (payload.get("issue") or {}).get("pull_request") is None:
            return False, "comment is on an issue, not a pull request"
        body = ((payload.get("comment") or {}).get("body") or "")
        # The marker the reset itself posts contains no mention, but a bot could
        # still quote one — checking the sender keeps that from looping.
        if sender == bot_name or sender in bot_logins:
            return False, "comment by the bot itself"
        if COMMAND_MENTION not in body.lower():
            return False, "comment is not a command"
        return True, COMMAND_REASON

    if event == "pull_request_review":
        # A human asking for changes should drive a round the same way the agent
        # reviewer does. The bot's own review must not, or every round would
        # trigger the next one twice.
        if sender == bot_name:
            return False, "review by the bot itself"
        state = ((payload.get("review") or {}).get("type")
                 or (payload.get("review") or {}).get("state") or "").lower()
        if state.replace("_", "") in ("requestchanges", "rejected"):
            return True, "human requested changes"
        return False, "review state %r is not a trigger" % state

    return False, "event %r is not handled" % event


def handle(cfg, payload, reason):
    repository = payload.get("repository") or {}
    owner = ((repository.get("owner") or {}).get("login")
             or (repository.get("owner") or {}).get("username") or "")
    name = repository.get("name") or ""
    number = ((payload.get("pull_request") or {}).get("number")
              or (payload.get("issue") or {}).get("number")
              or payload.get("number"))

    if not (owner and name and number):
        log("ignoring delivery: could not identify the pull request")
        return

    key = "%s/%s#%d" % (owner, name, int(number))
    lock = _pr_lock(key)
    if not lock.acquire(blocking=False):
        # Another round for this PR is already running; the push it makes will
        # fire a fresh webhook, so dropping this one loses nothing.
        log("%s: a round is already running, skipping" % key)
        return

    try:
        log("%s: %s" % (key, reason))
        fj = forgejo.Forgejo(cfg.forgejo_url, cfg.reviewer_token)
        result = loop.run_round(cfg, fj, owner, name, int(number), log=log,
                                reset=reason == COMMAND_REASON)
        log("%s: %s" % (key, json.dumps({k: v for k, v in result.items()
                                         if k in ("action", "reason", "round",
                                                  "sha", "pushed")})))
    except (loop.gitops.GitError, agents.AgentError, forgejo.ForgejoError) as exc:
        log("%s: failed: %s" % (key, exc))
    except Exception:
        log("%s: unexpected failure:\n%s" % (key, traceback.format_exc()))
    finally:
        lock.release()


class Handler(BaseHTTPRequestHandler):
    cfg = None
    server_version = "PingPongAPI/1.0"

    def _reply(self, code, message):
        body = json.dumps({"status": message}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") in ("/healthz", ""):
            self._reply(200, "ok")
        else:
            self._reply(404, "not found")

    def do_POST(self):
        if self.path.rstrip("/") not in ("/webhook", ""):
            self._reply(404, "not found")
            return

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        signature = (self.headers.get("X-Forgejo-Signature")
                     or self.headers.get("X-Gitea-Signature")
                     or self.headers.get("X-Hub-Signature-256", "").replace("sha256=", ""))
        if not verify(self.cfg.webhook_secret, signature, body):
            log("rejected delivery: bad signature")
            self._reply(401, "bad signature")
            return

        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._reply(400, "bad payload")
            return

        event = (self.headers.get("X-Forgejo-Event")
                 or self.headers.get("X-Gitea-Event")
                 or self.headers.get("X-GitHub-Event") or "").lower()

        run, reason = should_run(event, payload, self.cfg.bot_name,
                                 (self.cfg.reviewer_login, self.cfg.coder_login))
        if not run:
            self._reply(202, "ignored: %s" % reason)
            return

        # Answer immediately: a round takes minutes and Forgejo's delivery would
        # time out and be retried, starting the same round twice.
        threading.Thread(target=handle, args=(self.cfg, payload, reason),
                         daemon=True).start()
        self._reply(202, "accepted: %s" % reason)

    def log_message(self, fmt, *args):
        log("http: " + fmt % args)


def main():
    config.load_env()
    cfg = config.Config()

    problems = cfg.validate()
    if problems:
        sys.exit("Configuration problems:\n"
                 + "\n".join("  - " + problem for problem in problems))

    for container in (cfg.reviewer_container, cfg.coder_container):
        if not agents.container_running(container):
            log("warning: agent container %r is not running yet" % container)

    port = int(os.environ.get("API_LISTEN_PORT", "8080"))
    Handler.cfg = cfg
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log("PingPongAPI listening on :%d  (forgejo: %s)" % (port, cfg.forgejo_url))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())

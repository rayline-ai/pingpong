"""Configuration.

Deliberately small. PingPongAPI no longer knows anything about models, endpoints
or credentials — Rayline owns all three, behind rayline/pingpong.json, and the
API never parses that file. What is left here is Forgejo, the loop's limits, and
which container holds which role.
"""
import os


def _int(name, default):
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def load_env(path=None):
    """Load KEY=VALUE lines from a .env file, without overriding the real env.

    Compose already injects these in the container; this is for running the CLI
    directly on a host.
    """
    path = path or os.path.join(root(), ".env")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Config:
    def __init__(self):
        self.root = root()

        # Forgejo — the UI, the round-state store, and the merge gate.
        self.forgejo_url = os.environ.get("FORGEJO_URL", "http://forgejo:3000").rstrip("/")
        self.reviewer_token = os.environ.get("FORGEJO_REVIEWER_TOKEN", "").strip()
        self.webhook_secret = os.environ.get("PINGPONG_WEBHOOK_SECRET", "").strip()

        # Agents. One container per role; the brain behind each is a
        # routes.model_routes alias resolved inside that container's router.
        self.reviewer_container = os.environ.get("REVIEWER_CONTAINER", "pingpong-reviewer")
        self.coder_container = os.environ.get("CODER_CONTAINER", "pingpong-coder")

        # Loop limits.
        self.max_rounds = _int("MAX_ROUNDS", 3)
        self.max_diff_bytes = _int("MAX_DIFF_BYTES", 60000)
        self.review_timeout = _int("REVIEW_TIMEOUT", 900)
        # 30 minutes. A coder on a local model is the case that sets this: a 9B
        # sharing a GPU took ~10 minutes on a one-file diff, so the old 20 was
        # not the comfortable margin it looks like. The round holds a thread for
        # the whole of it, and the commit status now says so on the PR.
        self.fix_timeout = _int("FIX_TIMEOUT", 1800)

        self.work_root = os.environ.get("WORK_ROOT", "/work")
        self.prompts_dir = os.environ.get("PROMPTS_DIR", os.path.join(self.root, "prompts"))

        self.bot_name = os.environ.get("BOT_NAME", "pingpong-coder")
        self.bot_email = os.environ.get("BOT_EMAIL", "pingpong-coder@local")

        # Forgejo logins of the two bot accounts. Only used to ignore the bots'
        # own comments: a `@pingpong` mention the bot writes must not trigger a
        # round, or the loop would feed itself. Default to the container names,
        # which SETUP.md's account names make match.
        self.reviewer_login = os.environ.get("REVIEWER_LOGIN", self.reviewer_container)
        self.coder_login = os.environ.get("CODER_LOGIN", self.coder_container)

        # The coder's own Forgejo token, used only to push. Forgejo credits the
        # "added N commits" event to the pusher rather than the commit author, so
        # sharing one token makes the reviewer appear to have written the fix.
        # Falls back to it so a single-account setup still works.
        self.coder_token = os.environ.get("FORGEJO_CODER_TOKEN") or self.reviewer_token

    def public(self):
        """The loop's limits and identities, for anyone working against a repo.

        Instructions kept in a consuming repository would otherwise restate
        these, and restating them is how they go stale: every one is a per-
        instance `.env` setting, so a doc that hardcodes `MAX_ROUNDS` is wrong
        the moment an operator raises it. Serving them is the same rule the rest
        of the setup already follows for identity — ask the server, do not
        write it down twice.

        Nothing secret belongs here: this is reachable by anyone who can reach
        the engine at all. Tokens, the webhook secret and the model config are
        all deliberately absent.
        """
        return {
            "max_rounds": self.max_rounds,
            "max_diff_bytes": self.max_diff_bytes,
            "review_timeout": self.review_timeout,
            "fix_timeout": self.fix_timeout,
            "bot_email": self.bot_email,
            "reviewer_login": self.reviewer_login,
            "coder_login": self.coder_login,
        }

    def validate(self):
        problems = []
        if not self.reviewer_token:
            problems.append("FORGEJO_REVIEWER_TOKEN is not set")
        if not self.webhook_secret:
            problems.append("PINGPONG_WEBHOOK_SECRET is not set")
        if self.max_rounds < 1:
            problems.append("MAX_ROUNDS must be >= 1")
        for name in ("review", "fix"):
            path = os.path.join(self.prompts_dir, name + ".md")
            if not os.path.isfile(path):
                problems.append("missing prompt: %s" % path)
        return problems

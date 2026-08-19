"""`REVIEWER_MODE` / `CODER_MODE` — where each role's brain comes from.

Three ways, chosen per role, and they are mutually exclusive within a role:

    router      Rayline routes that role's requests. rayline/pingpong.json picks
                the model for it. This is what the rest of the project assumes.
    claude-sub  No router. Hermes uses the Claude subscription this host is
                already logged into, reading ~/.claude/.credentials.json.
    codex-sub   No router. Hermes holds a ChatGPT session of its own, created
                once by `./pingpong login` and kept in a volume.

Per role rather than instance-wide because the whole premise of this project is
that the reviewer and the coder are not the same brain. A single mode would have
made the subscription modes the one place where that stops being true — and the
interesting configuration is precisely the mixed one: the reviewer on a
subscription you already pay for, the coder on a keyed endpoint, or one of each
subscription.

Neither subscription mode is a Rayline feature and neither involves Rayline at
all: `rayline router start` accepts a `subscription` main route only by deleting
it, after which the router falls back to a keyed endpoint and bills it.

The two subscription modes differ in shape because the two providers do, not by
preference:

    Claude's credential file refreshes IN PLACE. Mounting the host's ~/.claude
    into an agent therefore keeps host and container on one token, and a refresh
    inside the container is a refresh for the person who logged in.

    Codex rotates its refresh token on every refresh and Hermes never writes the
    result back to ~/.codex. Mounting that file would work exactly once and then
    leave the operator's own `codex` CLI holding a revoked token. So each agent
    gets a session of its own — the same thing Hermes' own login flow recommends
    — and nothing on the host is touched.

This module runs on the host, like `address`, and for the same reason: it has to
resolve a path in the operator's home directory and write it to `.env` before
compose reads that file. It never prompts and it is never fatal on its own; `up`
decides what to do with the exit code.
"""
import os
import sys

from . import config, models

ROLES = ("reviewer", "coder")

# The compose mount point for a host credential directory. claude-sub only —
# codex-sub mounts nothing from the host, by design (see the module docstring).
MOUNT = "/credentials"

# Where a codex-sub agent keeps the session `./pingpong login` created. A Hermes
# home of its own, on a volume, rather than the image's populated /root/.hermes:
# auth.json is written by atomic replace, so nothing narrower than a directory
# can persist it, and a volume over /root/.hermes would freeze 295MB of image
# content at first boot.
HERMES_HOME = "/hermes"

MODES = {
    "claude-sub": {
        "provider": "anthropic",
        "example": "claude-sonnet-4-6",
        # The host directory holding the login, and the file that proves it
        # happened. None means this mode brings its own credentials.
        "host_dir": ".claude",
        "host_file": ".credentials.json",
        "login": "run `claude` and sign in, or `claude setup-token`",
    },
    "codex-sub": {
        "provider": "openai-codex",
        # Not gpt-5.5-codex: the API answers 400 "not supported when using Codex
        # with a ChatGPT account" for every -codex id. The plain ids work.
        "example": "gpt-5.5",
        "host_dir": None,
        "host_file": None,
        "login": "run ./pingpong login once — it signs that agent in",
    },
}

ROUTER = "router"


def _var(role, suffix):
    return "%s_%s" % (role.upper(), suffix)


def mode(role, env=None):
    env = env if env is not None else os.environ
    return (env.get(_var(role, "MODE")) or ROUTER).strip() or ROUTER


def model(role, env=None):
    env = env if env is not None else os.environ
    return (env.get(_var(role, "MODEL")) or "").strip()


def modes(env=None):
    return dict((role, mode(role, env)) for role in ROLES)


def roles_in(name, env=None):
    """The roles running in this mode, in the fixed reviewer-then-coder order."""
    return [role for role in ROLES if mode(role, env) == name]


def router_roles(env=None):
    return roles_in(ROUTER, env)


def subscription_roles(env=None):
    return [role for role in ROLES if is_subscription(mode(role, env))]


def is_subscription(name):
    return name in MODES


def uses_host_login(name):
    """Whether this mode reads a credential file the operator already has."""
    return bool(MODES.get(name, {}).get("host_dir"))


def home():
    """The operator's home directory, as the host sees it.

    expanduser rather than $HOME: on Windows the variable is often unset even
    though the profile directory is perfectly well known, and this has to work
    from PowerShell as well as from Git Bash.
    """
    return os.path.expanduser("~")


def credentials_dir(name, base=None):
    directory = MODES[name]["host_dir"]
    if not directory:
        return None
    return os.path.join(base or home(), directory)


def credentials_file(name, base=None):
    directory = credentials_dir(name, base)
    if not directory:
        return None
    return os.path.join(directory, MODES[name]["host_file"])


def compose_path(path):
    """A host path in the form compose accepts.

    Backslashes are not portable through a compose file — Docker Desktop takes
    `C:/Users/...` and mangles `C:\\Users\\...`, where the backslash before a
    letter is read as an escape.
    """
    return path.replace("\\", "/")


def check(name, base=None):
    """Whether this mode can work, as a list of problems. Empty means yes.

    A mode that brings its own credentials has nothing to check here: the
    session lives in a volume the host cannot read, so the agent reports on it
    at start instead.
    """
    if not uses_host_login(name):
        return []
    directory = credentials_dir(name, base)
    path = credentials_file(name, base)
    if not os.path.isdir(directory):
        return ["%s does not exist — log in on this host first (%s)"
                % (directory, MODES[name]["login"])]
    if not os.path.isfile(path):
        return ["%s has no %s — log in on this host first (%s)"
                % (directory, os.path.basename(path), MODES[name]["login"])]
    return []


def ensure(out, env=None):
    """Check every role's mode, and fill in CREDENTIALS_DIR if one needs it.

    Returns 0 when the stack can start, 1 when it cannot. Reports on both roles
    before returning rather than stopping at the first: two roles configured in
    one sitting are usually wrong in two ways, and finding out about the second
    only after fixing the first is the slower of the two conversations.

    Writing to `.env` is the same trick `address` uses: compose interpolates
    that file, and it cannot interpolate `~` or a `$HOME` that PowerShell never
    set.
    """
    env = env if env is not None else os.environ
    ok = True

    for role in ROLES:
        name = mode(role, env)
        if not is_subscription(name):
            if name != ROUTER:
                out("%s=%r is not a mode. There is %s."
                    % (_var(role, "MODE"), name, ", ".join([ROUTER] + sorted(MODES))))
                ok = False
            continue

        problems = check(name)
        if problems:
            out("%s=%s, but there is no login to use:" % (_var(role, "MODE"), name))
            for problem in problems:
                out("  - %s" % problem)
            out("")
            out("That mode reads the credential file this host already has.")
            out("Nothing here logs in for you, and nothing is copied — the")
            out("directory is mounted, so a token refreshed in the container")
            out("stays refreshed.")
            ok = False
            continue

        if not model(role, env):
            out("%s=%s, but %s is empty."
                % (_var(role, "MODE"), name, _var(role, "MODEL")))
            out("")
            out("There is no router for that role, so nothing resolves its alias")
            out("into a model — Hermes needs the real id. Set it in .env, e.g.")
            out("    %s=%s" % (_var(role, "MODEL"), MODES[name]["example"]))
            ok = False

    if not ok:
        return 1

    # One value for both roles, because one Claude account is one login: if
    # either role is on it, that is the directory to mount.
    wants = [mode(role, env) for role in ROLES if uses_host_login(mode(role, env))]
    if wants:
        wanted = compose_path(credentials_dir(wants[0]))
        if models.env_value("CREDENTIALS_DIR") != wanted:
            models.env_set("CREDENTIALS_DIR", wanted)
            out("CREDENTIALS_DIR -> %s" % wanted)
    return 0


def main(argv=None):
    config.load_env()
    return ensure(lambda line: print(line, flush=True))


if __name__ == "__main__":
    sys.exit(main())

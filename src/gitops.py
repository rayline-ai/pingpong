"""Git, all of it, on the API's side of the mount.

The agents only ever see a mounted working tree. They never push, never hold a
credential, and never learn the remote URL — so a compromised or confused agent
cannot rewrite history or leak a token.

Each PR gets its own clone under the work volume, so one run can never disturb
another.
"""
import os
import re
import subprocess


class GitError(RuntimeError):
    pass


def run(args, cwd=None, check=True, timeout=300):
    proc = subprocess.run(
        ["git"] + args, cwd=cwd, capture_output=True,
        encoding="utf-8", errors="replace", timeout=timeout)
    if check and proc.returncode != 0:
        raise GitError("git %s failed (%d): %s"
                       % (" ".join(args), proc.returncode, proc.stderr.strip()[-800:]))
    return proc.stdout.strip()


def _redact(url):
    return re.sub(r"//[^/@]*@", "//***@", url or "")


def slug(owner, repo, index):
    """Stable, filesystem-safe name for a PR's work dir."""
    safe = lambda part: re.sub(r"[^A-Za-z0-9._-]+", "-", part or "")
    return "%s__%s__pr%d" % (safe(owner), safe(repo), index)


def prepare_clone(clone_url, work_root, name, branch, base, bot_name, bot_email, log=print):
    """Create or refresh a clone checked out at the PR's head branch.

    Returns (clone_path, resolved_base_ref).
    """
    clone = os.path.join(work_root, name)
    os.makedirs(work_root, exist_ok=True)

    if not os.path.isdir(os.path.join(clone, ".git")):
        log("cloning %s -> %s" % (_redact(clone_url), clone))
        run(["clone", "--no-checkout", clone_url, clone], timeout=1800)
    else:
        log("reusing existing clone %s" % clone)
        run(["remote", "set-url", "origin", clone_url], cwd=clone)

    # Commits are authored by the coder bot. rounds_done() counts them by this
    # exact address, so it is also the loop's round counter.
    run(["config", "user.name", bot_name], cwd=clone)
    run(["config", "user.email", bot_email], cwd=clone)

    # Check out with LF endings regardless of any global core.autocrlf. The
    # agents run in Linux containers and rewrite whole files; a CRLF checkout
    # would turn every touched file into a whole-file whitespace diff with the
    # real change buried inside it. Set before the checkout below.
    run(["config", "core.autocrlf", "false"], cwd=clone)
    run(["config", "core.eol", "lf"], cwd=clone)

    _write_exclude(clone)

    log("fetching %s" % branch)
    run(["fetch", "--prune", "origin", branch], cwd=clone, timeout=1800)

    base_remote = base.split("/", 1)[1] if base.startswith("origin/") else base
    try:
        run(["fetch", "origin", base_remote], cwd=clone, timeout=1800)
    except GitError:
        log("warning: could not fetch base %r; using whatever is already local" % base)

    # Hard reset onto the remote branch: any local commit a previous run failed
    # to push is intentionally discarded, so every round starts from what the PR
    # actually shows.
    run(["checkout", "-B", branch, "origin/" + branch], cwd=clone)
    run(["reset", "--hard", "origin/" + branch], cwd=clone)
    # -x also removes the excluded artifacts below, so a round never inherits a
    # previous round's build output.
    run(["clean", "-fdx"], cwd=clone)

    base_ref = base if _rev_exists(clone, base) else "origin/" + base_remote
    if not _rev_exists(clone, base_ref):
        raise GitError("base ref %r not found in the clone" % base)

    return clone, base_ref


# Build and tool output the agent produces by running the code it is fixing.
# `commit_all` stages everything in the tree, so without this a PR picks up
# whatever the agent's test run left behind — observed: __pycache__/*.pyc.
ARTIFACTS = (
    "__pycache__/", "*.py[cod]", "*.egg-info/", ".eggs/",
    ".pytest_cache/", ".mypy_cache/", ".ruff_cache/", ".tox/", ".coverage",
    ".venv/", "venv/", "node_modules/", ".DS_Store",
)


def _write_exclude(repo):
    """Ignore build artifacts for this clone only.

    Goes in `.git/info/exclude` rather than `.gitignore`: it is never committed,
    so the target repo's own ignore rules are left exactly as its authors wrote
    them. It applies to untracked files only — a repo that deliberately tracks
    one of these can still have it modified.
    """
    path = os.path.join(repo, ".git", "info")
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "exclude"), "w", encoding="utf-8") as fh:
        fh.write("# Written by PingPong. Not part of the repository.\n")
        fh.write("\n".join(ARTIFACTS) + "\n")


def _rev_exists(repo, ref):
    try:
        run(["rev-parse", "--verify", "--quiet", ref + "^{commit}"], cwd=repo)
        return True
    except GitError:
        return False


def diff(repo, base_ref, head="HEAD"):
    """Three-dot diff: what this branch adds relative to where it forked, not
    everything that has landed on the base since."""
    return run(["diff", "--no-color", "%s...%s" % (base_ref, head)], cwd=repo, timeout=120)


def changed_files(repo, base_ref, head="HEAD"):
    out = run(["diff", "--name-only", "%s...%s" % (base_ref, head)], cwd=repo)
    return [line for line in out.splitlines() if line.strip()]


def is_dirty(repo):
    return bool(run(["status", "--porcelain"], cwd=repo))


def commit_all(repo, message):
    """Stage and commit everything in the tree. Returns the new sha, or None."""
    run(["add", "-A"], cwd=repo)
    if not run(["diff", "--cached", "--name-only"], cwd=repo):
        return None
    run(["commit", "-m", message, "--no-verify"], cwd=repo)
    return run(["rev-parse", "HEAD"], cwd=repo)


def commits_ahead(repo, branch):
    return int(run(["rev-list", "--count", "origin/%s..HEAD" % branch], cwd=repo) or "0")


def push(repo, branch, log=print):
    """Fast-forward push. Never forces — the PR's history is not rewritten under
    a reviewer who may already be reading it."""
    ahead = commits_ahead(repo, branch)
    if not ahead:
        log("nothing to push (no new commits)")
        return False
    log("pushing %d commit(s) to origin/%s" % (ahead, branch))
    run(["push", "origin", "%s:%s" % (branch, branch)], cwd=repo, timeout=1800)
    return True

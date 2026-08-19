"""Agent adapter — Hermes, one container per role.

This module knows nothing about models, endpoints, base URLs or credentials —
and nothing about which of the three ways of having a brain is in use. A
container either runs its own Rayline router against the shared
rayline/pingpong.json and requests its role alias, or talks to a provider
directly on a subscription with no router at all; either way this module writes a
prompt and runs one command. Changing a brain is a config edit, never a code
change.

`review()` and `fix()` are the only seam: prompt in, text out. That is what
absorbs a runtime change.
"""
import os
import subprocess


class AgentError(RuntimeError):
    pass


# Where the API drops prompt files. Inside the shared volume so both agents can
# read them; the reviewer's mount is read-only, so it can read but not rewrite.
PROMPT_DIR = "/work/.pingpong"


def _write_prompt(runs_dir, name, text):
    os.makedirs(PROMPT_DIR, exist_ok=True)
    path = os.path.join(PROMPT_DIR, name)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    if runs_dir:
        os.makedirs(runs_dir, exist_ok=True)
        with open(os.path.join(runs_dir, name), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    return path


def _exec(container, worktree, prompt_path, timeout):
    """Run one headless Hermes turn in `container` and return its output.

    `-z` is Hermes' one-shot headless form. The prompt is passed through a file
    rather than argv because a diff comfortably exceeds any argv limit.

    `hermes-run`, not `hermes`: the two ANTHROPIC_* variables the image bakes in
    have to be stripped in the subscription modes, and `docker exec` is the only
    point where that is possible. Keeping it in the image means this module still
    knows nothing about modes — which is the same reason it knows nothing about
    models. See docker/hermes-run.sh.
    """
    inner = 'hermes-run -z "$(cat %s)"' % prompt_path
    cmd = ["docker", "exec", "-w", worktree, container, "bash", "-lc", inner]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise AgentError("%s timed out after %ds" % (container, timeout))
    except FileNotFoundError:
        raise AgentError("docker CLI not available in the API container")

    if proc.returncode != 0:
        raise AgentError("%s exited %d: %s"
                         % (container, proc.returncode, (proc.stderr or "").strip()[-800:]))

    text = (proc.stdout or "").strip()
    if not text:
        # A reasoning model can burn its whole budget on thinking and return no
        # text at all. Say so plainly rather than reporting an empty
        # review as a clean one.
        raise AgentError("%s returned no output (check the model's token budget)"
                         % container)
    return text


def review(cfg, worktree, runs_dir, prompt, round_no):
    """Read-only pass. The reviewer's container mounts the tree read-only."""
    path = _write_prompt(runs_dir, "review-%d.md" % round_no, prompt)
    return _exec(cfg.reviewer_container, worktree, path, cfg.review_timeout)


def fix(cfg, worktree, runs_dir, prompt, round_no):
    """Editing pass. The coder writes files; all git work stays in the API."""
    path = _write_prompt(runs_dir, "fix-%d.md" % round_no, prompt)
    return _exec(cfg.coder_container, worktree, path, cfg.fix_timeout)


def codex_signed_in(name):
    """Whether this agent holds a ChatGPT session, for a role on codex-sub.

    Best-effort, for `doctor` alone. The session is made by `pingpong login` and
    lives in a volume, so unlike every other credential in this project there is
    nothing on the host to look at — asking the container is the only way.

    Both stores, because a session can be in either: `hermes auth add` writes a
    credential_pool entry, which is what the runtime selects from, while the
    older singleton is what an import leaves behind.
    """
    query = ('jq -e \'((.credential_pool["openai-codex"] // []) | length) > 0 '
             'or ((.providers["openai-codex"].tokens.access_token // "") != "")\' '
             '"${HERMES_HOME:-/root/.hermes}/auth.json"')
    try:
        out = subprocess.run(
            ["docker", "exec", name, "bash", "-lc", query],
            capture_output=True, encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


def container_running(name):
    try:
        out = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", name],
            capture_output=True, encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and out.stdout.strip() == "true"

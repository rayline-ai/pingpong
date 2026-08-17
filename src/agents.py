"""Agent adapter — Hermes, one container per role.

This module knows nothing about models, endpoints, base URLs or credentials.
Each agent container runs its own Rayline router against the shared
rayline/pingpong.json and requests its role alias; Rayline resolves the alias to
a real model. Changing a brain is a config edit, never a code change.

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

    `hermes -z` is the one-shot headless form. The prompt is passed through a
    file rather than argv because a diff comfortably exceeds any argv limit.
    """
    inner = 'hermes -z "$(cat %s)"' % prompt_path
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


def container_running(name):
    try:
        out = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", name],
            capture_output=True, encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and out.stdout.strip() == "true"

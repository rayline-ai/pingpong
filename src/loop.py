"""One ping-pong round.

The loop is no longer a `for` in this process. State lives on the PR and each
round is triggered by a webhook: the coder pushes, Forgejo fires, the next round
starts. That is what makes the reviewer swappable — a human leaving a
REQUEST_CHANGES review drives the next round exactly like the agent does — and it
gives the webhook-storm guard for free, because rounds are counted from the
coder's commits, which only the coder can create.
"""
import os
import re

from . import agents, forgejo, gitops

VERDICT_RE = re.compile(r"^\s*VERDICT:\s*(APPROVE|REQUEST_CHANGES)\s*$",
                        re.IGNORECASE | re.MULTILINE)


def parse_verdict(text):
    """Translate the reviewer's text into a Forgejo review event.

    This is a boundary translator, not the store: the authoritative verdict is
    the review state on the PR. The last VERDICT: line wins, because a reviewer
    that reconsiders mid-answer means the later line. No verdict at all is
    REQUEST_CHANGES — silence must never approve.
    """
    matches = VERDICT_RE.findall(text or "")
    if not matches:
        return forgejo.REQUEST_CHANGES
    return (forgejo.APPROVED if matches[-1].upper() == "APPROVE"
            else forgejo.REQUEST_CHANGES)


def render(template, **values):
    """Fill {placeholders} with str.replace, not str.format.

    A diff is full of braces. str.format would try to interpret them and raise on
    the first one.
    """
    out = template
    for key, value in values.items():
        out = out.replace("{%s}" % key, str(value))
    return out


def _truncate(text, limit):
    if limit <= 0 or len(text) <= limit:
        return text
    head = text[:limit]
    return head + ("\n\n[... truncated: %d of %d bytes shown ...]"
                   % (len(head), len(text)))


def _load_prompt(cfg, name):
    with open(os.path.join(cfg.prompts_dir, name + ".md"), encoding="utf-8") as handle:
        return handle.read()


def _previous_context(fj, owner, repo, index):
    """The last substantive review already on the PR, framed as context.

    Read back off the PR rather than held in memory, so a round picks up where
    the last one left off even though it runs in a fresh process — and so a
    human's review counts as context exactly like the agent's.
    """
    for entry in reversed(fj.reviews(owner, repo, index) or []):
        state = (entry.get("state") or "").upper()
        if state in (forgejo.APPROVED, forgejo.REQUEST_CHANGES) and entry.get("body"):
            return ("## Your previous review of this PR\n\n"
                    "The changes below already respond to it. Do not repeat findings "
                    "that have been addressed; do re-raise any that have not.\n\n"
                    + entry["body"])
    return ""


def run_round(cfg, fj, owner, repo, index, log=print):
    """Run one review (and, if it asks for changes, one fix) on a PR.

    Returns a dict describing what happened. The next round, if any, arrives as
    the webhook fired by this round's push.
    """
    pr = fj.pull_request(owner, repo, index)
    if pr.get("state") != "open":
        return {"action": "skipped", "reason": "pull request is not open"}
    if pr.get("merged"):
        return {"action": "skipped", "reason": "pull request is already merged"}

    branch = (pr.get("head") or {}).get("ref")
    base = (pr.get("base") or {}).get("ref")
    if not branch or not base:
        return {"action": "skipped", "reason": "pull request has no head/base ref"}

    commits = fj.pull_commits(owner, repo, index)
    rounds = forgejo.rounds_done(commits, cfg.bot_email)
    if rounds >= cfg.max_rounds:
        log("round limit reached (%d)" % cfg.max_rounds)
        fj.comment(owner, repo, index,
                   "PingPong stopped after %d round(s) without an approval. "
                   "Needs a human." % rounds)
        return {"action": "stopped", "reason": "round limit", "rounds": rounds}

    round_no = rounds + 1
    log("PR #%d %s -> %s, round %d/%d" % (index, branch, base, round_no, cfg.max_rounds))

    name = gitops.slug(owner, repo, index)
    clone, base_ref = gitops.prepare_clone(
        # The coder's credential: this remote is what the fix is pushed over.
        fj.clone_url(owner, repo, token=cfg.coder_token),
        cfg.work_root, name, branch, base,
        cfg.bot_name, cfg.bot_email, log=log)
    runs_dir = os.path.join(cfg.work_root, ".pingpong", "runs", name)

    changed = gitops.changed_files(clone, base_ref)
    if not changed:
        return {"action": "skipped", "reason": "no changes against the base"}

    raw_diff = gitops.diff(clone, base_ref)
    diff_text = _truncate(raw_diff, cfg.max_diff_bytes)

    review_prompt = render(
        _load_prompt(cfg, "review"),
        round=round_no, max_rounds=cfg.max_rounds,
        # Hermes does not inherit the cwd `docker exec -w` sets — its shell tool
        # runs in the home directory regardless. So the checkout has to be named
        # in the prompt; otherwise the agent hunts the filesystem for it and may
        # edit a different PR's tree, whose changes are then never committed.
        worktree=clone,
        title=pr.get("title") or "(no title)",
        description=pr.get("body") or "(no description)",
        diff=diff_text,
        previous_context=_previous_context(fj, owner, repo, index))

    log("reviewing (%d file(s), %d bytes of diff)" % (len(changed), len(diff_text)))
    review_text = agents.review(cfg, clone, runs_dir, review_prompt, round_no)
    verdict = parse_verdict(review_text)

    # A real review event, not a comment. A comment carries no approval state, so
    # it can neither gate a merge nor end the loop.
    fj.create_review(owner, repo, index, verdict, review_text)
    log("verdict: %s" % verdict)

    if verdict == forgejo.APPROVED:
        return {"action": "approved", "round": round_no, "review": review_text}

    fix_prompt = render(
        _load_prompt(cfg, "fix"),
        round=round_no, max_rounds=cfg.max_rounds, worktree=clone, review=review_text)

    log("fixing")
    fix_text = agents.fix(cfg, clone, runs_dir, fix_prompt, round_no)

    if not gitops.is_dirty(clone):
        log("coder changed nothing")
        fj.comment(owner, repo, index,
                   "PingPong round %d: changes were requested but the coder "
                   "made no edits. Needs a human.\n\n%s" % (round_no, fix_text))
        return {"action": "stalled", "round": round_no, "reason": "no edits"}

    sha = gitops.commit_all(clone, _commit_message(round_no, fix_text))
    pushed = gitops.push(clone, branch, log=log)
    return {"action": "fixed", "round": round_no, "sha": sha, "pushed": pushed,
            "review": review_text, "fix": fix_text}


def _commit_message(round_no, fix_text):
    """Subject from the coder's summary — what changed, not what was asked.

    Derived from the *review* the message would describe the PR rather than the
    commit, since a review opens by restating what the PR does.
    """
    summary = _fixed_bullet(fix_text) or _first_prose(fix_text) or "address review feedback"
    body = (fix_text or "").strip()
    subject = "pingpong round %d: %s" % (round_no, summary[:60].rstrip())
    return "%s\n\n%s" % (subject, body) if body else subject


def _fixed_bullet(fix_text):
    """First bullet of the `**Fixed**` section — the change itself.

    Preferred over the opening line, which in practice is narration about the
    run ("Verified by real execution: …") rather than what was changed.
    """
    lines = (fix_text or "").splitlines()
    for i, line in enumerate(lines):
        if _label(line) != "fixed":
            continue
        for candidate in lines[i + 1:]:
            if _label(candidate) in ("not fixed", "summary"):
                break
            text = _subject(candidate)
            if text:
                return text
    return ""


def _first_prose(fix_text):
    """First substantive line, stopping at `**Not fixed**`.

    Stopping matters: a coder that fixed nothing still lists what it skipped, and
    that must not become the subject of a commit describing the opposite.
    """
    for line in (fix_text or "").splitlines():
        if _label(line) == "not fixed":
            return ""
        text = _subject(line)
        if text:
            return text
    return ""


def _label(line):
    """The line's section label, lowercased, or "" if it is not one."""
    text = line.strip().lstrip("-*").strip().replace("**", "").replace("#", "").strip()
    return text.rstrip(":").lower() if text.rstrip(":").lower() in (
        "fixed", "not fixed", "summary") else ""


def _subject(line):
    """One line reduced to prose, or "" if it carries no summary of its own.

    Skips headings and standalone labels like `**Fixed**` — true of the first
    line of a well-formed fix summary, which otherwise becomes the subject.
    """
    text = line.strip().lstrip("-*").strip()
    if not text or text.startswith("#"):
        return ""
    text = text.replace("**", "").replace("`", "").strip()
    return "" if text.rstrip(":").lower() in ("fixed", "not fixed", "summary") else text

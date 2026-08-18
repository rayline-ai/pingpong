"""One ping-pong round.

The loop is no longer a `for` in this process. State lives on the PR and each
round is triggered by a webhook: the coder pushes, Forgejo fires, the next round
starts. That is what makes the reviewer swappable — a human leaving a
REQUEST_CHANGES review drives the next round exactly like the agent does — and it
gives the webhook-storm guard for free, because rounds are counted from the
coder's commits, which only the coder can create.
"""
import contextlib
import os
import re

from . import agents, forgejo, gitops


@contextlib.contextmanager
def _status_on_failure(status, round_no, who):
    """Resolve the round's commit status if the agent raises, then re-raise.

    Without this an agent that times out or dies leaves `pending` on the PR
    forever, and a stuck round becomes indistinguishable from a slow one — which
    is the very thing the status was added to tell apart. The timeout message
    itself is worth surfacing: it is how you learn FIX_TIMEOUT is too low for the
    model behind the coder.
    """
    try:
        yield
    except Exception as exc:
        status(forgejo.STATUS_ERROR, "round %d: %s failed: %s" % (round_no, who, exc))
        raise

VERDICT_RE = re.compile(r"^\s*VERDICT:\s*(APPROVE|REQUEST_CHANGES)\s*$",
                        re.IGNORECASE | re.MULTILINE)


def parse_verdict(text):
    """Translate the reviewer's text into a Forgejo review event, or None.

    This is a boundary translator, not the store: the authoritative verdict is
    the review state on the PR. The last VERDICT: line wins, because a reviewer
    that reconsiders mid-answer means the later line.

    No VERDICT: line at all returns None, not REQUEST_CHANGES. Silence still
    must never approve — but it must not be posted as a review either. The
    reviewer's contract requires that line, so its absence means we are not
    looking at a review: the runtime failed, the model ignored the contract, or
    the answer was truncated. Turning that into a REQUEST_CHANGES event makes a
    broken run indistinguishable from a reviewer that genuinely wants changes,
    and spends a round on it. run_round halts for a human instead.
    """
    matches = VERDICT_RE.findall(text or "")
    if not matches:
        return None
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


def run_round(cfg, fj, owner, repo, index, log=print, reset=False):
    """Run one review (and, if it asks for changes, one fix) on a PR.

    Returns a dict describing what happened. The next round, if any, arrives as
    the webhook fired by this round's push.

    `reset` banks the current round count in a marker comment, so a human who
    asks for another run gets a fresh MAX_ROUNDS budget rather than whatever was
    left of the old one.
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

    head_sha = (pr.get("head") or {}).get("sha")

    def status(state, description):
        """Progress, on the PR, attached to the sha this round is reading.

        Every path out of run_round past this point resolves it, so a `pending`
        left behind means the API died mid-round rather than that the round is
        merely slow — which is the distinction a blocking round could not make.
        """
        if head_sha:
            fj.set_status(owner, repo, head_sha, state, description)

    commits = fj.pull_commits(owner, repo, index)
    if reset:
        banked = forgejo.rounds_done(commits, cfg.bot_email)
        fj.comment(owner, repo, index,
                   "Round budget reset — %d more round(s) from here.\n\n%s"
                   % (cfg.max_rounds, forgejo.RESET_MARKER % banked))
        baseline = banked
    else:
        baseline = forgejo.rounds_baseline(fj.issue_comments(owner, repo, index))

    rounds = forgejo.rounds_done(commits, cfg.bot_email, baseline)
    if rounds >= cfg.max_rounds:
        log("round limit reached (%d)" % cfg.max_rounds)
        status(forgejo.STATUS_FAILURE,
               "stopped after %d round(s) without an approval" % rounds)
        fj.comment(owner, repo, index,
                   "PingPong stopped after %d round(s) without an approval. "
                   "Needs a human — comment `@pingpong` to run %d more."
                   % (rounds, cfg.max_rounds))
        return {"action": "stopped", "reason": "round limit", "rounds": rounds}

    round_no = rounds + 1
    log("PR #%d %s -> %s, round %d/%d" % (index, branch, base, round_no, cfg.max_rounds))
    status(forgejo.STATUS_PENDING,
           "round %d/%d: reviewing" % (round_no, cfg.max_rounds))

    name = gitops.slug(owner, repo, index)
    clone, base_ref = gitops.prepare_clone(
        fj.clone_url(owner, repo),
        # The coder's credential: this is what the fix is pushed over. Passed
        # per-invocation, never stored in the clone the agents can read.
        cfg.coder_token,
        cfg.work_root, name, branch, base,
        cfg.bot_name, cfg.bot_email, log=log)
    runs_dir = os.path.join(cfg.work_root, ".pingpong", "runs", name)

    changed = gitops.changed_files(clone, base_ref)
    if not changed:
        status(forgejo.STATUS_SUCCESS, "no changes against the base")
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
    with _status_on_failure(status, round_no, "reviewer"):
        review_text = agents.review(cfg, clone, runs_dir, review_prompt, round_no)
    verdict = parse_verdict(review_text)

    if verdict is None:
        # Not a review. Post a plain comment — a comment carries no approval
        # state, so this cannot gate a merge, and it does not become the
        # `previous_context` of the next round the way a review body would.
        log("reviewer produced no VERDICT: line — halting")
        status(forgejo.STATUS_ERROR, "round %d: reviewer returned no verdict" % round_no)
        fj.comment(owner, repo, index, _no_verdict_note(round_no, review_text))
        return {"action": "errored", "round": round_no,
                "reason": "reviewer produced no VERDICT: line",
                "review": review_text}

    # A real review event, not a comment. A comment carries no approval state, so
    # it can neither gate a merge nor end the loop.
    fj.create_review(owner, repo, index, verdict, review_text)
    log("verdict: %s" % verdict)

    if verdict == forgejo.APPROVED:
        status(forgejo.STATUS_SUCCESS, "approved in round %d" % round_no)
        return {"action": "approved", "round": round_no, "review": review_text}

    fix_prompt = render(
        _load_prompt(cfg, "fix"),
        round=round_no, max_rounds=cfg.max_rounds, worktree=clone, review=review_text)

    log("fixing")
    # The status the whole change is for: a coder on a local model can run for
    # many minutes, and until now nothing on the PR distinguished that from a
    # round that had died.
    status(forgejo.STATUS_PENDING,
           "round %d/%d: coder running" % (round_no, cfg.max_rounds))
    with _status_on_failure(status, round_no, "coder"):
        fix_text = agents.fix(cfg, clone, runs_dir, fix_prompt, round_no)

    if not gitops.is_dirty(clone):
        log("coder changed nothing")
        status(forgejo.STATUS_FAILURE,
               "round %d: changes requested but the coder made no edits" % round_no)
        fj.comment(owner, repo, index,
                   "PingPong round %d: changes were requested but the coder "
                   "made no edits. Needs a human.\n\n%s" % (round_no, fix_text))
        return {"action": "stalled", "round": round_no, "reason": "no edits"}

    sha = gitops.commit_all(clone, _commit_message(round_no, fix_text))
    # Resolved before the push, not after: the push fires the webhook that starts
    # the next round, and that round sets its own pending status on the new sha.
    status(forgejo.STATUS_SUCCESS, "round %d: changes pushed" % round_no)
    pushed = gitops.push(clone, branch, token=cfg.coder_token, log=log)
    return {"action": "fixed", "round": round_no, "sha": sha, "pushed": pushed,
            "review": review_text, "fix": fix_text}


def _no_verdict_note(round_no, review_text):
    """The comment left when the reviewer returns something that is not a review.

    Quotes what actually came back: with the round halted, this output is the
    only evidence of why, and the common cause — an agent runtime that failed
    but still exited 0, e.g. printing `HTTP 401: Invalid credentials.` — is
    diagnosable from one line of it.
    """
    body = _truncate((review_text or "").strip(), 2000) or "(no output)"
    return ("PingPong round %d: the reviewer returned no `VERDICT:` line, so this "
            "is not a review and no review event was posted. The round was "
            "halted rather than counted. Usually the agent runtime failed — "
            "check `pingpong logs` and the agent's Rayline router log. Needs a "
            "human.\n\nWhat the reviewer returned:\n\n```\n%s\n```" % (round_no, body))


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

"""Forgejo API client.

Forgejo is the UI *and* the state store. Round state lives on the PR, not in this
process, which is what makes the reviewer pluggable — a human reviewer leaving a
REQUEST_CHANGES review drives the next round exactly like the agent does.

stdlib only, on purpose: the API image stays dependency-free.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request

APPROVED = "APPROVED"
REQUEST_CHANGES = "REQUEST_CHANGES"
COMMENT = "COMMENT"


class ForgejoError(RuntimeError):
    pass


class Forgejo:
    def __init__(self, base_url, token, timeout=60):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, method, path, payload=None):
        url = "%s/api/v1%s" % (self.base_url, path)
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", "token %s" % self.token)
        req.add_header("Accept", "application/json")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:800]
            raise ForgejoError("%s %s -> %d: %s" % (method, path, exc.code, detail))
        except urllib.error.URLError as exc:
            raise ForgejoError("%s %s -> %s" % (method, path, exc.reason))
        return json.loads(raw) if raw.strip() else None

    # -- reads ---------------------------------------------------------------

    def pull_request(self, owner, repo, index):
        return self._request("GET", "/repos/%s/%s/pulls/%d" % (owner, repo, index))

    def pull_commits(self, owner, repo, index):
        return self._request(
            "GET", "/repos/%s/%s/pulls/%d/commits?limit=100" % (owner, repo, index)) or []

    def reviews(self, owner, repo, index):
        return self._request(
            "GET", "/repos/%s/%s/pulls/%d/reviews?limit=100" % (owner, repo, index)) or []

    def clone_url(self, owner, repo, token=None):
        """Authenticated clone URL. Only ever used inside the API container.

        `token` overrides the API's own. Forgejo attributes an "added N commits"
        timeline event to whoever *pushed*, not to the commit author, so pushing
        with the reviewer's token would credit the reviewer for the coder's work.
        """
        parts = urllib.parse.urlsplit(self.base_url)
        netloc = "%s@%s" % (urllib.parse.quote(token or self.token, safe=""), parts.netloc)
        return urllib.parse.urlunsplit(
            (parts.scheme, netloc, "%s/%s/%s.git" % (parts.path.rstrip("/"), owner, repo), "", ""))

    # -- writes --------------------------------------------------------------

    def create_review(self, owner, repo, index, event, body):
        """Post a real review event.

        `event` must be APPROVED or REQUEST_CHANGES for the review to gate a
        merge — a COMMENT carries no approval state and cannot terminate the loop
        or satisfy branch protection.
        """
        if event not in (APPROVED, REQUEST_CHANGES, COMMENT):
            raise ForgejoError("unknown review event %r" % event)
        return self._request(
            "POST", "/repos/%s/%s/pulls/%d/reviews" % (owner, repo, index),
            {"event": event, "body": body})

    def issue_comments(self, owner, repo, index):
        return self._request(
            "GET", "/repos/%s/%s/issues/%d/comments?limit=100" % (owner, repo, index)) or []

    def comment(self, owner, repo, index, body):
        """Plain issue comment — for progress notes, never for a verdict."""
        return self._request(
            "POST", "/repos/%s/%s/issues/%d/comments" % (owner, repo, index),
            {"body": body})


# Written into the marker comment a `@pingpong` command posts. The number is the
# round count at that moment, so the budget restarts from there. It lives in the
# PR rather than in the API's memory for the same reason the round count does:
# the PR is the state store, and a restarted container must not forget.
RESET_MARKER = "<!-- pingpong:reset rounds=%d -->"
_RESET_RE = re.compile(r"<!--\s*pingpong:reset\s+rounds=(\d+)\s*-->")


def rounds_done(commits, bot_email, baseline=0):
    """How many rounds this PR has run since the last reset.

    Counted from the coder's *commits*, not from comments. A comment count would
    be inflated by every webhook a bot comment itself provokes — the storm guard
    is free if the count is anchored to something only the coder can produce.
    """
    total = 0
    for entry in commits:
        author = (entry.get("commit") or {}).get("author") or {}
        if (author.get("email") or "").lower() == bot_email.lower():
            total += 1
    return max(0, total - baseline)


def rounds_baseline(comments):
    """The round count banked by the most recent reset, or 0.

    Highest wins rather than last-in-list: comment order is the API's, and a
    lower number would hand out a bigger budget than the human asked for.
    """
    found = [int(m.group(1))
             for entry in comments or []
             for m in [_RESET_RE.search(entry.get("body") or "")] if m]
    return max(found) if found else 0


def latest_review_state(reviews, bot_login=None):
    """The last non-comment review state on the PR, or None."""
    for entry in reversed(reviews or []):
        state = (entry.get("state") or "").upper()
        if state not in (APPROVED, REQUEST_CHANGES):
            continue
        if bot_login and (entry.get("user") or {}).get("login") != bot_login:
            continue
        return state
    return None

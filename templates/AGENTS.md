# AGENTS.md

Instructions for an agent working in this repository.

> **Template.** Copy this into a repository that PingPong reviews and edit the
> parts marked *decide this per repo*. Everything else is derived at runtime and
> should be left alone — the whole design of this file is that it names no host,
> no person and no limit, so it cannot go stale when the instance changes.

Work is reviewed on a PingPong instance first, and only reaches the upstream
forge once the loop has approved it.

## Where things are

PingPong runs on another machine, not here. There is no `docker compose`, no
`./pingpong` CLI and no container logs on this machine — you reach the instance
over HTTP only.

Nothing in this document names a host, an owner or a person. All of it comes
from the `forgejo` remote and the credential behind it:

```bash
FORGEJO=$(git remote get-url forgejo | sed -E 's#^(https?://[^/]+)/.*#\1#')
REPO=$(git remote get-url forgejo | sed -E 's#^https?://[^/]+/##; s#\.git$##')
ME=$(curl -n -s --max-time 5 "$FORGEJO/api/v1/user" \
     | python3 -c 'import json,sys; print(json.load(sys.stdin)["login"])')
```

Run those first, every session, and check `$ME` is non-empty before anything
else. Empty means the `~/.netrc` entry is missing or wrong, and you want to know
that now rather than halfway through a push.

`REPO` uses two `sed` expressions rather than one with `.+?` because BSD `sed`
on macOS has no lazy quantifiers and fails silently.

If the `forgejo` remote is missing, ask the human for the URL — do not guess it:

```bash
git remote add forgejo http://<host>:<port>/<owner>/<repo>.git
```

Credentials live in `~/.netrc`, never in this repo:

```
machine <host> login <you> password <token>
```

An access token, not the account password — on a plain-HTTP LAN instance it
crosses the network in the clear, and a token can be revoked on its own. That
one entry serves both `git push forgejo` and `curl -n` against the API. If it is
absent, stop and ask — do not put a credential in a URL, a command line, or a
file inside this repo.

Every `curl` below carries `--max-time`. An instance that accepts the connection
and then never answers is the normal failure mode, and without a bound it hangs
the session instead of reporting anything.

## The instance's limits

Ask the engine rather than trusting a number written here. Every one of these is
an operator's `.env` setting, so anything this file claimed about them would be
wrong the moment the operator changed it:

```bash
API=<engine base URL>            # decide this per repo; ask the human once
curl -s --max-time 5 "$API/config"
```

```json
{"max_rounds": 3, "max_diff_bytes": 60000, "review_timeout": 900,
 "fix_timeout": 1800, "bot_email": "...", "reviewer_login": "...",
 "coder_login": "..."}
```

Read `max_diff_bytes` before sizing a branch and `max_rounds` before deciding a
loop has stalled. `bot_email` is what the coder's commits are authored with —
you need it in step 6.

## Before you start

Check the instance answers, and that your identity is coherent:

```bash
curl -s --max-time 5 "$FORGEJO/api/v1/version"
git config user.email
curl -n -s --max-time 5 "$FORGEJO/api/v1/user/emails"
```

`git config user.email` must appear in that list. Forgejo links a commit to an
account by the author's email, so if it does not, every commit you push shows as
an unlinked author on the PR. That is the one per-person invariant this setup
has; report a mismatch rather than working around it.

If the instance does not answer at all, stop. A PR opened against a stopped
stack sits there silently: nothing queues, the webhook delivery just fails.

## The cycle

### 1. Refresh the review copy of `main`

*Decide this per repo.* If the instance mirrors an upstream forge, `main` there
holds no original work and should be overwritten from upstream every cycle —
otherwise the reviewer diffs your branch against a stale base and comments on
code that has already changed:

```bash
git fetch origin
git checkout main && git merge --ff-only origin/main
git push forgejo --force origin/main:main
```

If instead the instance *is* the only forge, drop this step and never
force-push anything.

### 2. Make the change

```bash
git checkout -b <branch> origin/main
# edit, then commit
```

Keep the diff under `max_diff_bytes`. Past that the reviewer is handed a
truncated diff and says so, but it is still reviewing a prefix. Split large work
across branches rather than letting it truncate.

### 3. Push and open the PR

```bash
git push forgejo HEAD:refs/heads/<branch>
```

```bash
curl -n --max-time 15 -X POST "$FORGEJO/api/v1/repos/$REPO/pulls" \
    -H 'Content-Type: application/json' \
    -d '{"head":"<branch>","base":"main","title":"<title>","body":"<what and why>"}'
```

The response contains `"number"` — that is the PR index used below.

Open it as `$ME`, never as the reviewer or coder account (`reviewer_login` and
`coder_login` from `/config`). Forgejo refuses to let an account review its own
PR, so a bot-authored PR silently never gets reviewed.

Write a real description. It is passed to the reviewer verbatim, and "update
stuff" gives it nothing to check the diff against.

### 4. Wait for the loop

Opening the PR fires the webhook and round 1 starts by itself. A round can take
up to `review_timeout` plus `fix_timeout`.

```bash
curl -n -s --max-time 10 "$FORGEJO/api/v1/repos/$REPO/pulls/<n>/reviews" \
    | python3 -c 'import json,sys; [print(r["state"], r["user"]["login"]) for r in json.load(sys.stdin)]'
```

The `pingpong/round` commit status is `pending` while a round is in flight and
resolves on every exit, including a timeout:

```bash
curl -n -s --max-time 10 "$FORGEJO/api/v1/repos/$REPO/commits/<sha>/statuses" \
    | python3 -c 'import json,sys; [print(s["context"], s["status"], s["description"]) for s in json.load(sys.stdin)]'
```

Poll on a slow interval. Do not push anything while a round is running — the
coder is about to push to the same branch.

Finer-grained diagnosis only exists on the host. If you need it, ask the human
to look; you cannot reach it from here.

### 5. Take the result back

**Mandatory when the loop made any commits.** The coder's fixes exist only on
the review branch; your local branch is behind.

```bash
git fetch forgejo
git reset --hard forgejo/<branch>
```

Skip this and you will push the un-fixed version upstream without noticing.

### 6. Send it upstream

*Decide this per repo.* Only once the PR is `APPROVED`.

The coder's commits are authored with the instance's `bot_email`, an address
with no upstream account, so they render as an unlinked author. Decide whether
to squash them into your own commits with a trailer, or keep them to show the
loop's steps:

```
Co-authored-by: <coder_login> <bot_email from /config>
```

### 7. Clean up

*Decide this per repo.* Delete the review branch, and leave the review PR
closed rather than merged if the instance is a mirror.

## Reading the loop's output

Every one of these is on the PR. Read it before reacting.

- **`REQUEST_CHANGES`, coder pushed a fix.** Normal. The push fires the next
  round on its own. Wait.
- **"stopped after N round(s)".** `max_rounds` is spent. Comment `@pingpong` on
  the PR to grant a fresh budget from that point. Only do this if the last
  review shows real progress; if the two agents are going in circles, fix it
  yourself instead.
- **"the coder made no edits".** The coder read the review and changed nothing.
  Make the change by hand, commit, and push — that fires a new round, and your
  commits do **not** count against `max_rounds` (only commits authored with
  `bot_email` do).
- **"no `VERDICT:` line".** Not a review — the agent runtime failed. Report it;
  it needs someone on the host to read the logs. Do not retry blindly.

You can also review the PR yourself. A human `REQUEST_CHANGES` review drives a
round exactly like the agent's, so it is a way to hand the coder specific work.

## Hard rules

1. Never force-push a **PR branch**. A round may be reading it, and the coder
   pushes fast-forward only. A PR branch carries the only copy of the coder's
   work.
2. Always `git fetch forgejo && git reset --hard forgejo/<branch>` before
   sending that branch upstream.
3. Never open the PR as the reviewer or coder account.
4. Never write a credential into this repo, a command line, or a remote URL.
5. Never commit the instance's address to this repo — it belongs in the
   `forgejo` remote, which is local to the clone.
6. Nothing goes upstream without an `APPROVED`, unless the human says otherwise.

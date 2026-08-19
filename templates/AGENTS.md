# AGENTS.md

Instructions for an agent working in this repository.

> **Template.** Copy this into a repository that PingPong reviews and edit the
> parts marked *decide this per repo*. Everything else is derived at runtime and
> should be left alone — the whole design of this file is that it names no host,
> no person and no limit, so it cannot go stale when the instance changes.

Work is reviewed on a PingPong instance first, and only reaches the upstream
forge once the loop has approved it.

## Where things are

*Decide this per repo.* PingPong usually runs on another machine, and then there
is no `docker compose`, no `./pingpong` CLI and no container logs here — you
reach the instance over HTTP only. But it may also run on this one, and if it
does, say so here and name the checkout. An agent that has been told the logs
are unreachable will hand back a failure it could have read itself.

Everything below goes over HTTP and is unchanged either way. The only thing this
decides is whether "ask the human to look" is the last resort or the first thing
to try.

Nothing in this document names a host, an owner or a person. All of it comes
from the `forgejo` remote and the credential behind it:

```bash
FORGEJO=$(git remote get-url forgejo | sed -E 's#^(https?://[^/]+)/.*#\1#')
REPO=$(git remote get-url forgejo | sed -E 's#^https?://[^/]+/##; s#\.git$##')
PY=$(for c in python3 python py; do "$c" -c '' >/dev/null 2>&1 && { echo "$c"; break; }; done)
ME=$(curl -n -s --max-time 5 "$FORGEJO/api/v1/user" \
     | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["login"])')
```

Run those first, every session, and check `$ME` is non-empty before anything
else. Empty means the `~/.netrc` entry is missing or wrong, and you want to know
that now rather than halfway through a push.

`REPO` uses two `sed` expressions rather than one with `.+?` because BSD `sed`
on macOS has no lazy quantifiers and fails silently.

`PY` is found by **running** each candidate, not by looking it up on `PATH`.
Windows ships a `python3` that is a Microsoft Store stub: it is a real file, so
`command -v python3` finds it, and it then prints *"Python was not found"* to
stderr and exits `49` without running anything. Piped into a command
substitution that leaves `$ME` empty — which reads exactly like the missing
`~/.netrc` entry above, and is not one. Empty `$PY` means no working
interpreter; use any JSON reader you have, or stop and say so.

If the `forgejo` remote is missing, ask the human for the URL — do not guess it:

```bash
git remote add forgejo http://<host>:<port>/<owner>/<repo>.git
```

A missing remote may also mean this repository has never been put on the
instance at all. Check before adding one — a remote pointing at a repository
that does not exist fails later, and less clearly, than one that is absent:

```bash
curl -n -s -o /dev/null -w '%{http_code}\n' --max-time 5 \
    "<instance>/api/v1/repos/<owner>/<repo>"
```

`404` means it still has to be created, the bot accounts added as collaborators
and a webhook registered. That is one command on the machine running PingPong,
and it is idempotent, so a half-finished attempt is safe to repeat:

```bash
./pingpong onboard <path to this repository>
```

Whether you can run it yourself is the per-repo question at the top of this file.
If PingPong is elsewhere, say the repository is not on the instance and name that
command — do not guess a remote into place, and do not build the same thing out
of `curl` calls, which is where the scope and ordering traps it handles live.

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
API=$(git config pingpong.api)   # set once per clone; see below
curl -s --max-time 5 "$API/config"
```

The engine's address lives in this clone's git config, **not in this file**. It
is on a different port from Forgejo, so unlike `$FORGEJO` it cannot be derived
from the remote — and writing it here would put a private address into a
repository that may be pushed somewhere public, which rule 5 forbids. Git config
is local to the clone and never travels with a push:

```bash
git config pingpong.api http://<host>:<engine port>
```

If it is unset, ask the human for the address. Do not guess it from the
`forgejo` remote by changing the port.

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

Branch from whichever `main` step 1 left authoritative — `origin/main` if the
instance mirrors an upstream, `forgejo/main` if the instance is the only forge:

```bash
git fetch forgejo
git checkout -b <branch> forgejo/main        # or origin/main, per step 1
# edit, then commit
```

Do not assume `origin` exists. A repository that lives only on the instance has
just the `forgejo` remote, and `origin/main` there is not a stale base — it is a
name git has never heard of, which fails before you have written anything.

Nor assume a local `main`. `onboard` pushes this folder's `HEAD` to `main` on the
instance whatever the local branch is called, so a clone that was `git init`ed
locally is commonly on `master` with no `main` and no tracking. `forgejo/main` is
the branch the reviewer diffs against; that is the one to branch from.

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
    | "$PY" -c 'import json,sys; [print(r["state"], r["user"]["login"]) for r in json.load(sys.stdin)]'
```

The `pingpong/round` commit status is `pending` while a round is in flight and
resolves on every exit, including a timeout:

```bash
curl -n -s --max-time 10 "$FORGEJO/api/v1/repos/$REPO/commits/<sha>/statuses" \
    | "$PY" -c 'import json,sys; [print(s["context"], s["status"], s["description"]) for s in json.load(sys.stdin)]'
```

Poll on a slow interval. Do not push anything while a round is running — the
coder is about to push to the same branch.

Finer-grained diagnosis only exists on the host — `pingpong logs` and the
container state. Whether you can reach that yourself is the per-repo question
at the top of this file; if it is not on this machine, ask the human to look.

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

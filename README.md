# PingPong

Two agents review each other's work on a pull request until it is ready to merge.

**PingPong = Forgejo + PingPongAPI.** Forgejo is the UI, the state store and the
merge gate. PingPongAPI is the engine: it hears a webhook, runs one round, and
pushes. The round's result lands on the PR as a real review event, so the loop's
state is something you can see and a human can take over at any point.

Built on [Forgejo](https://codeberg.org/forgejo/forgejo) for the forge,
[Hermes](https://github.com/NousResearch/hermes-agent) as the agent runtime, and
[Rayline](https://rayline.ai) to route each agent to a model.

## How a round works

```
PR opened  ──►  webhook  ──►  reviewer agent reads the diff
                                        │
                        APPROVED ◄──────┴──────► REQUEST_CHANGES
                            │                          │
                       loop ends                 coder agent edits
                                                       │
                                                  commit + push
                                                       │
                                                   webhook ──► next round
```

Two design choices carry most of the weight:

- **The PR is the state store.** No round counter in memory. That makes the
  reviewer pluggable — a human leaving a `REQUEST_CHANGES` review drives the next
  round exactly like the agent does.
- **Rounds are counted from the coder's commits, not from comments.** A comment
  count would be inflated by every webhook a bot comment itself provokes. Anchor
  the count to something only the coder can produce and the storm guard is free.

`MAX_ROUNDS` (default 3) caps that count. On the next webhook past the limit the
loop stops, comments *"needs a human"* and leaves the PR unmerged — so a pair that
cannot converge costs three rounds, not an unbounded number. Both prompts are told
which round they are on and what the limit is.

**Comment `@pingpong` on the PR to run it again.** `@pingpong-reviewer` and
`@pingpong-coder` work too, so whichever name the mention autocomplete offers you
is fine. That grants a fresh `MAX_ROUNDS` from that point: the count so far is
banked in a hidden marker in the comment PingPong posts back, keeping the PR the
only state store. Comments from the bots themselves are ignored.

## Models

Neither the code nor `.env` names a model. Both live in one file:

```jsonc
// rayline/pingpong.json
"model_routes": {
  "reviewer-brain": { "model": "rayline-router" },
  "coder-brain":    { "model": "rayline-router" }
}
```

Those keys are arbitrary aliases, not model ids. Each agent requests its role
alias and Rayline resolves it. Changing a brain is a one-line edit here — no code
change, no rebuild. `rayline-router` lets Rayline pick per request; name a real
model instead (`gpt-5.6-terra`, `z-ai/glm-5.2`, …) to pin one.

### Running a role on OpenRouter

Put `OPENROUTER_API_KEY` in `.env`, point an alias at the `openrouter` endpoint
already in the config, and recreate that agent so it picks up the key:

```jsonc
"coder-brain": { "endpoint": "openrouter", "model": "moonshotai/kimi-k3" }
```

```bash
docker compose up -d coder
```

### Running a role on a local model

Needs [ollama](https://ollama.com) on the host. Point an alias at the
`ollama-local` endpoint already in the config, then restart that agent:

```jsonc
"coder-brain": { "endpoint": "ollama-local", "model": "qwen3.5:9b-32k" }
```

```bash
docker compose restart coder
```

**Give the model a 32k context window or it will not call tools.** ollama sizes
the window from VRAM and a constrained host silently gets 4096 tokens, which
truncates Hermes' tool definitions out of the prompt. Bake it into the tag:

```bash
printf 'FROM qwen3.5:9b\nPARAMETER num_ctx 32768\n' > Modelfile
ollama create qwen3.5:9b-32k -f Modelfile
ollama ps          # CONTEXT must read 32768, not 4096
```

## Layout

```
docker-compose.yml        forgejo + api + reviewer + coder
rayline/pingpong.json     the one RouterConfig; the API never parses it
docker/agent.Dockerfile   Hermes + Rayline; one image, role set at runtime
prompts/review.md         the reviewer's contract, incl. the VERDICT: line
prompts/fix.md            the coder's contract
src/webhook.py            trigger: signature check, dedup, dispatch
src/loop.py               one round
src/forgejo.py            PR reads, review events, round counting
src/gitops.py             all git, on the API's side of the mount
src/agents.py             `docker exec hermes -z` — knows nothing about models
accounts.sh               creates the accounts and the tokens .env needs
onboard.sh                puts a repository on the instance; host-side, so it
                          can see your folder
templates/AGENTS.md       instructions to copy into a repository under review
watch.sh                  a test aid, not part of the system: live round progress
SETUP.md                  standing an instance up, once per instance
```

## Setup

You need Docker with Compose, and a Rayline router key (`rlk-…`) from
[platform.rayline.ai/keys](https://platform.rayline.ai/keys). Everything else runs
in containers.

```bash
cp .env.sample .env        # fill in RAYLINE_ROUTER_API_KEY
./pingpong up              # builds the images; first run pulls a lot
./pingpong accounts        # the admin, the two bots and their tokens, and you
./pingpong up              # again, so the engine picks those up
./pingpong onboard ../some-repo
./pingpong doctor
```

Two passes, because the accounts and the tokens the rest of `.env` needs cannot
be minted until Forgejo has booted. `accounts` prints a password per account, and
each one has a first login that only a human can do. **[SETUP.md](SETUP.md)** is
the actual procedure, and the Forgejo behaviours that cost an afternoon if you
meet them by surprise.

Forgejo lands on **23000**, the engine on **23080**, Forgejo's SSH on **23022** —
not 3000/8080/2222, which are the most contended numbers on a machine that runs
anything else.

## Using it from a repository

Setting the instance up is one job; working in a repository it reviews is
another, done by different people on different machines. `templates/AGENTS.md`
is the second half — copy it into a reviewed repository and edit the handful of
places it marks *decide this per repo*.

The split is worth keeping sharp. That file names no host, no person and no
limit: it derives the instance from the repository's `forgejo` remote, the
identity from the credential behind it, and the limits from the engine. Anything
it restated instead would be a second copy to keep true, and the numbers are the
ones that bite — every limit below is a per-instance `.env` setting, so
instructions that hardcode `MAX_ROUNDS` are wrong the moment an operator raises
it.

```bash
curl -s "$API/config"
```

```json
{"max_rounds": 3, "max_diff_bytes": 60000, "review_timeout": 900,
 "fix_timeout": 1800, "bot_email": "...", "reviewer_login": "...",
 "coder_login": "..."}
```

Nothing secret is served there — no tokens, no webhook secret, no model config —
because anything that can reach the engine can read it.

### What the loop says

The vocabulary a PR is written in. It is defined here, and the template points
back rather than copying it:

| On the PR | Means |
| --- | --- |
| `pingpong/round` `pending` | a round is in flight; resolved on every exit, timeouts included |
| `APPROVED` | the loop is done and the PR is ready |
| `REQUEST_CHANGES` | the coder is fixing it; its push fires the next round |
| *"stopped after N round(s)"* | `MAX_ROUNDS` spent — comment `@pingpong` to grant a fresh budget |
| *"the coder made no edits"* | the coder read the review and changed nothing; do it by hand |
| *"no `VERDICT:` line"* | not a review at all — the agent runtime failed; read `pingpong logs` |

Only the last one is a fault in PingPong itself. The other five are the loop
working, and a reader who cannot tell them apart will retry something that was
never broken.

## Commands

```bash
./pingpong up                      # build and start everything
./pingpong accounts                # the admin, the two bots and their tokens, you
./pingpong accounts --user x@y.z   # add a person later, --token if they are elsewhere
./pingpong onboard ../some-repo    # put a repository on the instance
./pingpong doctor                  # config, containers, Forgejo reachability
./pingpong round owner/repo#123    # run one round by hand
./pingpong logs                    # follow the API
./pingpong down
```

`round` exits non-zero unless the PR ended approved, so it can gate a script.

`up`, `down`, `logs`, `accounts` and `onboard` run on the host; everything else
runs inside the API container. Those last two have to: the container can see
neither the folder being onboarded nor the `~/.netrc` the push authenticates
with, and cannot run Forgejo's CLI or rewrite the operator's `.env`.

## Tests

```bash
python -m unittest discover -s tests
```

They cover the pure logic — verdict translation, prompt rendering, round
counting, webhook triggers, signature checks — and need no Docker, Forgejo or
model.

## Notes on the architecture

**Each agent container runs its own Rayline router.** `rld` binds `127.0.0.1`
unconditionally, so a single shared router container would be unreachable from
its siblings. Containers have separate network and filesystem namespaces, so a
router per agent is the correct shape rather than a workaround: each binds its
own loopback, and the `rld` pidfile singleton cannot collide. It is still one
config file, mounted into both.

**Agents use the injector on `:20809`, not the proxy on `:20810`.** The proxy is
a CONNECT-style MITM that needs its CA trusted; a plain `POST` to it returns 405.

**The reviewer's tree is mounted read-only.** Enforced at the mount, not by an
agent setting, because that is the guarantee that does not depend on the agent
honouring its own configuration. If the reviewer could write, review and fix
would stop being separable.

**Agents never touch git.** They see a mounted working tree and nothing else — no
credential, no remote URL. Every git operation happens in the API container, and
pushes are fast-forward only, so a PR's history is never rewritten under a
reviewer who may already be reading it.

Keeping that true takes one deliberate step. `git clone http://<token>@host/…`
writes the authenticated URL into `.git/config`, and the clone lives in the work
volume *both* agents mount — so the coder's token used to be readable by the
reviewer, whose read-only mount stops it writing files but not reading secrets.
The token is therefore passed per git invocation (`git -c http.extraHeader=…`)
and the stored remote URL is left clean. GitHub reached the same place with
`actions/checkout`, which now keeps its token out of `.git/config` as well.

**Progress is a commit status, not a comment.** A round can hold a thread for the
whole of `FIX_TIMEOUT` (30 minutes) while a local model works, and until the push
there was nothing on the PR to distinguish that from a round that had died.
`pingpong/round` goes `pending` while the reviewer and then the coder run, and is
resolved on every exit — including a timeout. A status is the right channel
rather than a comment: it is attached to a sha, so it cannot go stale against a
force-push; it replaces itself instead of accumulating; and, unlike a bot
comment, it is not also a webhook trigger, so reporting progress can never start
a round.

**One identity check, not one per event.** Anything a PingPong account does is
excluded from triggering a round — asked once, of the sender, rather than
re-derived in each event branch. Spelled out per event it was right in one place
and wrong in another: the review branch compared only `BOT_NAME` (the *coder*),
so the reviewer's own `REQUEST_CHANGES` was never excluded and reviewed the same
unchanged diff twice. GitHub settles it the same way — actions taken with the
workflow's own token do not start a workflow run.

The one deliberate exception is `pull_request` itself: the coder's push is the
loop's forward edge, and what bounds it is `MAX_ROUNDS`, not the sender.

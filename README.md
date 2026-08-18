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
```

## Setup

You need Docker with Compose, and a Rayline router key (`rlk-…`) from
[platform.rayline.ai/keys](https://platform.rayline.ai/keys). Everything else runs
in containers. Only if you want a role on a local model do you also need ollama on
the host — see [above](#running-a-role-on-a-local-model).

```bash
cp .env.sample .env        # fill in RAYLINE_ROUTER_API_KEY
./pingpong up              # builds the images; first run pulls a lot
```

`up` is the only step that works before Forgejo exists — the rest of `.env` needs
accounts and tokens that can only be minted once Forgejo has booted, which is why
this is two passes.

Self-registration is off (`DISABLE_REGISTRATION` in `docker-compose.yml`), so the
first account is made with Forgejo's own CLI rather than through the sign-up page.
Call it `pingpong-admin`. This is the instance's administrator — it exists to run
Forgejo, not to write code:

```bash
docker compose exec -u git forgejo \
    forgejo admin user create --admin --username pingpong-admin \
    --email pingpong-admin@local --random-password
```

**Write down what that prints.** The generated password is the only credential
that exists at this point, it is shown once, and there is no sign-up page and no
password-reset mail to recover from losing it — the only way back in is the same
CLI (`forgejo admin user change-password`).

```
generated random password: <this is your sign-in password>
New user 'pingpong-admin' has been successfully created!
```

Sign in with it at <http://localhost:3000> and change the password when Forgejo
asks. Do that before minting any token for this account: until the password is
changed, Forgejo rejects its API writes with *"You must change your password"*,
which arrives through a perfectly valid token and so reads as a scope problem.

### Give each person their own account

`pingpong-admin` is not your account. Sign in as it, then avatar (top right) →
**Site Administration** → **User Accounts** → **Create User Account**, one per
person, and use that for day-to-day work. Because self-registration is off this
is the only way anyone gets an account, so it is also how you add the second and
third person later.

Those accounts are what open pull requests and, if you want, review them by hand.
Keeping them separate from `pingpong-admin` is what makes a PR page legible:
admin actions and review actions stop being the same name.

If an account will drive the API rather than the UI, clear **Require user to
change password** on that form — otherwise its token hits the same rejection
described above.

### Bots, secret and webhook

The rest of `.env`. Back in Forgejo, signed in as `pingpong-admin`:

1. Create **two** bot accounts, `pingpong-reviewer` and `pingpong-coder`, and put a
   token from each into `.env` as `FORGEJO_REVIEWER_TOKEN` and
   `FORGEJO_CODER_TOKEN`. One account per role is what makes the PR page readable:
   the reviewer posts the reviews, the coder authors *and pushes* the commits.
   Forgejo credits an "added N commits" event to whoever pushed rather than to the
   commit's author, so a single shared token shows the reviewer writing the fixes.

   Give `pingpong-coder` the address in `BOT_EMAIL` — that is what links a commit
   to the account — and give `pingpong-reviewer` a different one. Rounds are
   counted from commits carrying `BOT_EMAIL`, so if the reviewer shared it, its own
   commits would count as rounds. Neither account should author PRs: Forgejo
   refuses to let an account review its own.

   Admin → User Accounts → Create User Account does this, or the same CLI as
   above without `--admin`. Note `--must-change-password=false`: nobody is ever
   going to sign in as a bot to clear that flag, and while it is set Forgejo
   rejects the account's API writes.

   ```bash
   docker compose exec -u git forgejo forgejo admin user create \
       --username pingpong-coder --email pingpong-coder@local \
       --random-password --must-change-password=false
   ```

   Then each bot needs a token in `.env`. Tokens are issued per account and
   there is no admin screen for minting one on another account's behalf, so
   either sign in as the bot and use Settings → Applications, or stay in the
   CLI:

   ```bash
   docker compose exec -u git forgejo forgejo admin user generate-access-token \
       --username pingpong-coder --token-name pingpong \
       --scopes write:repository,write:user,write:issue
   ```

   `write:repository`, not `write:repo` — the short form is rejected as an
   invalid scope.
2. Set `PINGPONG_WEBHOOK_SECRET` to any long random string.
3. Add a repository webhook: `http://api:8080/webhook`, content type JSON, the
   same secret, events **Pull Request**, **Pull Request Review** and **Issue
   Comment** (the last is what `@pingpong` needs).

   Forgejo does not send one `pull_request_review` event with the state in the
   body the way GitHub does — it puts the state in the event name and calls the
   action `reviewed`:

   ```
   X-Forgejo-Event: pull_request_rejected      action: reviewed
   ```

   Both spellings are accepted. `pingpong logs` names the event of every
   delivery it ignores, and why — a trigger that silently does not fire looks
   exactly like a webhook that never arrived.
4. `./pingpong up` again to pick up the new `.env`, then `./pingpong doctor`.

Optionally turn on branch protection requiring an approving review — that is what
turns the reviewer's `APPROVED` into an actual merge gate.

## Commands

```bash
./pingpong up                      # build and start everything
./pingpong doctor                  # config, containers, Forgejo reachability
./pingpong round owner/repo#123    # run one round by hand
./pingpong logs                    # follow the API
./pingpong down
```

`round` exits non-zero unless the PR ended approved, so it can gate a script.

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

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
onboard.sh                setup step 4; host-side, so it can see your folder
templates/AGENTS.md       instructions to copy into a repository under review
watch.sh                  a test aid, not part of the system: live round progress
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

### Ports and address

Forgejo is on **23000**, the engine on **23080**, Forgejo's SSH on **23022** —
not 3000/8080/2222. Those defaults are the most contended numbers on a machine
that runs anything else, and losing one shows up as a container that refuses to
start rather than as a message naming the port. Override any of them with
`FORGEJO_PORT`, `API_PORT`, `FORGEJO_SSH_PORT`.

Only the host side moves. Inside the compose network the services keep their own
ports, which is why `FORGEJO_URL` is `http://forgejo:3000` and the webhook target
is `http://api:8080/webhook` no matter what you publish them on.

Two more settings decide who can reach the stack:

```bash
BIND_ADDR=<host LAN address>                      # which interface to publish on
FORGEJO_ROOT_URL=http://<host LAN address>:23000/ # the address everyone else uses
```

`BIND_ADDR` empty publishes on every interface — on a Windows host that includes
the Hyper-V and WSL switches. Naming one address keeps the stack on the network
it is meant to serve, at the cost of `localhost` no longer answering on that
machine.

`FORGEJO_ROOT_URL` is separate and is not cosmetic: Forgejo builds clone URLs and
the links in everything it sends from it. Leave it `localhost` while serving a
LAN and every other machine gets handed a URL pointing back at itself.

Self-registration is off (`DISABLE_REGISTRATION` in `docker-compose.yml`), so the
first account is made with Forgejo's own CLI rather than through the sign-up page.
Call it `pingpong-admin`. It is an **ops account**: it creates the other accounts
and the webhooks, and that is all. It owns no repository and authors nothing, so
that no repository's fate is tied to the account that happens to administer the
instance:

```bash
docker compose exec -u git forgejo \
    forgejo admin user create --admin --username pingpong-admin \
    --email pingpong-admin@local --random-password
```

It prints a generated password; log in with it at whatever you set
`FORGEJO_ROOT_URL` to — `localhost` only if you left `BIND_ADDR` empty — and
Forgejo will ask you to choose a new one. Do that before minting a token —
until the password is changed, Forgejo rejects the account's API writes with
*"You must change your password"*, which looks like a permissions problem and is
not one. Then, in Forgejo:

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
   above without `--admin`:

   ```bash
   docker compose exec -u git forgejo forgejo admin user create \
       --username pingpong-coder --email pingpong-coder@local \
       --random-password --must-change-password=false
   ```

   `--must-change-password=false` is what makes the token work. A CLI-created
   account is otherwise required to change its password at first login, and
   Forgejo enforces that on the **API** too — every call the token makes comes
   back `403 You must change your password`. Nothing ever logs in as these two,
   so nothing would clear it. It reads as a permissions problem and is not one;
   the flag is the whole fix, and setting it at creation costs nothing.

   Use it only for the bots. A person's account should keep the forced change —
   that is what turns the printed password into a one-shot credential.

   Mint each token with the scopes the engine actually uses — reading PRs and
   diffs, posting reviews and commit statuses, and commenting:

   ```bash
   docker compose exec -u git forgejo forgejo admin user generate-access-token \
       --username pingpong-coder --token-name pingpong --raw \
       --scopes write:repository,write:issue
   ```
2. Create **one account per person**, and give each the email that person's
   workstation already commits with.

   If you are setting this up on your own machine, that address is already on it
   and there is nothing to look up:

   ```bash
   EMAIL=$(git config user.email)   # empty means no git identity on this machine
   USER=${EMAIL%%@*}                # local part as the login; override if you prefer

   test -n "$EMAIL" && docker compose exec -u git forgejo \
       forgejo admin user create \
       --username "$USER" --email "$EMAIL" --random-password
   ```

   ```
   generated random password is 'xxxxxxxxxxxx'
   New user '<them>' has been successfully created!
   ```

   **Write that password down before you clear the terminal.** It is printed
   once and stored only as a hash, so nothing can show it to you again — and it
   is the whole of the handover: the person signs in with it at
   `$FORGEJO_ROOT_URL`, and Forgejo forces a change on first login, so it is a
   one-shot credential rather than a password you are choosing on their behalf.

   Lost it before they logged in? Do not recreate the account — that orphans
   anything already attached to it. Issue a fresh one:

   ```bash
   docker compose exec -u git forgejo forgejo admin user change-password \
       --username <them> --password '<new one>' --must-change-password
   ```

   For anyone on another machine there is nothing to derive — the host cannot see
   their git config. Have them run `git config user.email` and send you the
   result, then run the same command with it.

   That address is the whole point of the step. Forgejo links a commit to an
   account by the author's email, so an account created with anything else leaves
   every commit that person pushes showing as an unlinked author — the PR still
   works, it just stops saying who wrote what, which is most of what the loop is
   for. If they commit under several addresses, add the rest under Settings →
   Emails.

   The login is cosmetic and the email is not, which is why only the email is
   derived. `${EMAIL%%@*}` is a starting point, not a rule: it collides when two
   people share a local part across domains, and Forgejo rejects characters that
   are legal in an address but not in a username.

   The password is for the web UI. Each person also needs an **access token**:
   `templates/AGENTS.md` requires one in `~/.netrc` for `git push` and every API
   call it makes, and is firm that it must not be the account password. Mint it
   *after* their first login — until the forced password change is done Forgejo
   rejects the token's calls with the same `403` described above:

   ```bash
   docker compose exec -u git forgejo forgejo admin user generate-access-token \
       --username <them> --token-name workstation --raw \
       --scopes write:repository,write:user,read:user
   ```

   `write:repository` pushes and `POST`s a pull request, `read:user` answers the
   template's identity checks against `/api/v1/user` and `/api/v1/user/emails`.
   `write:user` is there for one call and one only — creating a repository, in
   step 4 — and it is on this token rather than on a second one because step 4
   repeats for every repository you ever add, and a token minted per repository
   is a token you cannot revoke per repository. Better one credential you know
   the whereabouts of than a growing set of forgotten ones.

   Know what it widens: `write:user` also rewrites the account's email addresses
   and SSH keys, and this token lives in plaintext in `~/.netrc`, so whatever
   reaches that workstation reaches those too. Creating repositories in Forgejo's
   UI needs no token at all — if that is the trade you want, drop `write:user`
   here and take the UI route in step 4.

   **Do not infer a scope from the endpoint's path.** Each endpoint declares its
   own, and neither direction of the obvious guess holds: creating a repository
   is `POST /api/v1/user/repos` and needs `write:user` *and* `write:repository`
   (step 4), while opening an issue is `POST /api/v1/repos/{owner}/{repo}/issues`
   and needs `write:issue` despite living under `repos` — which is why the bots
   above carry `write:issue` at all. When a call is refused, the `403` names the
   scope it wanted; read it rather than widening the token by guesswork.

   Mint once, and get the scopes right at that moment: you cannot clean up
   afterwards from here. Forgejo's `DELETE /users/{username}/tokens/{id}` refuses
   token auth and answers `401 auth method not allowed`. Revoking needs basic
   auth with the account password, so only the person themselves can do it, in
   Settings → Applications. A token minted from this CLI is one you are stuck
   with until they remove it — which is the argument for one token per person
   carrying what that person's work needs, rather than a fresh one each time a
   call turns out to be refused.

   Scopes cannot be widened after the fact, so an account minted before this
   list changed — or one refused for a scope you now know it needs — gets a
   **replacement**: run the same command again, put the new token in `~/.netrc`,
   and tell the owner to delete the stale one in Settings → Applications. That
   is not the accumulation warned about above. One token superseding another is
   a cleanup with a known end; one token per repository is not.

   **These accounts own the repositories and open the pull requests.** Not
   `pingpong-admin`, and never the two bot accounts — Forgejo refuses to let an
   account review its own PR, so a PR authored by the reviewer or the coder is
   silently never reviewed.
3. Set `PINGPONG_WEBHOOK_SECRET` to any long random string.
4. **Put a repository on the instance**, owned by a person's account. One
   command, run from this directory:

   ```bash
   ./pingpong onboard ../some-repo
   ```

   It creates the repository under the account whose email that folder commits
   with, adds `pingpong-reviewer` and `pingpong-coder` as collaborators with
   **write**, mints the owner a token if `~/.netrc` has none — or replaces one
   the create call refuses for scope — pushes `HEAD` to `main`, sets
   `pingpong.api`, copies `templates/AGENTS.md` in, and registers the webhook.
   Every step checks the instance first and reports `already` rather than
   failing, so a run interrupted halfway is repeated rather than unpicked.

   That is a script rather than a list of steps here because most of what this
   step knows is conditional — which scope a call needs, what order the
   credential and the push go in, whether the folder was ever pointed at an
   instance before. Prose cannot check any of it, and every one of those
   failures is a quiet one.

   Everything from here is per repository, and repeats for each one you add.

   Four things it does that are worth knowing anyway, because they are what
   costs time when this goes wrong elsewhere:

   - **The bots need write.** Without it the reviewer cannot post a review and
     the coder cannot push, and the round fails partway rather than at the start.
   - **The hook's secret must be non-empty.** With an empty one Forgejo answers
     `201`, the hook looks correct in the UI, and every delivery afterwards fails
     its signature check. `onboard` refuses to create it rather than leave you a
     webhook that exists and never fires.
   - **`main` has to exist before a PR can be opened against it**, which is why
     the folder's `HEAD` goes there first — and why the repository is created
     with `auto_init` false. An initialised repository already holds a commit of
     its own, and pushing real history at it is then a non-fast-forward that
     fails for a reason that reads as a permissions problem.
   - **No credential in the remote URL, and the engine's address in git config
     rather than in the repository.** Both are rules the reviewed repo's
     `AGENTS.md` states and expects to hold. The credential comes from
     `~/.netrc`, host only and no port, and that one entry serves both
     `git push forgejo` and `curl -n` against Forgejo and the engine.
   **Doing it in the UI instead:** create the repository, add the two
   collaborators under Settings → Collaborators, and add a webhook pointing at
   `http://api:8080/webhook`, content type JSON, the same secret, events **Pull
   Request**, **Pull Request Review** and **Issue Comment** — the last is what
   `@pingpong` needs.

   That is `8080`, not `API_PORT`, and it is not a typo. Forgejo calls the
   engine from inside the compose network, where the service still listens on
   its own port — the published one exists only for you.

   Read the hook back afterwards and its event list is longer than the three you
   checked: Forgejo stores `pull_request_review` expanded into its `_approved`,
   `_rejected` and `_comment` variants, and `pull_request` into `_sync`,
   `_assign` and the rest. That expansion is why those are the event names that
   actually arrive — and it does not send one `pull_request_review` event with
   the state in the body the way GitHub does. The state is in the event name and
   the action is `reviewed`:

   ```
   X-Forgejo-Event: pull_request_rejected      action: reviewed
   ```

   Both spellings are accepted. `pingpong logs` names the event of every
   delivery it ignores, and why — a trigger that silently does not fire looks
   exactly like a webhook that never arrived.
5. Edit the places `AGENTS.md` marks *decide this per repo* in the repository you
   just onboarded. It is what tells whoever works there how to drive the loop;
   without it they have a forge with two bots on it and no way to know what any
   of it means.
6. `./pingpong up` again to pick up the new `.env`, then `./pingpong doctor`.

Optionally turn on branch protection requiring an approving review — that is what
turns the reviewer's `APPROVED` into an actual merge gate.

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
./pingpong onboard ../some-repo    # put a repository on the instance (step 4)
./pingpong doctor                  # config, containers, Forgejo reachability
./pingpong round owner/repo#123    # run one round by hand
./pingpong logs                    # follow the API
./pingpong down
```

`round` exits non-zero unless the PR ended approved, so it can gate a script.

`up`, `down`, `logs` and `onboard` run on the host; everything else runs inside
the API container. `onboard` has to: the container can see neither the folder
being onboarded nor the `~/.netrc` the push authenticates with.

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

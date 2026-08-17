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
this is two passes. Open <http://localhost:3000> and register your own admin
account first (the first account registered becomes admin; self-registration is
then disabled).

Then, in Forgejo:

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
2. Set `PINGPONG_WEBHOOK_SECRET` to any long random string.
3. Add a repository webhook: `http://api:8080/webhook`, content type JSON, the
   same secret, events **Pull Request** and **Pull Request Review**.
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

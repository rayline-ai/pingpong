# Setting up a PingPong instance

Standing an instance up: the stack, the accounts, and putting a repository on
it. Working *in* a repository the instance reviews is a different job, done by
different people — see [Using it from a
repository](README.md#using-it-from-a-repository) and `templates/AGENTS.md`.

Everything here is done once per instance, except step 4, which repeats for
every repository you add.

## Starting the stack

You need Docker with Compose, and somewhere for the two agents to think. Both
roles ship pointed at `ollama-local`, so out of the box that is
[ollama](https://ollama.com) running on this host with the model pulled, and no
key anywhere. If you would rather rent the thinking, `rayline/pingpong.json`
carries four more endpoints — Rayline's cloud router, Anthropic and OpenAI
directly, and OpenRouter — and each names the one key it draws on. See
[Models](README.md#models) for what each costs you.

Nothing else touches the host; everything else runs in containers.

```bash
cp .env.sample .env        # no key needed yet
./pingpong up              # builds the images; first run pulls a lot
```

`up` is the only step that works before Forgejo exists — the rest of `.env` needs
accounts and tokens that can only be minted once Forgejo has booted, which is why
this is two passes.

## Ports and address

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

## Accounts, tokens, and the first repository

Self-registration is off (`DISABLE_REGISTRATION` in `docker-compose.yml`), so the
accounts are made with Forgejo's own CLI rather than through the sign-up page.
That is one command, run from this directory:

1. **Create the accounts and the secrets that depend on them.**

   ```bash
   ./pingpong accounts
   ```

   It creates `pingpong-admin`, the two bot accounts `pingpong-reviewer` and
   `pingpong-coder` with a token from each in `.env`, a random
   `PINGPONG_WEBHOOK_SECRET`, and an account for whoever this checkout commits as
   — read off `git config user.email`, because Forgejo links a commit to an
   account by the author's email. It prints each generated password once.

   Every account is looked up before it is created and every `.env` value is
   written only if it is empty, so an interrupted run is repeated rather than
   unpicked, and re-running it changes nothing. Before its first write it keeps
   the old file as `.env.accounts-<timestamp>` — gitignored, and holding whatever
   tokens `.env` held, so treat it as one.

   What it does not do is mint a person a token. A token cannot be revoked from
   the admin side ([Replacing a token you cannot
   revoke](#replacing-a-token-you-cannot-revoke)), so one is minted only where
   something will store it: `onboard` puts this machine's in `~/.netrc`, and
   `--token` below hands one to someone on another machine.

   Three things it arranges are worth knowing, because they are what the loop
   depends on: [why there are two bots](#why-two-bot-accounts), [why a person's
   account is created from their commit address](#one-account-per-person), and
   [the forced password change](#the-forced-password-change) each printed password
   still needs.
2. **Log in once as each account it created**, at whatever you set
   `FORGEJO_ROOT_URL` to — `localhost` only if you left `BIND_ADDR` empty.
   Forgejo asks for a new password, and that is the step nothing can do for you:
   until it is done every API call the account's token makes comes back `403 You
   must change your password`. The bots are exempt and are created that way.

   **Write the printed passwords down before you clear the terminal.** They are
   stored only as a hash, so nothing can show them again. Lost one before its
   first login? Do not recreate the account — that orphans anything already
   attached to it. Issue a fresh password instead:

   ```bash
   docker compose exec -u git forgejo forgejo admin user change-password \
       --username <them> --password '<new one>' --must-change-password
   ```

   Anyone else who will use the instance gets an account the same way. There is
   nothing to derive for them — the host cannot see their git config — so ask
   them for `git config user.email`, and add `--token` if they work on another
   machine and need one to carry to their own `~/.netrc`:

   ```bash
   ./pingpong accounts --user them@example.com --token
   ```

   This is not part of setup; it is what you run months later when someone joins.
3. `./pingpong up` again to pick up the new `.env`, then `./pingpong doctor`. The
   engine reads the tokens and the webhook secret at start, so it needs a restart
   before the next step can check the hook it registers.
4. **Put a repository on the instance**, owned by a person's account. One
   command, run from this directory:

   ```bash
   ./pingpong onboard ../some-repo
   ```

   It creates the repository under the account whose email that folder commits
   with, adds `pingpong-reviewer` and `pingpong-coder` as collaborators with
   **write**, mints the owner a token if `~/.netrc` has none — or replaces one
   the create call refuses for scope — pushes `HEAD` to `main`, sets
   `pingpong.api`, copies `templates/AGENTS.md` in, registers the webhook and
   proves that the webhook's secret still works. Every step checks the instance
   first and reports `already` rather than failing, so a run interrupted halfway
   is repeated rather than unpicked — and running it again on a repository that
   is already set up is a useful check in itself.

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
   - **The hook's secret is checked by using it, every run.** An empty or stale
     one is the worst failure this setup has: Forgejo answers `201`, the hook
     looks correct in the UI, and every delivery afterwards fails its signature
     check. `onboard` refuses to create a hook with an empty secret, and for one
     that already exists it makes Forgejo sign a throwaway delivery and reads the
     engine's verdict — the only way to know, since the secret cannot be read
     back or edited. See [The webhook secret cannot be read
     back](#the-webhook-secret-cannot-be-read-back).
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

   To do the same thing by hand — or to read a hook back and understand its
   event list — see [Onboarding a repository by hand](#onboarding-a-repository-by-hand).
5. Edit the places `AGENTS.md` marks *decide this per repo* in the repository you
   just onboarded. It is what tells whoever works there how to drive the loop;
   without it they have a forge with two bots on it and no way to know what any
   of it means.

Optionally turn on branch protection requiring an approving review — that is what
turns the reviewer's `APPROVED` into an actual merge gate.

## Reference

None of this is a step. It is the handful of Forgejo behaviours that cost a
setup its afternoon, kept out of the steps above so the steps stay short.

### Why two bot accounts

One account per role is what makes the PR page readable: the reviewer posts the
reviews, the coder authors *and pushes* the commits. Forgejo credits an "added N
commits" event to whoever pushed rather than to the commit's author, so a single
shared token shows the reviewer writing the fixes it asked for.

`pingpong-coder` carries the address in `BOT_EMAIL` and `pingpong-reviewer` must
not. Rounds are counted from commits authored with `BOT_EMAIL`, so a reviewer
sharing it would have its own commits counted as rounds — and a coder *not*
carrying it means no commit is ever counted, so `MAX_ROUNDS` never bites and the
loop has no bound. `accounts` checks that pairing on every run, including for an
account it did not create, because it is the one address in the system that is
load-bearing.

Neither bot should author PRs. Forgejo refuses to let an account review its own,
so a PR opened by the reviewer or the coder is silently never reviewed. The
repositories and the PRs belong to people's accounts — not to `pingpong-admin`
either, which is an ops account: it creates the others and administers the
instance, owns no repository and authors nothing, so no repository's fate is tied
to the account that happens to administer the instance. Nothing uses an admin
*token*; it exists for the CLI and the web UI.

### One account per person

Each person's account is created with the email their workstation already commits
with. That address is the whole point: Forgejo links a commit to an account by
the author's email, so an account created with anything else leaves every commit
that person pushes showing as an unlinked author. The PR still works, it just
stops saying who wrote what, which is most of what the loop is for. If they
commit under several addresses, add the rest under Settings → Emails.

The login is cosmetic and the email is not, which is why only the email is
derived. `accounts` takes the local part of the address as the login, which is a
starting point and not a rule — it collides when two people share a local part
across domains, and Forgejo rejects characters that are legal in an address but
not in a username. `--login` overrides it.

The printed password is for the web UI. Each person also needs an **access
token**: `templates/AGENTS.md` requires one in `~/.netrc` for `git push` and
every API call it makes, and is firm that it must not be the account password.
It has to be minted *after* their first login, or it meets the same `403` below.
On this machine `onboard` does that when it needs one; for anyone else it is
`accounts --user … --token`, which mints exactly these scopes:

```
write:repository,write:user,read:user
```

Three scopes, one token, minted once — see [Token scopes](#token-scopes) for why
those three and what `write:user` costs. Getting them right at that moment
matters, because a token cannot be widened afterwards and cannot be revoked from
here either: [Replacing a token you cannot
revoke](#replacing-a-token-you-cannot-revoke).

### The forced password change

A CLI-created account is required to change its password at first login, and
Forgejo enforces that on the **API** too: until it is done, every call the
account's token makes comes back `403 You must change your password`. It reads as
a permissions problem and is not one.

For the two bot accounts, nothing ever logs in, so nothing would ever clear it —
hence `--must-change-password=false` at creation, which is the whole fix and
costs nothing.

For a person's account, keep the forced change: it is what turns the printed
password into a one-shot credential. It also means their token has to be minted
*after* their first login, or it hits the same `403`.

### Token scopes

Forgejo's scopes are per-route and ANDed, and `write:X` does not imply `read:X`
across namespaces. The three on a person's token:

| scope | what needs it |
| --- | --- |
| `write:repository` | `git push`, and `POST` of a pull request |
| `read:user` | the identity checks in `templates/AGENTS.md` — `/api/v1/user`, `/api/v1/user/emails` |
| `write:user` | one call only: creating a repository, in step 4 |

`write:user` is on this token rather than on a second one because step 4 repeats
for every repository you ever add, and a token minted per repository is a token
you cannot revoke per repository. Better one credential you know the whereabouts
of than a growing set of forgotten ones.

Know what it widens: `write:user` also rewrites the account's email addresses and
SSH keys, and the token lives in plaintext in `~/.netrc`, so whatever reaches
that workstation reaches those too. Creating repositories in Forgejo's UI needs
no token at all — if that is the trade you want, drop `write:user` and onboard by
hand.

The bots' `write:repository,write:issue` is narrower because the engine only
reads PRs and diffs, posts reviews and commit statuses, and comments.

**Do not infer a scope from the endpoint's path.** Each endpoint declares its
own, and neither direction of the obvious guess holds: creating a repository is
`POST /api/v1/user/repos` and needs `write:user` *and* `write:repository`, while
opening an issue is `POST /api/v1/repos/{owner}/{repo}/issues` and needs
`write:issue` despite living under `repos` — which is why the bots carry
`write:issue` at all. When a call is refused, the `403` names the scope it
wanted; read it rather than widening the token by guesswork.

### Replacing a token you cannot revoke

Scopes cannot be widened after the fact, and you cannot clean up from the admin
side either: `DELETE /users/{username}/tokens/{id}` refuses token auth and
answers `401 auth method not allowed`. Revoking needs basic auth with the account
password, so only the person themselves can do it, in Settings → Applications.

So an account minted before this list changed — or one refused for a scope you
now know it needs — gets a **replacement**: run `generate-access-token` again,
put the new token in `~/.netrc`, and tell the owner to delete the stale one.
`./pingpong onboard` does exactly this on its own when the create call comes back
`403` naming `write:user`.

One token superseding another is a cleanup with a known end. One token per
repository is not — that is the accumulation worth avoiding.

### Onboarding a repository by hand

What step 4 automates: create the repository (with `auto_init` **false**), add
`pingpong-reviewer` and `pingpong-coder` under Settings → Collaborators with
**write**, push the folder's `HEAD` to `main`, and add a webhook pointing at
`http://api:8080/webhook`, content type JSON, `PINGPONG_WEBHOOK_SECRET` as the
secret, events **Pull Request**, **Pull Request Review** and **Issue Comment** —
the last is what `@pingpong` needs.

That is `8080`, not `API_PORT`, and it is not a typo. Forgejo calls the engine
from inside the compose network, where the service still listens on its own port;
the published one exists only for you.

Read the hook back afterwards and its event list is longer than the three you
checked: Forgejo stores `pull_request_review` expanded into its `_approved`,
`_rejected` and `_comment` variants, and `pull_request` into `_sync`, `_assign`
and the rest. That expansion is why those are the event names that actually
arrive — and it means Forgejo does not send one `pull_request_review` event with
the state in the body the way GitHub does. The state is in the event name and the
action is `reviewed`:

```
X-Forgejo-Event: pull_request_rejected      action: reviewed
```

Both spellings are accepted. `pingpong logs` names the event of every delivery it
ignores, and why — a trigger that silently does not fire looks exactly like a
webhook that never arrived.

### The webhook secret cannot be read back

Three Forgejo behaviours meet here, and each one on its own is enough to send you
looking for a fault that is not there.

**The API never returns it.** A hook's `config` carries `url` and `content_type`
and no `secret` key at all — not an empty one, an absent one. Anything printing
`secret: EMPTY` from that response is reporting a missing key, *not* the
empty-secret failure above.

**A `PATCH` carrying a new secret answers `200` and changes nothing.** The stored
value is untouched. So a secret cannot be corrected in place: a hook whose secret
is wrong has to be deleted and created again.

**The test-delivery endpoint sends a `push`.** `POST /hooks/{id}/tests` on a hook
that does not list `push` is filtered before delivery — `204`, nothing at the
engine, and no information either way.

Which leaves one honest question: make Forgejo sign something and see whether the
engine accepts it. `push` is the right event to use, because the engine verifies
the signature *before* deciding it does not handle pushes, so nothing runs and no
round is spent. Add `push` to the hook's events, fire a test delivery, read
`pingpong logs`, then take `push` back off:

```
ignored delivery: event='push' action='': event 'push' is not handled   → secret agrees
rejected delivery: bad signature                                        → it does not
```

`./pingpong onboard` does exactly this on every run, and replaces the hook when
the answer is the second one. The case that makes it worth doing is editing
`PINGPONG_WEBHOOK_SECRET` in `.env` after a repository was onboarded: Forgejo
keeps the old value, the engine picks up the new one at its next start, and
nothing anywhere reports a problem.

To read the stored value rather than test it, go to the database. The quoting
matters on Windows — a bare `/data/...` argument is rewritten to
`C:/Program Files/Git/data/...` by MSYS before Docker sees it, so the path has to
be inside `sh -c`:

```bash
docker compose exec -T forgejo sh -c \
    'sqlite3 /data/gitea/forgejo.db "select id, url, length(secret) from webhook;"'
```

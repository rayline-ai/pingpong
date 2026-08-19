# Setting up a PingPong instance

Standing an instance up: the stack, the accounts, and putting a repository on
it. Working *in* a repository the instance reviews is a different job, done by
different people — see [Using it from a
repository](README.md#using-it-from-a-repository) and `templates/AGENTS.md`.

Everything here is done once per instance, except step 4, which repeats for
every repository you add.

## Starting the stack

You need Docker with Compose, and a Rayline router key (`rlk-…`) from
[platform.rayline.ai/keys](https://platform.rayline.ai/keys). Everything else runs
in containers. Only if you want a role on a local model do you also need ollama on
the host — see [Running a role on a local
model](README.md#running-a-role-on-a-local-model).

```bash
cp .env.sample .env        # fill in RAYLINE_ROUTER_API_KEY
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

   `--must-change-password=false` is what makes the token work, and it belongs on
   the bots only — see [The forced password change](#the-forced-password-change).

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
   rejects the token's calls with the same `403` ([The forced password
   change](#the-forced-password-change)):

   ```bash
   docker compose exec -u git forgejo forgejo admin user generate-access-token \
       --username <them> --token-name workstation --raw \
       --scopes write:repository,write:user,read:user
   ```

   Three scopes, one token, minted once — see [Token scopes](#token-scopes) for
   why those three and what `write:user` costs. Getting them right at this moment
   matters, because you cannot widen a token afterwards and you cannot revoke one
   either: [Replacing a token you cannot
   revoke](#replacing-a-token-you-cannot-revoke).

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

   To do the same thing by hand — or to read a hook back and understand its
   event list — see [Onboarding a repository by hand](#onboarding-a-repository-by-hand).
5. Edit the places `AGENTS.md` marks *decide this per repo* in the repository you
   just onboarded. It is what tells whoever works there how to drive the loop;
   without it they have a forge with two bots on it and no way to know what any
   of it means.
6. `./pingpong up` again to pick up the new `.env`, then `./pingpong doctor`.

Optionally turn on branch protection requiring an approving review — that is what
turns the reviewer's `APPROVED` into an actual merge gate.

## Reference

None of this is a step. It is the handful of Forgejo behaviours that cost a
setup its afternoon, kept out of the steps above so the steps stay short.

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

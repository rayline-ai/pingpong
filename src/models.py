"""`pingpong model` — point a role at an endpoint and a model.

The routing itself lives in rayline/pingpong.json, which Rayline reads and this
project does not otherwise parse. That file is editable by hand and says so; this
is the same edit with the three things a hand-edit gets wrong done for you: the
endpoint has to exist, the credential it names has to be in `.env`, and
`routes.main`/`routes.subagent` have to stay sensible when both roles move.

Runs on the host rather than in the API container: it writes `.env` and the
mounted config, neither of which that container can reach, and it has to work
before the stack is up.
"""
import datetime
import json
import os
import sys
import urllib.request

ROLES = {"reviewer": "reviewer-brain", "coder": "coder-brain"}

# Inside an agent every credential the ROUTER uses is RAYLINE_-prefixed, and
# `.env` uses the conventional name for each provider. docker-compose.yml is the
# real mapping — this is the same table, and the agent entrypoint carries it a
# third time as a sed. Add an endpoint whose key is not here and it still works;
# the operator is just told to set the prefixed name verbatim.
DOTENV_NAMES = {
    "RAYLINE_ROUTER_API_KEY": "RAYLINE_ROUTER_API_KEY",
    "RAYLINE_ANTHROPIC_API_KEY": "ANTHROPIC_API_KEY",
    "RAYLINE_OPENAI_API_KEY": "OPENAI_API_KEY",
    "RAYLINE_OPENROUTER_API_KEY": "OPENROUTER_API_KEY",
}

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "rayline", "pingpong.json")
ENV_PATH = os.path.join(ROOT, ".env")


class ModelError(Exception):
    pass


# ---------------------------------------------------------------------------
# The routing config
# ---------------------------------------------------------------------------

# The paths are read through the module rather than bound as defaults so a test
# can point the whole command at a scratch copy.
def load(path=None):
    path = path or CONFIG_PATH
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise ModelError("%s is missing" % path)
    except ValueError as exc:
        raise ModelError("%s is not valid JSON: %s" % (path, exc))


def save(cfg, path=None):
    """Rewrite the whole file. Safe because every key here is data — including
    `_comment`, which is a JSON array precisely so it survives this."""
    path = path or CONFIG_PATH
    text = json.dumps(cfg, indent=2, ensure_ascii=False) + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def endpoints(cfg):
    return cfg.get("endpoints") or []


# The order the provider question offers them in, and it is not the config's.
# The two that ship first are the two this repo is built around — a model on
# your own machine, then Rayline's router — and the three direct providers come
# after because choosing one of them is choosing to bill yourself per token.
# Anything not named here keeps its position from the config, at the end.
PROVIDER_ORDER = ("ollama-local", "rayline-cloud")


def ordered_endpoints(cfg):
    """The endpoints as the menu shows them. Sorted here rather than left to the
    order of the array in the file, so adding or moving an endpoint in that file
    cannot quietly reshuffle the question."""
    listed = endpoints(cfg)
    def rank(pair):
        index, spec = pair
        try:
            return (PROVIDER_ORDER.index(spec.get("id")), 0)
        except ValueError:
            return (len(PROVIDER_ORDER), index)
    return [spec for _, spec in sorted(enumerate(listed), key=rank)]


def endpoint(cfg, endpoint_id):
    for item in endpoints(cfg):
        if item.get("id") == endpoint_id:
            return item
    raise ModelError("no endpoint %r in the config. There is %s."
                     % (endpoint_id, ", ".join(e.get("id", "?") for e in endpoints(cfg))))


def route(cfg, role):
    """What a role resolves to now, as (endpoint_id, model). Either may be None:
    a config can name an alias that no endpoint backs, and that is worth showing
    rather than hiding behind a default."""
    alias = ROLES[role]
    entry = cfg.get("routes", {}).get("model_routes", {}).get(alias) or {}
    return entry.get("endpoint"), entry.get("model")


def unset_roles(cfg):
    """The roles that have not been pointed at anything. Ships as both of them,
    deliberately: a shipped default is a decision taken on someone's behalf, and
    the one thing this repo cannot know is which models are on their host."""
    return [role for role in ("reviewer", "coder") if not route(cfg, role)[0]]


def set_route(cfg, role, endpoint_id, model):
    """Point one role at one model, and move the shared routes with it.

    `routes.main` and `routes.subagent` are per-config, not per-role, and there
    is one config for both containers — so they can only follow when the two
    roles agree. Leaving them behind on a stale endpoint is the failure this
    avoids: they are the fallback for any request naming neither alias.
    """
    if role not in ROLES:
        raise ModelError("no role %r — there is reviewer and coder" % role)
    spec = endpoint(cfg, endpoint_id)
    routes = cfg.setdefault("routes", {})
    routes.setdefault("model_routes", {})

    entry = dict(cfg["routes"]["model_routes"].get(ROLES[role]) or {})
    entry["endpoint"] = endpoint_id
    entry["model"] = model
    # `router` names the local rld instance and is not ours to invent; keep
    # whatever the file already used, and fall back to the other role's.
    if "router" not in entry:
        other = [r for r in ROLES if r != role][0]
        entry["router"] = (cfg["routes"]["model_routes"].get(ROLES[other]) or {}).get(
            "router", "rayline-local")
    cfg["routes"]["model_routes"][ROLES[role]] = entry

    both = [route(cfg, r) for r in ROLES]
    if both[0] == both[1]:
        for shared in ("main", "subagent"):
            current = dict(cfg["routes"].get(shared) or {})
            current["endpoint"] = endpoint_id
            current["model"] = model
            current.setdefault("router", entry["router"])
            cfg["routes"][shared] = current
    return spec


def needed_key(cfg, role):
    """The `.env` variable this role's endpoint draws on, or None if it needs
    none. Keyless means local, which needs something else entirely — the agent
    entrypoint probes for that at startup."""
    endpoint_id, _ = route(cfg, role)
    if not endpoint_id:
        return None
    key_env = endpoint(cfg, endpoint_id).get("api_key_env")
    if not key_env:
        return None
    return DOTENV_NAMES.get(key_env, key_env)


def describe(spec):
    """One line for the endpoint menu: what it is and what it will cost you."""
    key_env = spec.get("api_key_env")
    if not key_env:
        return "no key — a model on this host"
    return "needs %s" % DOTENV_NAMES.get(key_env, key_env)


def host_url(base_url):
    """The endpoint's address as reachable from *here*. base_url is written for
    the agent containers, where the host is `host.docker.internal`; that name
    does not resolve on the host itself."""
    return (base_url or "").replace("host.docker.internal", "127.0.0.1")


def local_models(spec, timeout=3):
    """What a local ollama actually has, or None if it cannot be asked.

    The menu otherwise lists tags from the config, which say what this project
    suggests and nothing about what is on the machine — and the -32k ones are not
    pullable at all, they are tags someone has to create. Better to say so while
    the choice is being made than to let the agent discover it at startup.

    None is not an empty list: "ollama is not running" and "ollama has nothing"
    are different answers, and neither is a reason to refuse the choice."""
    if spec.get("api_key_env"):
        return None
    url = host_url(spec.get("base_url")).rstrip("/") + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except Exception:
        # Unreachable, not ollama, not JSON — all the same answer here, and none
        # of them is this command's business to diagnose.
        return None
    if not isinstance(body, dict) or not isinstance(body.get("models"), list):
        return None
    names = set()
    for item in body["models"]:
        for field in ("name", "model"):
            if isinstance(item, dict) and isinstance(item.get(field), str):
                names.add(item[field])
    return names


def pinned_window(spec, name, timeout=3):
    """The `num_ctx` baked into a tag, or None if it has none.

    Presence is not the useful question about a local model — the window is. A
    stock tag is on the disk and still wrong for this, unless the whole ollama
    server has been raised with OLLAMA_CONTEXT_LENGTH, which is why this reports
    what it found and does not refuse anything.

    /api/show is metadata only: it does not load the model."""
    url = host_url(spec.get("base_url")).rstrip("/") + "/api/show"
    body = json.dumps({"model": name}).encode("utf-8")
    request = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            shown = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    for line in (shown.get("parameters") or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "num_ctx" and parts[1].isdigit():
            return int(parts[1])
    return None


def pull_recipe(model, out):
    """What to run to get a model the host does not have. A `-32k` tag has no
    upstream to pull from: the suffix means someone pinned the context window,
    which is the whole reason it is named that."""
    base = model.rsplit("-32k", 1)[0] if model.endswith("-32k") else None
    if base:
        out("    ollama pull %s" % base)
        out("    printf 'FROM %s\\nPARAMETER num_ctx 32768\\n' > Modelfile" % base)
        out("    ollama create %s -f Modelfile" % model)
        out("  the pinned window is what keeps Hermes' 19 tool definitions in")
        out("  the prompt; at the stock 4096 they are truncated out of it.")
    else:
        out("    ollama pull %s" % model)


# ---------------------------------------------------------------------------
# .env
# ---------------------------------------------------------------------------
# Same contract as accounts.sh's env_set: rewrite one assignment and keep every
# other line byte-for-byte, because most of that file is prose the operator may
# have edited. One backup per run, not per value — .env holds tokens.

def env_value(key, path=None):
    path = path or ENV_PATH
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip()
    except FileNotFoundError:
        return None
    return None


def env_set(key, value, path=None, backup=None, stamp=None):
    path = path or ENV_PATH
    if not os.path.exists(path):
        raise ModelError("%s is missing — cp .env.sample .env first" % path)
    if backup is None:
        stamp = stamp or datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        backup = path + ".model-" + stamp
    if not os.path.exists(backup):
        with open(path, encoding="utf-8") as src:
            body = src.read()
        with open(backup, "w", encoding="utf-8", newline="") as dst:
            dst.write(body)

    with open(path, encoding="utf-8") as handle:
        lines = handle.readlines()
    out, done = [], False
    for line in lines:
        if not done and line.startswith(key + "="):
            out.append("%s=%s\n" % (key, value))
            done = True
        else:
            out.append(line)
    if not done:
        if out and not out[-1].endswith("\n"):
            out.append("\n")
        out.append("%s=%s\n" % (key, value))
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.writelines(out)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return backup


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _show(cfg, out):
    out("")
    for role in ("reviewer", "coder"):
        endpoint_id, model = route(cfg, role)
        if not endpoint_id:
            out("  %-9s not chosen yet" % role)
            continue
        key = needed_key(cfg, role)
        if key:
            state = "%s is %s" % (key, "set" if env_value(key) else "EMPTY")
        else:
            state = "no key needed"
        out("  %-9s %s / %s   (%s)" % (role, endpoint_id, model, state))
    if unset_roles(cfg):
        out("")
        out("  Run `./pingpong model` to choose. Nothing ships chosen, so there")
        out("  is no default to weigh up and `up` will not start without this.")


def _choose(items, prompt, ask, out, default=1, footer=()):
    """Numbered menu. Returns the chosen item, or None for the free-text option
    when one was offered (the caller then asks for the value itself)."""
    for i, (label, note) in enumerate(items, 1):
        out(("  %d) %-16s %s" % (i, label, note)).rstrip())
    for line in footer:
        out(line)
    while True:
        raw = ask("  %s [%d]: " % (prompt, default)).strip()
        if not raw:
            raw = str(default)
        if raw.isdigit() and 1 <= int(raw) <= len(items):
            return int(raw) - 1
        out("  not one of 1-%d" % len(items))


def _ask_key(spec, ask, out, backup):
    """The credential this provider draws on, asked as part of choosing it.

    Which provider you are on decides whether there is a key at all, so this
    belongs after that answer and not in a pass of its own: nobody staying on
    ollama should be asked about an OpenAI key to say no to."""
    key_env = spec.get("api_key_env")
    if not key_env:
        return backup
    name = DOTENV_NAMES.get(key_env, key_env)
    if env_value(name):
        out("  %s is already in .env" % name)
        return backup
    out("  %s needs %s, and .env does not have it." % (spec.get("id"), name))
    value = ask("  paste it (blank to skip): ").strip()
    if not value:
        out("  skipped — the agent will refuse to start until it is in .env.")
        return backup
    backup = env_set(name, value, backup=backup)
    out("  written to .env")
    return backup


WANTED_WINDOW = 32768


def _model_menu(spec, listed, present, window=None):
    """The model question, which is a different question per provider.

    For a hosted one the config's list is all there is to go on. For a local one
    the honest menu is what the machine actually has, and the honest note is the
    context window rather than the presence — a tag on the disk with a stock
    4096 window is the failure this project keeps warning about.

    Returns (items, skipped). Tags the config never suggested are offered too,
    but only where the window is pinned: a machine with a dozen models pulled
    would otherwise bury the four that work in a list of ones that do not. The
    skipped ones come back to be named in a line, not buried silently."""
    if present is None:
        return [(m, "") for m in listed], []
    window = window or (lambda name: None)

    def note(name):
        got = window(name)
        if got is None:
            return "no pinned window"
        if got < WANTED_WINDOW:
            return "window pinned at %d" % got
        return ""

    items = [(m, note(m)) for m in listed if m in present]
    skipped = []
    for name in sorted(present):
        if name in listed:
            continue
        if note(name):
            skipped.append(name)
        else:
            items.append((name, "pulled here, not in the config"))
    items += [(m, "not on this host") for m in listed if m not in present]
    return items, skipped


def _pick_role(cfg, role, ask, out, backup=None):
    endpoint_id, model = route(cfg, role)
    out("")
    if endpoint_id:
        out("== %s (currently %s / %s)" % (role, endpoint_id, model))
    else:
        out("== %s (not chosen yet)" % role)
    specs = ordered_endpoints(cfg)
    default = 1
    for i, spec in enumerate(specs, 1):
        if spec.get("id") == endpoint_id:
            default = i
    index = _choose([(s.get("id", "?"), describe(s)) for s in specs],
                    "provider", ask, out, default)
    spec = specs[index]
    out("")

    # Everything from here follows from that answer: a hosted provider needs a
    # key and offers a catalog, a local one needs neither and offers what is on
    # the disk. Asking both of everyone is how a two-question command became a
    # gauntlet.
    backup = _ask_key(spec, ask, out, backup)

    listed = list(spec.get("models") or [])
    present = local_models(spec)
    windows = {}
    items, skipped = _model_menu(
        spec, listed, present,
        lambda name: windows.setdefault(name, pinned_window(spec, name)))
    names = [name for name, _ in items]
    default = 1
    for i, name in enumerate(names, 1):
        if name == model and spec.get("id") == endpoint_id:
            default = i
    footer = []
    if skipped:
        # Named, not silently dropped. They are legitimate answers on a server
        # whose OLLAMA_CONTEXT_LENGTH has been raised — just not ones to put in
        # front of someone by default.
        footer = ["  %d more here have no pinned window: %s."
                  % (len(skipped), ", ".join(skipped)),
                  "  Type one at `other` if you raised OLLAMA_CONTEXT_LENGTH."]
    index = _choose(items + [("other", "type an id")], "model", ask, out, default,
                    footer=footer)
    if index == len(names):
        chosen = ask("  model id: ").strip()
        if not chosen:
            raise ModelError("no model given")
        out("  note: %r is not in this endpoint's list in the config. That list"
            % chosen)
        out("        is a menu, not a limit, so this is written as given.")
    else:
        chosen = names[index]
    if present is not None and chosen not in present:
        out("")
        out("  this host does not have %r yet. The agent checks that at" % chosen)
        out("  startup and refuses to run without it, so get it before `up`:")
        pull_recipe(chosen, out)
    return spec.get("id"), chosen, backup


def _ask_keys(cfg, ask, out, backup=None):
    """Ask for whatever the two roles now need and do not have. Asking here and
    not at `up` is the point: the choice and its cost are one step."""
    wanted = []
    for role in ("reviewer", "coder"):
        key = needed_key(cfg, role)
        if key and not env_value(key) and key not in wanted:
            wanted.append(key)
    for key in wanted:
        who = [r for r in ("reviewer", "coder") if needed_key(cfg, r) == key]
        out("")
        out("== %s" % key)
        out("  the %s needs it, and .env does not have it." % " and the ".join(who))
        value = ask("  paste it (blank to skip): ").strip()
        if not value:
            out("  skipped — the agent will refuse to start until it is in .env.")
            continue
        backup = env_set(key, value, backup=backup)
        out("  written to .env")
    return backup


def _force_utf8():
    """Windows consoles default to cp1252, and every line here is prose."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv=None, ask=None, out=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if out is None:
        _force_utf8()
    out = out or (lambda line: print(line, flush=True))
    ask = ask or input

    if argv and argv[0] in ("-h", "--help"):
        out(__doc__.strip().splitlines()[0])
        out("")
        out("usage: pingpong model                      choose interactively")
        out("       pingpong model <role> <endpoint> <model>")
        out("       pingpong model --show")
        out("       pingpong model --check         exit 1 if a role is unchosen")
        out("")
        out("  role      reviewer | coder")
        out("  endpoint  an id from rayline/pingpong.json")
        return 0

    cfg = load()

    if argv and argv[0] == "--show":
        _show(cfg, out)
        return 0

    if argv and argv[0] == "--check":
        # What `up` gates on. Quiet when there is nothing to say, so it can run
        # on every start without becoming noise.
        missing = unset_roles(cfg)
        if not missing:
            return 0
        out("No brain chosen for the %s." % " or the ".join(missing))
        out("")
        out("Nothing ships chosen — there is no default model here, because the")
        out("one thing this repo cannot know is which models you have. Choose:")
        out("")
        out("    ./pingpong model")
        out("")
        out("It asks for a provider first, then only what that provider needs.")
        return 1

    backup = None
    if argv:
        if len(argv) != 3:
            raise ModelError("expected: model <role> <endpoint> <model>, got %d "
                             "argument(s)" % len(argv))
        role, endpoint_id, model = argv
        if role not in ROLES:
            raise ModelError("no role %r — there is reviewer and coder" % role)
        spec = set_route(cfg, role, endpoint_id, model)
        if model not in (spec.get("models") or []):
            out("note: %r is not in %s's list in the config, which is a menu and "
                "not a limit." % (model, endpoint_id))
        save(cfg)
        out("%s -> %s / %s" % (role, endpoint_id, model))
        # Only here. The interactive path asks for the key as part of choosing
        # the provider that needs it, which is the whole point of asking there.
        backup = _ask_keys(cfg, ask, out, backup)
    else:
        out("Both agents' brains. One config for two containers, so this is")
        out("instance-wide: every repository the instance reviews gets it.")
        _show(cfg, out)
        picks = {}
        for role in ("reviewer", "coder"):
            endpoint_id, model, backup = _pick_role(cfg, role, ask, out, backup)
            picks[role] = (endpoint_id, model)
        for role, (endpoint_id, model) in picks.items():
            set_route(cfg, role, endpoint_id, model)
        save(cfg)
        out("")
        out("== written to rayline/pingpong.json")
        _show(cfg, out)

    if backup:
        out("")
        out("original .env kept at %s" % os.path.basename(backup))

    out("")
    out("rld reads that config once, when it starts, so the running agents are")
    out("still on the old one:")
    out("    docker compose up -d reviewer coder")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ModelError as exc:
        sys.exit("model: %s" % exc)
    except (KeyboardInterrupt, EOFError):
        # Not "nothing written": the routes are saved before the keys are asked
        # for, so a stop here can leave a real change on disk. Say which.
        sys.exit("\nmodel: stopped. Whatever it reported above, it had already "
                 "written; run it again for the rest.")

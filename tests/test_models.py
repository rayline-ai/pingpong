"""Tests for `pingpong model`: routing edits, the .env rewrite, and the shipped
config staying coherent.

Everything here runs without Docker, Rayline or a model.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import models  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CONFIG = {
    "_comment": ["prose the operator may have edited", "second line"],
    "endpoints": [
        {"id": "ollama-local", "protocol": "anthropic_messages",
         "base_url": "http://host.docker.internal:11434",
         "models": ["qwen3.5:9b-32k", "qwen2.5-coder:7b"]},
        {"id": "openai-direct", "protocol": "openai_chat",
         "base_url": "https://api.openai.com",
         "api_key_env": "RAYLINE_OPENAI_API_KEY", "auth": "bearer",
         "models": ["gpt-5.6", "gpt-5.6-mini"]},
        {"id": "rayline-cloud", "protocol": "anthropic_messages",
         "base_url": "https://api.rayline.ai",
         "api_key_env": "RAYLINE_ROUTER_API_KEY", "auth": "api_key",
         "models": ["rayline-router"]},
    ],
    "routes": {
        "main": {"endpoint": "ollama-local", "model": "qwen3.5:9b-32k",
                 "router": "rayline-local"},
        "subagent": {"endpoint": "ollama-local", "model": "qwen3.5:9b-32k",
                     "router": "rayline-local"},
        "model_routes": {
            "reviewer-brain": {"endpoint": "ollama-local", "model": "qwen3.5:9b-32k",
                               "router": "rayline-local"},
            "coder-brain": {"endpoint": "ollama-local", "model": "qwen3.5:9b-32k",
                            "router": "rayline-local"},
        },
    },
}

ENV = """\
# A comment the operator wrote.
RAYLINE_ROUTER_API_KEY=

# Another comment.
OPENAI_API_KEY=
MAX_ROUNDS=3
"""


class Sandbox(unittest.TestCase):
    """Points the module at a scratch config and .env, so nothing here can touch
    the operator's real ones."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.config_path = os.path.join(self.dir, "pingpong.json")
        self.env_path = os.path.join(self.dir, ".env")
        with io.open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(CONFIG, handle, indent=2)
        with io.open(self.env_path, "w", encoding="utf-8", newline="") as handle:
            handle.write(ENV)

        for name, value in (("CONFIG_PATH", self.config_path),
                            ("ENV_PATH", self.env_path)):
            original = getattr(models, name)
            setattr(models, name, value)
            self.addCleanup(setattr, models, name, original)

        # The fixture's local endpoint is 127.0.0.1:11434 once rewritten for the
        # host, which on a developer's machine is a *live ollama* — its tags
        # would land in the menu and move every index these tests count on.
        # Unknown is the default; `self.host_has(...)` opts a test back in.
        self.host_has(None)

    def host_has(self, tags, windows=None):
        """What the local endpoint reports having: a set, or None for "could not
        be asked", which is what an absent ollama looks like. `windows` maps a
        tag to its pinned num_ctx; anything unlisted is treated as pinned, so a
        test only says so when the window is the point."""
        windows = windows or {}
        for name, stub in (
                ("local_models",
                 lambda spec, timeout=3: None if spec.get("api_key_env") else tags),
                ("pinned_window",
                 lambda spec, tag, timeout=3: windows.get(tag, models.WANTED_WINDOW))):
            original = getattr(models, name)
            setattr(models, name, stub)
            self.addCleanup(setattr, models, name, original)

    def written(self):
        return models.load(self.config_path)

    def env_text(self):
        with io.open(self.env_path, encoding="utf-8") as handle:
            return handle.read()

    def run_cli(self, argv, answers=()):
        """Drive main() with scripted answers and collect what it printed."""
        replies = iter(answers)
        lines = []

        def ask(prompt):
            try:
                return next(replies)
            except StopIteration:
                raise AssertionError("main() asked more than expected: %r" % prompt)

        code = models.main(list(argv), ask=ask, out=lines.append)
        return code, "\n".join(lines)


class TestRoutes(Sandbox):
    def test_one_role_moves_alone(self):
        cfg = self.written()
        models.set_route(cfg, "coder", "openai-direct", "gpt-5.6")
        self.assertEqual(models.route(cfg, "coder"), ("openai-direct", "gpt-5.6"))
        self.assertEqual(models.route(cfg, "reviewer"),
                         ("ollama-local", "qwen3.5:9b-32k"))

    def test_shared_routes_stay_put_while_the_roles_disagree(self):
        # main/subagent are per-config and there is one config for two
        # containers, so there is no per-role answer to give here.
        cfg = self.written()
        models.set_route(cfg, "coder", "openai-direct", "gpt-5.6")
        self.assertEqual(cfg["routes"]["main"]["endpoint"], "ollama-local")
        self.assertEqual(cfg["routes"]["subagent"]["endpoint"], "ollama-local")

    def test_shared_routes_follow_once_both_agree(self):
        cfg = self.written()
        models.set_route(cfg, "coder", "openai-direct", "gpt-5.6")
        models.set_route(cfg, "reviewer", "openai-direct", "gpt-5.6")
        for shared in ("main", "subagent"):
            self.assertEqual(cfg["routes"][shared],
                             {"endpoint": "openai-direct", "model": "gpt-5.6",
                              "router": "rayline-local"})

    def test_router_is_kept_not_invented(self):
        cfg = self.written()
        cfg["routes"]["model_routes"]["coder-brain"]["router"] = "somewhere-else"
        models.set_route(cfg, "coder", "openai-direct", "gpt-5.6")
        self.assertEqual(cfg["routes"]["model_routes"]["coder-brain"]["router"],
                         "somewhere-else")

    def test_unknown_endpoint_names_the_ones_that_exist(self):
        cfg = self.written()
        with self.assertRaises(models.ModelError) as caught:
            models.set_route(cfg, "coder", "openai-dierct", "gpt-5.6")
        self.assertIn("openai-direct", str(caught.exception))

    def test_unknown_role(self):
        cfg = self.written()
        with self.assertRaises(models.ModelError):
            models.set_route(cfg, "referee", "openai-direct", "gpt-5.6")

    def test_needed_key_is_the_dotenv_name_not_the_container_one(self):
        # The agent sees RAYLINE_OPENAI_API_KEY; naming that would send the
        # operator looking for something that is not in .env.
        cfg = self.written()
        models.set_route(cfg, "coder", "openai-direct", "gpt-5.6")
        self.assertEqual(models.needed_key(cfg, "coder"), "OPENAI_API_KEY")

    def test_keyless_endpoint_needs_no_key(self):
        self.assertIsNone(models.needed_key(self.written(), "coder"))

    def test_save_keeps_everything_it_did_not_touch(self):
        cfg = self.written()
        models.set_route(cfg, "coder", "openai-direct", "gpt-5.6")
        models.save(cfg, self.config_path)
        again = self.written()
        self.assertEqual(again["_comment"], CONFIG["_comment"])
        self.assertEqual(again["endpoints"], CONFIG["endpoints"])


class TestEnvSet(Sandbox):
    def test_replaces_in_place_and_keeps_the_prose(self):
        models.env_set("OPENAI_API_KEY", "sk-test", path=self.env_path,
                       backup=os.path.join(self.dir, "backup"))
        text = self.env_text()
        self.assertIn("OPENAI_API_KEY=sk-test\n", text)
        self.assertIn("# A comment the operator wrote.", text)
        self.assertIn("# Another comment.", text)
        self.assertIn("MAX_ROUNDS=3", text)

    def test_appends_a_key_that_is_not_there(self):
        models.env_set("ANTHROPIC_API_KEY", "sk-ant", path=self.env_path,
                       backup=os.path.join(self.dir, "backup"))
        self.assertIn("ANTHROPIC_API_KEY=sk-ant\n", self.env_text())

    def test_one_backup_per_run_not_per_value(self):
        # .env holds tokens: three writes must not leave three copies of it.
        backup = models.env_set("OPENAI_API_KEY", "one", path=self.env_path,
                                stamp="20260818000000")
        second = models.env_set("MAX_ROUNDS", "9", path=self.env_path, backup=backup)
        self.assertEqual(backup, second)
        with io.open(backup, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), ENV)

    def test_env_value_reads_the_file(self):
        self.assertIsNone(models.env_value("OPENAI_API_KEY", path=self.env_path)
                          or None)
        models.env_set("OPENAI_API_KEY", "sk-test", path=self.env_path,
                       backup=os.path.join(self.dir, "backup"))
        self.assertEqual(models.env_value("OPENAI_API_KEY", path=self.env_path),
                         "sk-test")

    def test_missing_env_is_an_error_not_a_new_file(self):
        os.remove(self.env_path)
        with self.assertRaises(models.ModelError):
            models.env_set("OPENAI_API_KEY", "x", path=self.env_path)


class TestCheck(Sandbox):
    """`up` runs this before compose. It is the whole reason a blank config is
    safe to ship: the refusal happens at the step that can fix it."""

    def blank(self):
        cfg = self.written()
        for entry in list(cfg["routes"]["model_routes"].values()) + [
                cfg["routes"]["main"], cfg["routes"]["subagent"]]:
            entry.pop("endpoint", None)
            entry.pop("model", None)
        models.save(cfg, self.config_path)

    def test_it_is_silent_when_both_roles_are_chosen(self):
        code, output = self.run_cli(["--check"])
        self.assertEqual(code, 0)
        self.assertEqual(output, "")

    def test_it_refuses_and_names_the_command_that_fixes_it(self):
        self.blank()
        code, output = self.run_cli(["--check"])
        self.assertEqual(code, 1)
        self.assertIn("reviewer or the coder", output)
        self.assertIn("./pingpong model", output)

    def test_one_role_short_is_still_a_refusal(self):
        # Half-configured is not configured: the other agent still has no brain.
        cfg = self.written()
        cfg["routes"]["model_routes"]["coder-brain"].pop("endpoint")
        models.save(cfg, self.config_path)
        code, output = self.run_cli(["--check"])
        self.assertEqual(code, 1)
        self.assertIn("No brain chosen for the coder.", output)

    def test_it_writes_nothing(self):
        self.blank()
        before = self.env_text(), self.written()
        self.run_cli(["--check"])
        self.assertEqual((self.env_text(), self.written()), before)


class TestCLI(Sandbox):
    def test_show_points_at_the_command_when_nothing_is_chosen(self):
        cfg = self.written()
        for entry in cfg["routes"]["model_routes"].values():
            entry.pop("endpoint")
        models.save(cfg, self.config_path)
        code, output = self.run_cli(["--show"])
        self.assertEqual(code, 0)
        self.assertIn("not chosen yet", output)
        self.assertIn("./pingpong model", output)

    def test_show_changes_nothing(self):
        before = self.env_text()
        code, output = self.run_cli(["--show"])
        self.assertEqual(code, 0)
        self.assertIn("ollama-local", output)
        self.assertEqual(self.written(), CONFIG)
        self.assertEqual(self.env_text(), before)

    def test_non_interactive_writes_the_route(self):
        code, output = self.run_cli(["coder", "openai-direct", "gpt-5.6"],
                                    answers=["sk-test"])
        self.assertEqual(code, 0)
        self.assertEqual(models.route(self.written(), "coder"),
                         ("openai-direct", "gpt-5.6"))
        self.assertIn("docker compose up -d reviewer coder", output)

    def test_it_asks_for_the_key_the_choice_needs(self):
        code, _ = self.run_cli(["coder", "openai-direct", "gpt-5.6"],
                               answers=["sk-test"])
        self.assertEqual(models.env_value("OPENAI_API_KEY", path=self.env_path),
                         "sk-test")

    def test_a_key_already_in_env_is_not_asked_for(self):
        models.env_set("OPENAI_API_KEY", "sk-already", path=self.env_path,
                       backup=os.path.join(self.dir, "backup"))
        # No answers: an unexpected prompt fails the test rather than hanging.
        self.run_cli(["coder", "openai-direct", "gpt-5.6"])
        self.assertEqual(models.env_value("OPENAI_API_KEY", path=self.env_path),
                         "sk-already")

    def test_a_keyless_choice_asks_for_nothing(self):
        self.run_cli(["coder", "ollama-local", "qwen2.5-coder:7b"])
        self.assertEqual(models.route(self.written(), "coder"),
                         ("ollama-local", "qwen2.5-coder:7b"))

    def test_skipping_the_key_still_writes_the_route(self):
        # Half-done beats not-done: the agent says which .env value is empty at
        # startup, so a skip is recoverable and a lost choice is not.
        code, output = self.run_cli(["coder", "openai-direct", "gpt-5.6"],
                                    answers=[""])
        self.assertEqual(code, 0)
        self.assertIn("skipped", output)
        self.assertEqual(models.route(self.written(), "coder"),
                         ("openai-direct", "gpt-5.6"))

    def test_a_model_off_the_list_is_written_with_a_note(self):
        # The list is a menu, not an allowlist: providers ship models faster
        # than this file is edited.
        code, output = self.run_cli(["coder", "openai-direct", "gpt-6"],
                                    answers=["sk-test"])
        self.assertIn("not in", output)
        self.assertEqual(models.route(self.written(), "coder")[1], "gpt-6")

    def test_wrong_argument_count(self):
        with self.assertRaises(models.ModelError):
            self.run_cli(["coder", "openai-direct"])

    def test_interactive_picks_both_roles(self):
        # The menu is ordered by PROVIDER_ORDER, not by the config array, so
        # openai-direct is 3 here even though the fixture lists it second.
        # reviewer: provider 3 (openai-direct), its key, then model 1 (gpt-5.6)
        # coder:    provider 1 (ollama-local), no key to ask, model 2
        code, output = self.run_cli([], answers=["3", "sk-test", "1", "1", "2"])
        self.assertEqual(code, 0)
        cfg = self.written()
        self.assertEqual(models.route(cfg, "reviewer"), ("openai-direct", "gpt-5.6"))
        self.assertEqual(models.route(cfg, "coder"),
                         ("ollama-local", "qwen2.5-coder:7b"))
        # The roles disagree, so the shared routes were left alone.
        self.assertEqual(cfg["routes"]["main"]["endpoint"], "ollama-local")
        self.assertEqual(models.env_value("OPENAI_API_KEY", path=self.env_path),
                         "sk-test")

    def test_interactive_empty_answer_keeps_what_is_there(self):
        code, _ = self.run_cli([], answers=["", "", "", ""])
        cfg = self.written()
        self.assertEqual(models.route(cfg, "reviewer"),
                         ("ollama-local", "qwen3.5:9b-32k"))
        self.assertEqual(models.route(cfg, "coder"),
                         ("ollama-local", "qwen3.5:9b-32k"))

    def test_interactive_rejects_a_number_off_the_menu_and_asks_again(self):
        code, output = self.run_cli([], answers=["9", "1", "1", "1", "1", "1"])
        self.assertIn("not one of", output)
        self.assertEqual(models.route(self.written(), "reviewer")[0], "ollama-local")

    def test_the_local_menu_leads_with_what_the_host_has(self):
        # A tag the config never suggested is still a real answer if it is on
        # the disk; a suggested one that is missing is not, and goes last.
        self.host_has({"qwen2.5-coder:7b", "llama9:70b"})

        code, output = self.run_cli([], answers=["1", "2", "1", "1"])
        top = output[output.index("== reviewer"):output.index("== coder")]
        # By menu position, not by offset in the text: the header line names the
        # current model too, and would match first.
        entries = [line.strip() for line in top.splitlines()
                   if line.strip()[1:3] == ") "]
        # past the provider menu, and stopping before the "other" escape hatch
        menu = entries[len(models.endpoints(self.written())):-1]
        self.assertEqual(menu, ["1) qwen2.5-coder:7b",
                                "2) llama9:70b       pulled here, not in the config",
                                "3) qwen3.5:9b-32k   not on this host"])
        self.assertEqual(models.route(self.written(), "reviewer"),
                         ("ollama-local", "llama9:70b"))

    def test_a_suggested_model_with_a_stock_window_is_offered_but_marked(self):
        # It is in the config and on the disk, so refusing it would be wrong;
        # saying nothing would be worse. gemma4 in the shipped config is exactly
        # this case.
        self.host_has({"qwen3.5:9b-32k", "qwen2.5-coder:7b"},
                      windows={"qwen2.5-coder:7b": None})
        code, output = self.run_cli([], answers=["1", "2", "1", "1"])
        self.assertIn("no pinned window", output)
        self.assertEqual(models.route(self.written(), "reviewer"),
                         ("ollama-local", "qwen2.5-coder:7b"))

    def test_unpinned_tags_the_config_never_named_are_named_not_listed(self):
        # A machine with a dozen models pulled would otherwise bury the ones
        # that work. They are still reachable through `other`.
        self.host_has({"qwen3.5:9b-32k", "mistral:7b", "llama9:70b"},
                      windows={"mistral:7b": None, "llama9:70b": 4096})
        code, output = self.run_cli([], answers=["1", "1", "1", "1"])
        top = output[output.index("== reviewer"):output.index("== coder")]
        self.assertIn("2 more here have no pinned window", top)
        self.assertIn("llama9:70b", top)
        self.assertIn("mistral:7b", top)
        entries = [line for line in top.splitlines() if line.strip()[1:3] == ") "]
        self.assertNotIn("mistral:7b", "".join(entries))

    def test_a_missing_model_comes_with_the_command_to_get_it(self):
        self.host_has(set())
        code, output = self.run_cli([], answers=["1", "1", "1", "1"])
        self.assertIn("does not have", output)
        self.assertIn("ollama create qwen3.5:9b-32k -f Modelfile", output)

    def test_a_local_provider_is_never_asked_for_a_key(self):
        code, output = self.run_cli([], answers=["1", "1", "1", "1"])
        self.assertNotIn("paste it", output)

    def test_interactive_other_takes_a_typed_model(self):
        # reviewer: provider 3 (openai-direct), model "other" (3rd of 2
        # models + other), typed
        code, output = self.run_cli([], answers=["3", "sk-test", "3",
                                                 "gpt-6-preview", "1", "1"])
        self.assertEqual(models.route(self.written(), "reviewer"),
                         ("openai-direct", "gpt-6-preview"))
        self.assertIn("menu, not a limit", output)


class TestShippedConfig(unittest.TestCase):
    """The file as it ships has to be coherent, or the first `up` is the one
    that finds out."""

    def setUp(self):
        self.cfg = models.load(os.path.join(REPO, "rayline", "pingpong.json"))

    def committed(self):
        """The config as committed, not as it sits on disk. `./pingpong model`
        rewrites the working copy, so asserting on that would fail for every
        operator who has run it — including on this machine."""
        try:
            blob = subprocess.check_output(
                ["git", "show", "HEAD:rayline/pingpong.json"], cwd=REPO,
                stderr=subprocess.DEVNULL)
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("no git, or the file is not committed here")
        return json.loads(blob.decode("utf-8"))

    def test_it_ships_with_no_brain_chosen(self):
        # On purpose, and this is the assertion that keeps it that way: a
        # working default is a decision made on the operator's behalf, and the
        # one thing this repo cannot know is which models are on their host.
        cfg = self.committed()
        self.assertEqual(models.unset_roles(cfg), ["reviewer", "coder"],
                         "the committed config names a brain, so a fresh clone "
                         "gets a default nobody chose")
        for role in ("reviewer", "coder"):
            self.assertEqual(models.route(cfg, role), (None, None))

    def test_the_shared_routes_ship_unset_too(self):
        # `main` is the fallback for anything naming neither alias. Leaving it
        # pointed somewhere while the roles are blank is a half-default.
        cfg = self.committed()
        for shared in ("main", "subagent"):
            entry = cfg["routes"][shared]
            self.assertNotIn("model", entry)
            self.assertNotIn("endpoint", entry)

    def test_the_provider_menu_leads_with_local_then_rayline(self):
        names = [spec["id"] for spec in models.ordered_endpoints(self.cfg)]
        self.assertEqual(names[:2], ["ollama-local", "rayline-cloud"])
        self.assertEqual(sorted(names),
                         sorted(spec["id"] for spec in models.endpoints(self.cfg)))

    def test_every_endpoint_lists_models_that_could_be_chosen(self):
        for spec in models.endpoints(self.cfg):
            self.assertTrue(spec.get("models"),
                            "%s offers no menu, so `model` has nothing to show"
                            % spec.get("id"))

    def test_every_endpoint_key_has_a_dotenv_name(self):
        for spec in models.endpoints(self.cfg):
            key_env = spec.get("api_key_env")
            if key_env:
                self.assertIn(key_env, models.DOTENV_NAMES,
                              "%s draws on %s, which docker-compose.yml has to "
                              "map from a .env name" % (spec["id"], key_env))

    def test_every_dotenv_name_is_in_the_sample(self):
        # An endpoint whose key is never mentioned in .env.sample is a key
        # nobody knows to set.
        with io.open(os.path.join(REPO, ".env.sample"), encoding="utf-8") as handle:
            sample = handle.read()
        for spec in models.endpoints(self.cfg):
            key_env = spec.get("api_key_env")
            if key_env:
                self.assertIn(models.DOTENV_NAMES[key_env] + "=", sample)

    def test_it_ships_on_an_endpoint_that_needs_no_key(self):
        # A fresh clone must be able to get to a first round without an account
        # anywhere, which is the whole reason the default is local.
        for role in ("reviewer", "coder"):
            self.assertIsNone(models.needed_key(self.cfg, role))


class TestLocalModels(unittest.TestCase):
    """What the host actually has, which the config's `models` list does not say."""

    def test_container_hostname_is_rewritten_for_this_side(self):
        # base_url is written for the agent containers. `pingpong model` runs on
        # the host, where that name does not resolve.
        self.assertEqual(models.host_url("http://host.docker.internal:11434"),
                         "http://127.0.0.1:11434")
        self.assertEqual(models.host_url("https://api.openai.com"),
                         "https://api.openai.com")

    def test_a_keyed_endpoint_is_never_probed(self):
        # Nothing to ask and nowhere local to ask it; a hosted provider's catalog
        # is not this command's business.
        spec = {"id": "openai-direct", "base_url": "https://api.openai.com",
                "api_key_env": "RAYLINE_OPENAI_API_KEY"}
        self.assertIsNone(models.local_models(spec))

    def test_nothing_listening_reads_as_unknown_not_as_empty(self):
        # The difference matters: unknown offers every model without comment,
        # empty would mark all of them missing.
        spec = {"id": "ollama-local", "base_url": "http://127.0.0.1:1"}
        self.assertIsNone(models.local_models(spec, timeout=1))

    def test_a_pinned_tag_is_created_and_not_pulled(self):
        # `ollama pull qwen2.5-coder:7b-32k` fails: there is no such upstream tag.
        # The suffix means someone pinned the window, which is the point of it.
        lines = []
        models.pull_recipe("qwen2.5-coder:7b-32k", lines.append)
        text = "\n".join(lines)
        self.assertIn("ollama pull qwen2.5-coder:7b", text)
        self.assertIn("PARAMETER num_ctx 32768", text)
        self.assertIn("ollama create qwen2.5-coder:7b-32k -f Modelfile", text)

    def test_a_stock_tag_is_just_pulled(self):
        lines = []
        models.pull_recipe("deepseek-r2:14b", lines.append)
        self.assertEqual(lines, ["    ollama pull deepseek-r2:14b"])


if __name__ == "__main__":
    unittest.main()

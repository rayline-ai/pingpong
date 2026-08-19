"""Tests for AGENT_MODE: what `up` refuses to start, and what it writes.

The credential directories are faked in a temp tree rather than read from the
machine running the suite — the whole point of the check is what it does when a
login is absent, and a developer's real ~/.claude would make that untestable.
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import models, subscription  # noqa: E402

ENV = """\
# Prose the operator may have edited.
AGENT_MODE=router
SUBSCRIPTION_MODEL=
CREDENTIALS_DIR=
MAX_ROUNDS=3
"""


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.env_path = os.path.join(self.dir, ".env")
        with io.open(self.env_path, "w", encoding="utf-8", newline="") as handle:
            handle.write(ENV)
        original = models.ENV_PATH
        models.ENV_PATH = self.env_path
        self.addCleanup(setattr, models, "ENV_PATH", original)

        # A fake home, so `check` sees exactly the logins this test set up.
        self.home = os.path.join(self.dir, "home")
        os.makedirs(self.home)
        original_home = subscription.home
        subscription.home = lambda: self.home
        self.addCleanup(setattr, subscription, "home", original_home)

        self.env = {}

    def login(self, mode, payload=None):
        """Write the file a real login would leave behind.

        claude-sub only — codex-sub reads nothing from the host, so there is no
        host-side login for it to find.
        """
        body = {"claudeAiOauth": {"accessToken": "sk-ant-oat-x",
                                  "refreshToken": "r", "expiresAt": 0}}
        path = subscription.credentials_file(mode, self.home)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(body if payload is None else payload))
        return path

    def ensure(self):
        lines = []
        code = subscription.ensure(lines.append, env=self.env)
        return code, "\n".join(lines)


class TestMode(unittest.TestCase):
    def test_unset_is_router(self):
        self.assertEqual(subscription.mode({}), "router")
        self.assertEqual(subscription.mode({"AGENT_MODE": ""}), "router")
        self.assertEqual(subscription.mode({"AGENT_MODE": "  "}), "router")

    def test_only_the_two_are_subscriptions(self):
        self.assertTrue(subscription.is_subscription("claude-sub"))
        self.assertTrue(subscription.is_subscription("codex-sub"))
        for other in ("router", "", "sub", "claude"):
            self.assertFalse(subscription.is_subscription(other), other)

    def test_compose_path_uses_forward_slashes(self):
        # A backslash before a letter is an escape to compose, so C:\Users would
        # arrive as C:Users and the mount would silently be a relative path.
        self.assertEqual(subscription.compose_path(r"C:\Users\someone\.claude"),
                         "C:/Users/someone/.claude")


class TestRouterMode(Sandbox):
    def test_it_does_nothing_at_all(self):
        code, output = self.ensure()
        self.assertEqual(code, 0)
        self.assertEqual(output, "")
        self.assertEqual(models.env_value("CREDENTIALS_DIR"), "")

    def test_a_mode_that_does_not_exist_is_refused(self):
        # Not silently treated as router: a typo in AGENT_MODE would otherwise
        # start the stack in the mode the operator was trying to leave.
        self.env = {"AGENT_MODE": "claude"}
        code, output = self.ensure()
        self.assertEqual(code, 1)
        self.assertIn("is not a mode", output)


class TestSubscriptionMode(Sandbox):
    def test_it_refuses_when_nobody_has_logged_in(self):
        self.env = {"AGENT_MODE": "claude-sub", "SUBSCRIPTION_MODEL": "m"}
        code, output = self.ensure()
        self.assertEqual(code, 1)
        self.assertIn("no login", output)

    def test_it_refuses_a_directory_with_no_credential_file(self):
        os.makedirs(subscription.credentials_dir("claude-sub", self.home))
        self.env = {"AGENT_MODE": "claude-sub", "SUBSCRIPTION_MODEL": "m"}
        code, output = self.ensure()
        self.assertEqual(code, 1)
        self.assertIn(".credentials.json", output)

    def test_it_refuses_without_a_model(self):
        # There is no router in this mode, so nothing resolves an alias. Failing
        # here rather than letting the provider 404 on "reviewer-brain".
        self.login("claude-sub")
        self.env = {"AGENT_MODE": "claude-sub"}
        code, output = self.ensure()
        self.assertEqual(code, 1)
        self.assertIn("SUBSCRIPTION_MODEL", output)

    def test_it_writes_the_host_directory_when_everything_is_there(self):
        self.login("claude-sub")
        self.env = {"AGENT_MODE": "claude-sub", "SUBSCRIPTION_MODEL": "claude-sonnet-4-6"}
        code, output = self.ensure()
        self.assertEqual(code, 0)
        written = models.env_value("CREDENTIALS_DIR")
        self.assertTrue(written.endswith("/.claude"), written)
        self.assertNotIn("\\", written)
        self.assertIn("CREDENTIALS_DIR", output)

    def test_codex_needs_nothing_from_this_host(self):
        # Its session is Hermes' own, made by `pingpong login` and kept in a
        # volume. Reading ~/.codex would work once and then revoke the
        # operator's own `codex` CLI, so nothing here goes looking for it.
        self.env = {"AGENT_MODE": "codex-sub", "SUBSCRIPTION_MODEL": "gpt-5.5"}
        code, output = self.ensure()
        self.assertEqual(code, 0, output)
        self.assertEqual(models.env_value("CREDENTIALS_DIR"), "")
        self.assertFalse(subscription.uses_host_login("codex-sub"))
        self.assertIsNone(subscription.credentials_dir("codex-sub"))

    def test_codex_still_needs_a_model(self):
        self.env = {"AGENT_MODE": "codex-sub"}
        code, output = self.ensure()
        self.assertEqual(code, 1)
        self.assertIn("SUBSCRIPTION_MODEL", output)
        # The example must be one a ChatGPT account is allowed to ask for: every
        # -codex id is refused with "not supported when using Codex with a
        # ChatGPT account", which reads like a typo rather than a plan limit.
        self.assertNotIn("-codex", output)

    def test_it_keeps_every_other_line_byte_for_byte(self):
        self.login("claude-sub")
        self.env = {"AGENT_MODE": "claude-sub", "SUBSCRIPTION_MODEL": "m"}
        self.ensure()
        text = io.open(self.env_path, encoding="utf-8").read()
        self.assertIn("# Prose the operator may have edited.", text)
        self.assertIn("MAX_ROUNDS=3", text)

    def test_it_is_quiet_when_the_value_is_already_right(self):
        # Runs on every `up`, so a no-op has to look like one.
        self.login("claude-sub")
        self.env = {"AGENT_MODE": "claude-sub", "SUBSCRIPTION_MODEL": "m"}
        self.ensure()
        code, output = self.ensure()
        self.assertEqual(code, 0)
        self.assertEqual(output, "")


class TestExecSeam(unittest.TestCase):
    """The engine must stay mode-blind.

    `hermes-run` is where the two ANTHROPIC_* image variables get stripped, and
    `docker exec` is the only point at which they can be. Calling `hermes`
    directly would work perfectly in router mode and silently spend API credit in
    a subscription one, which is why this is worth a test of its own.
    """

    def setUp(self):
        from src import agents
        self.agents = agents
        self.seen = []
        original = agents.subprocess.run

        class Result(object):
            returncode = 0
            stdout = "reviewed"
            stderr = ""

        def fake(cmd, **kwargs):
            self.seen.append(cmd)
            return Result()

        agents.subprocess.run = fake
        self.addCleanup(setattr, agents.subprocess, "run", original)

    def test_it_runs_hermes_run(self):
        self.agents._exec("pingpong-reviewer", "/work/x", "/work/.pingpong/p.md", 60)
        inner = self.seen[0][-1]
        self.assertIn("hermes-run -z", inner)

    def test_it_does_not_branch_on_the_mode(self):
        # Same command in every mode: the difference lives in the image.
        import os as _os
        commands = []
        for mode in ("router", "claude-sub", "codex-sub"):
            _os.environ["AGENT_MODE"] = mode
            self.addCleanup(_os.environ.pop, "AGENT_MODE", None)
            self.seen = []
            self.agents._exec("pingpong-coder", "/work/x", "/work/p.md", 60)
            commands.append(self.seen[0])
        self.assertEqual(commands[0], commands[1])
        self.assertEqual(commands[1], commands[2])


class TestSecrets(Sandbox):
    def test_no_token_is_ever_printed(self):
        # This module reads a credential file to decide whether it is usable. It
        # must never put what it read on the terminal or into .env.
        self.login("claude-sub")
        self.env = {"AGENT_MODE": "claude-sub", "SUBSCRIPTION_MODEL": "m"}
        _, output = self.ensure()
        text = output + io.open(self.env_path, encoding="utf-8").read()
        self.assertNotIn("sk-ant-oat-x", text)


if __name__ == "__main__":
    unittest.main()

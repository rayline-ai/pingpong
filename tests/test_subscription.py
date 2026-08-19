"""Tests for REVIEWER_MODE / CODER_MODE: what `up` refuses, and what it writes.

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
REVIEWER_MODE=router
CODER_MODE=router
REVIEWER_MODEL=
CODER_MODEL=
CREDENTIALS_DIR=
MAX_ROUNDS=3
"""


def env(reviewer=None, coder=None, reviewer_model=None, coder_model=None):
    """The .env values as compose would present them, roles left out when unset."""
    values = {}
    for key, value in (("REVIEWER_MODE", reviewer), ("CODER_MODE", coder),
                       ("REVIEWER_MODEL", reviewer_model), ("CODER_MODEL", coder_model)):
        if value is not None:
            values[key] = value
    return values


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
        for role in subscription.ROLES:
            self.assertEqual(subscription.mode(role, {}), "router")
            self.assertEqual(subscription.mode(role, {"%s_MODE" % role.upper(): ""}),
                             "router")
            self.assertEqual(subscription.mode(role, {"%s_MODE" % role.upper(): "  "}),
                             "router")

    def test_the_roles_are_read_independently(self):
        # The whole point of the split: one role's setting must not answer for
        # the other, in either direction.
        values = env(reviewer="claude-sub", coder="codex-sub",
                     reviewer_model="claude-sonnet-4-6", coder_model="gpt-5.5")
        self.assertEqual(subscription.mode("reviewer", values), "claude-sub")
        self.assertEqual(subscription.mode("coder", values), "codex-sub")
        self.assertEqual(subscription.model("reviewer", values), "claude-sonnet-4-6")
        self.assertEqual(subscription.model("coder", values), "gpt-5.5")

    def test_one_role_on_a_subscription_leaves_the_other_on_router(self):
        values = env(reviewer="codex-sub", reviewer_model="gpt-5.5")
        self.assertEqual(subscription.router_roles(values), ["coder"])
        self.assertEqual(subscription.subscription_roles(values), ["reviewer"])
        self.assertEqual(subscription.roles_in("codex-sub", values), ["reviewer"])

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
        # Not silently treated as router: a typo would otherwise start the stack
        # in the mode the operator was trying to leave.
        self.env = env(reviewer="claude")
        code, output = self.ensure()
        self.assertEqual(code, 1)
        self.assertIn("is not a mode", output)

    def test_the_message_names_the_role_that_is_wrong(self):
        # With two settings, "that is not a mode" without a name is half an
        # answer — and the wrong half if both are set.
        self.env = env(coder="cdoex-sub")
        code, output = self.ensure()
        self.assertEqual(code, 1)
        self.assertIn("CODER_MODE", output)
        self.assertNotIn("REVIEWER_MODE", output)


class TestSubscriptionMode(Sandbox):
    def test_it_refuses_when_nobody_has_logged_in(self):
        self.env = env(reviewer="claude-sub", reviewer_model="m")
        code, output = self.ensure()
        self.assertEqual(code, 1)
        self.assertIn("no login", output)

    def test_it_refuses_a_directory_with_no_credential_file(self):
        os.makedirs(subscription.credentials_dir("claude-sub", self.home))
        self.env = env(reviewer="claude-sub", reviewer_model="m")
        code, output = self.ensure()
        self.assertEqual(code, 1)
        self.assertIn(".credentials.json", output)

    def test_it_refuses_without_a_model(self):
        # There is no router in this mode, so nothing resolves an alias. Failing
        # here rather than letting the provider 404 on "reviewer-brain".
        self.login("claude-sub")
        self.env = env(reviewer="claude-sub")
        code, output = self.ensure()
        self.assertEqual(code, 1)
        self.assertIn("REVIEWER_MODEL", output)

    def test_it_writes_the_host_directory_when_everything_is_there(self):
        self.login("claude-sub")
        self.env = env(reviewer="claude-sub", reviewer_model="claude-sonnet-4-6")
        code, output = self.ensure()
        self.assertEqual(code, 0)
        written = models.env_value("CREDENTIALS_DIR")
        self.assertTrue(written.endswith("/.claude"), written)
        self.assertNotIn("\\", written)
        self.assertIn("CREDENTIALS_DIR", output)

    def test_one_claude_login_serves_whichever_role_wants_it(self):
        # One account is one login, so CREDENTIALS_DIR stays a single value no
        # matter which role — or both — is on claude-sub.
        self.login("claude-sub")
        for values in (env(reviewer="claude-sub", reviewer_model="m"),
                       env(coder="claude-sub", coder_model="m"),
                       env(reviewer="claude-sub", coder="claude-sub",
                           reviewer_model="m", coder_model="m")):
            models.env_set("CREDENTIALS_DIR", "")
            self.env = values
            code, _ = self.ensure()
            self.assertEqual(code, 0)
            self.assertTrue(models.env_value("CREDENTIALS_DIR").endswith("/.claude"))

    def test_codex_needs_nothing_from_this_host(self):
        # Its session is Hermes' own, made by `pingpong login` and kept in a
        # volume. Reading ~/.codex would work once and then revoke the
        # operator's own `codex` CLI, so nothing here goes looking for it.
        self.env = env(reviewer="codex-sub", reviewer_model="gpt-5.5")
        code, output = self.ensure()
        self.assertEqual(code, 0, output)
        self.assertEqual(models.env_value("CREDENTIALS_DIR"), "")
        self.assertFalse(subscription.uses_host_login("codex-sub"))
        self.assertIsNone(subscription.credentials_dir("codex-sub"))

    def test_codex_still_needs_a_model(self):
        self.env = env(coder="codex-sub")
        code, output = self.ensure()
        self.assertEqual(code, 1)
        self.assertIn("CODER_MODEL", output)
        # The example must be one a ChatGPT account is allowed to ask for: every
        # -codex id is refused with "not supported when using Codex with a
        # ChatGPT account", which reads like a typo rather than a plan limit.
        self.assertNotIn("-codex", output)

    def test_one_of_each_subscription_is_a_working_pair(self):
        # The configuration this whole split exists for.
        self.login("claude-sub")
        self.env = env(reviewer="claude-sub", coder="codex-sub",
                       reviewer_model="claude-sonnet-4-6", coder_model="gpt-5.5")
        code, output = self.ensure()
        self.assertEqual(code, 0, output)
        self.assertTrue(models.env_value("CREDENTIALS_DIR").endswith("/.claude"))

    def test_both_roles_are_reported_before_it_gives_up(self):
        # Two roles configured in one sitting are usually wrong in two ways, and
        # finding out about the second only after fixing the first is the slower
        # of the two conversations.
        self.env = env(reviewer="claude-sub", coder="codex-sub",
                       reviewer_model="m")
        code, output = self.ensure()
        self.assertEqual(code, 1)
        self.assertIn("REVIEWER_MODE", output)
        self.assertIn("CODER_MODEL", output)

    def test_it_keeps_every_other_line_byte_for_byte(self):
        self.login("claude-sub")
        self.env = env(reviewer="claude-sub", reviewer_model="m")
        self.ensure()
        text = io.open(self.env_path, encoding="utf-8").read()
        self.assertIn("# Prose the operator may have edited.", text)
        self.assertIn("MAX_ROUNDS=3", text)

    def test_it_is_quiet_when_the_value_is_already_right(self):
        # Runs on every `up`, so a no-op has to look like one.
        self.login("claude-sub")
        self.env = env(reviewer="claude-sub", reviewer_model="m")
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
        # Same command in every mode, and the same for both roles: the
        # difference lives in the image, keyed off the container's own
        # AGENT_MODE, which compose renames from the role's setting.
        import os as _os
        commands = []
        for mode in ("router", "claude-sub", "codex-sub"):
            for role in ("REVIEWER", "CODER"):
                _os.environ["%s_MODE" % role] = mode
                self.addCleanup(_os.environ.pop, "%s_MODE" % role, None)
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
        self.env = env(reviewer="claude-sub", reviewer_model="m")
        _, output = self.ensure()
        text = output + io.open(self.env_path, encoding="utf-8").read()
        self.assertNotIn("sk-ant-oat-x", text)


if __name__ == "__main__":
    unittest.main()

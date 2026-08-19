"""Tests for the address preflight: what `up` writes into .env before Forgejo
boots, and the cases where it must keep its hands off.

No network. `lan_address` is stubbed everywhere except the one test that checks
it returns something sane on the machine running the suite.
"""
import io
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import address, models  # noqa: E402

ENV = """\
# Prose the operator may have edited.
FORGEJO_ROOT_URL=http://<ip-address>:23000/
FORGEJO_PORT=23000
MAX_ROUNDS=3
"""


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.env_path = os.path.join(self.dir, ".env")
        self.write(ENV)
        original = models.ENV_PATH
        models.ENV_PATH = self.env_path
        self.addCleanup(setattr, models, "ENV_PATH", original)
        self.detect("192.0.2.50")

    def write(self, text):
        with io.open(self.env_path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)

    def detect(self, value):
        original = address.lan_address
        address.lan_address = lambda: value
        self.addCleanup(setattr, address, "lan_address", original)

    def ensure(self):
        lines = []
        result = address.ensure(lines.append)
        return result, "\n".join(lines)

    def value(self):
        return models.env_value("FORGEJO_ROOT_URL", path=self.env_path)


class TestNeedsSetting(unittest.TestCase):
    def test_the_placeholder_and_the_empties(self):
        for value in (None, "", "http://<ip-address>:23000/"):
            self.assertTrue(address.needs_setting(value), value)

    def test_loopback_counts_as_unset(self):
        # It is never the wanted answer for a stack whose point is that other
        # machines reach it, and it is the value that fails silently.
        for value in ("http://localhost:23000/", "http://127.0.0.1:23000/",
                      "http://0.0.0.0:23000/"):
            self.assertTrue(address.needs_setting(value), value)

    def test_a_real_address_is_left_alone(self):
        # RFC 5737 documentation addresses throughout: nobody's actual machine
        # belongs in a committed test.
        for value in ("http://192.0.2.101:23000/", "https://forge.example.com/",
                      "http://198.51.100.4:23000/"):
            self.assertFalse(address.needs_setting(value), value)

    def test_host_is_read_without_the_scheme_or_port(self):
        self.assertEqual(address.host_of("http://192.0.2.101:23000/"),
                         "192.0.2.101")
        self.assertEqual(address.host_of("https://forge.example.com/x"),
                         "forge.example.com")


class TestEnsure(Sandbox):
    def test_it_fills_the_placeholder_in(self):
        result, output = self.ensure()
        self.assertEqual(result, "http://192.0.2.50:23000/")
        self.assertEqual(self.value(), "http://192.0.2.50:23000/")
        self.assertIn("FORGEJO_ROOT_URL=http://192.0.2.50:23000/", output)

    def test_it_uses_the_port_the_operator_set(self):
        self.write(ENV.replace("FORGEJO_PORT=23000", "FORGEJO_PORT=9000"))
        self.ensure()
        self.assertEqual(self.value(), "http://192.0.2.50:9000/")

    def test_a_deliberate_address_is_not_overwritten(self):
        # Someone who typed a hostname meant it; this runs on every `up`.
        self.write(ENV.replace("http://<ip-address>:23000/",
                               "https://forge.example.com/"))
        result, output = self.ensure()
        self.assertEqual(result, "https://forge.example.com/")
        self.assertEqual(output, "")

    def test_localhost_is_replaced_because_it_is_the_silent_bug(self):
        self.write(ENV.replace("http://<ip-address>:23000/",
                               "http://localhost:23000/"))
        self.ensure()
        self.assertEqual(self.value(), "http://192.0.2.50:23000/")

    def test_no_address_leaves_the_file_alone_and_says_so(self):
        self.detect(None)
        before = io.open(self.env_path, encoding="utf-8").read()
        result, output = self.ensure()
        self.assertIn("leaving", output)
        self.assertEqual(io.open(self.env_path, encoding="utf-8").read(), before)

    def test_it_keeps_every_other_line_byte_for_byte(self):
        self.ensure()
        text = io.open(self.env_path, encoding="utf-8").read()
        self.assertIn("# Prose the operator may have edited.", text)
        self.assertIn("MAX_ROUNDS=3", text)

    def test_it_never_asks(self):
        # The whole point: the machine knows its own address, so `up` does not
        # stop to ask for it.
        original = address.models.env_set
        try:
            self.ensure()
        finally:
            address.models.env_set = original


class TestDetection(unittest.TestCase):
    def test_it_finds_an_address_or_honestly_returns_none(self):
        # Not asserting a value -- CI has no LAN. Asserting it never returns a
        # loopback, which is the one answer that would be worse than none.
        found = address.lan_address()
        if found is not None:
            self.assertFalse(found.startswith("127."))
            self.assertNotIn(found, address.LOOPBACK)


if __name__ == "__main__":
    unittest.main()

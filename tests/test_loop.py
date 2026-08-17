"""Tests for the pure logic: verdict translation, prompt rendering, round counting.

Everything here runs without Docker, Forgejo or a model.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import forgejo, loop  # noqa: E402
from src import webhook  # noqa: E402


class TestParseVerdict(unittest.TestCase):
    def test_approve(self):
        self.assertEqual(loop.parse_verdict("Looks fine.\n\nVERDICT: APPROVE"),
                         forgejo.APPROVED)

    def test_request_changes(self):
        self.assertEqual(loop.parse_verdict("VERDICT: REQUEST_CHANGES"),
                         forgejo.REQUEST_CHANGES)

    def test_case_insensitive(self):
        self.assertEqual(loop.parse_verdict("verdict: approve"), forgejo.APPROVED)

    def test_last_verdict_wins(self):
        # A reviewer that reconsiders mid-answer means the later line.
        text = "VERDICT: APPROVE\n...on reflection:\nVERDICT: REQUEST_CHANGES"
        self.assertEqual(loop.parse_verdict(text), forgejo.REQUEST_CHANGES)

    def test_missing_verdict_is_not_an_approval(self):
        # Silence must never approve, or a truncated or failed review would
        # merge itself.
        self.assertEqual(loop.parse_verdict("I ran out of tokens"),
                         forgejo.REQUEST_CHANGES)
        self.assertEqual(loop.parse_verdict(""), forgejo.REQUEST_CHANGES)

    def test_verdict_inside_prose_is_ignored(self):
        # Only a line that is *only* a verdict counts, so the reviewer quoting
        # the contract back at us does not decide the round.
        self.assertEqual(
            loop.parse_verdict("You should end with VERDICT: APPROVE when done."),
            forgejo.REQUEST_CHANGES)


class TestRender(unittest.TestCase):
    def test_substitutes(self):
        self.assertEqual(loop.render("round {round}/{max_rounds}", round=2, max_rounds=3),
                         "round 2/3")

    def test_braces_in_the_diff_survive(self):
        # The reason this is str.replace and not str.format: a diff is full of
        # braces and format() would try to interpret them.
        diff = "+ if (x) { y(); }\n+ fmt('{0}')"
        self.assertIn(diff, loop.render("```diff\n{diff}\n```", diff=diff))

    def test_unused_placeholder_is_left_alone(self):
        self.assertEqual(loop.render("{a} {b}", a="1"), "1 {b}")


class TestTruncate(unittest.TestCase):
    def test_short_text_untouched(self):
        self.assertEqual(loop._truncate("abc", 100), "abc")

    def test_long_text_marked(self):
        out = loop._truncate("x" * 100, 10)
        self.assertTrue(out.startswith("x" * 10))
        self.assertIn("truncated", out)

    def test_zero_limit_disables(self):
        self.assertEqual(loop._truncate("x" * 100, 0), "x" * 100)


class TestRoundsDone(unittest.TestCase):
    @staticmethod
    def _commit(email):
        return {"commit": {"author": {"email": email}}}

    def test_counts_only_the_bot(self):
        commits = [self._commit("human@example.com"),
                   self._commit("pingpong-coder@local"),
                   self._commit("pingpong-coder@local")]
        self.assertEqual(forgejo.rounds_done(commits, "pingpong-coder@local"), 2)

    def test_case_insensitive(self):
        self.assertEqual(
            forgejo.rounds_done([self._commit("PingPong-Coder@Local")],
                                "pingpong-coder@local"), 1)

    def test_no_commits(self):
        self.assertEqual(forgejo.rounds_done([], "pingpong-coder@local"), 0)


class TestWebhookTriggers(unittest.TestCase):
    def test_opened_pr_triggers(self):
        run, _ = webhook.should_run("pull_request", {"action": "opened"}, "bot")
        self.assertTrue(run)

    def test_push_to_pr_triggers_next_round(self):
        run, _ = webhook.should_run("pull_request", {"action": "synchronized"}, "bot")
        self.assertTrue(run)

    def test_closing_a_pr_does_not(self):
        run, _ = webhook.should_run("pull_request", {"action": "closed"}, "bot")
        self.assertFalse(run)

    def test_human_request_changes_triggers(self):
        run, _ = webhook.should_run(
            "pull_request_review",
            {"action": "reviewed", "review": {"type": "request_changes"},
             "sender": {"login": "alice"}}, "bot")
        self.assertTrue(run)

    def test_the_bots_own_review_does_not(self):
        # Otherwise each round would trigger the next one twice.
        run, _ = webhook.should_run(
            "pull_request_review",
            {"action": "reviewed", "review": {"type": "request_changes"},
             "sender": {"login": "bot"}}, "bot")
        self.assertFalse(run)

    def test_approval_does_not_trigger(self):
        run, _ = webhook.should_run(
            "pull_request_review",
            {"action": "reviewed", "review": {"type": "approved"},
             "sender": {"login": "alice"}}, "bot")
        self.assertFalse(run)


class TestSignature(unittest.TestCase):
    SECRET = "s3cret"

    def test_accepts_a_good_signature(self):
        import hashlib
        import hmac
        body = b'{"action":"opened"}'
        sig = hmac.new(self.SECRET.encode(), body, hashlib.sha256).hexdigest()
        self.assertTrue(webhook.verify(self.SECRET, sig, body))

    def test_rejects_a_bad_one(self):
        self.assertFalse(webhook.verify(self.SECRET, "deadbeef", b"{}"))

    def test_rejects_a_missing_one(self):
        self.assertFalse(webhook.verify(self.SECRET, "", b"{}"))
        self.assertFalse(webhook.verify(self.SECRET, None, b"{}"))


class TestSlug(unittest.TestCase):
    def test_is_filesystem_safe(self):
        from src import gitops
        self.assertEqual(gitops.slug("my org", "some/repo", 7),
                         "my-org__some-repo__pr7")


if __name__ == "__main__":
    unittest.main()


class TestCommitMessage(unittest.TestCase):
    """The subject must describe the fix, not restate the PR."""

    def test_skips_bold_label_heading(self):
        msg = loop._commit_message(2, "**Fixed**\n- `a.py` — stop mutating the caller's list\n")
        self.assertEqual(msg.splitlines()[0],
                         "pingpong round 2: a.py — stop mutating the caller's list")

    def test_body_carries_full_summary(self):
        msg = loop._commit_message(1, "**Fixed**\n- did a thing\n\n**Not fixed**\n- nope")
        self.assertIn("**Not fixed**", msg)
        self.assertEqual(msg.splitlines()[1], "")

    def test_empty_fix_text_falls_back(self):
        self.assertEqual(loop._commit_message(3, ""), "pingpong round 3: address review feedback")

    def test_subject_is_truncated(self):
        msg = loop._commit_message(1, "x" * 200)
        self.assertLessEqual(len(msg.splitlines()[0]), len("pingpong round 1: ") + 60)


class TestCommitSubjectSource(unittest.TestCase):
    """Prefer the Fixed bullet over the coder's opening narration."""

    REAL = ("Verified by real execution: the failure path now raises URLError.\n"
            "\n**Fixed**\n"
            "- `net.py:6` — retries < 1 now raises ValueError up front\n"
            "\n**Not fixed**\n"
            "- the timeout, not raised this round\n")

    def test_prefers_fixed_bullet_over_narration(self):
        subject = loop._commit_message(1, self.REAL).splitlines()[0]
        self.assertEqual(subject, "pingpong round 1: net.py:6 — retries < 1 now raises ValueError up front")

    def test_never_takes_from_not_fixed(self):
        text = "**Fixed**\n\n**Not fixed**\n- skipped this one\n"
        # Subject only — the body keeps the whole summary, Not fixed included.
        subject = loop._commit_message(1, text).splitlines()[0]
        self.assertEqual(subject, "pingpong round 1: address review feedback")

    def test_falls_back_to_prose_without_a_fixed_section(self):
        subject = loop._commit_message(1, "Removed the duplicated retry loop.").splitlines()[0]
        self.assertEqual(subject, "pingpong round 1: Removed the duplicated retry loop.")


class TestArtifactExclude(unittest.TestCase):
    """Build output the agent produces must not land in the PR."""

    def _repo(self):
        import tempfile, shutil
        from src import gitops
        path = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, path, True)
        gitops.run(["init", "-q", "-b", "main"], cwd=path)
        return gitops, path

    def test_pycache_is_not_staged(self):
        gitops, repo = self._repo()
        gitops._write_exclude(repo)
        os.makedirs(os.path.join(repo, "__pycache__"))
        open(os.path.join(repo, "__pycache__", "m.cpython-313.pyc"), "w").close()
        open(os.path.join(repo, "m.py"), "w").write("x = 1\n")
        gitops.run(["add", "-A"], cwd=repo)
        staged = gitops.run(["diff", "--cached", "--name-only"], cwd=repo).splitlines()
        self.assertEqual(staged, ["m.py"])

    def test_leaves_the_repos_own_gitignore_alone(self):
        gitops, repo = self._repo()
        gitops._write_exclude(repo)
        self.assertFalse(os.path.exists(os.path.join(repo, ".gitignore")))

    def test_an_already_tracked_artifact_still_updates(self):
        # The exclude applies to untracked files only, so a repo that
        # deliberately commits one of these keeps working.
        gitops, repo = self._repo()
        open(os.path.join(repo, "vendor.pyc"), "w").write("a")
        gitops.run(["add", "-f", "vendor.pyc"], cwd=repo)
        gitops.run(["-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "seed"], cwd=repo)
        gitops._write_exclude(repo)
        open(os.path.join(repo, "vendor.pyc"), "w").write("b")
        gitops.run(["add", "-A"], cwd=repo)
        self.assertEqual(
            gitops.run(["diff", "--cached", "--name-only"], cwd=repo).splitlines(),
            ["vendor.pyc"])

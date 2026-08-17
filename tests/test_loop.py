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
        self.assertNotEqual(loop.parse_verdict("I ran out of tokens"), forgejo.APPROVED)
        self.assertNotEqual(loop.parse_verdict(""), forgejo.APPROVED)

    def test_missing_verdict_is_not_a_review_either(self):
        # None, not REQUEST_CHANGES: an agent runtime that failed while exiting 0
        # would otherwise be posted as a genuine "changes requested" review and
        # spend a round. run_round halts on None instead.
        self.assertIsNone(loop.parse_verdict("HTTP 401: Invalid credentials."))
        self.assertIsNone(loop.parse_verdict("I ran out of tokens"))
        self.assertIsNone(loop.parse_verdict(""))
        self.assertIsNone(loop.parse_verdict(None))

    def test_verdict_inside_prose_is_ignored(self):
        # Only a line that is *only* a verdict counts, so the reviewer quoting
        # the contract back at us does not decide the round.
        self.assertIsNone(
            loop.parse_verdict("You should end with VERDICT: APPROVE when done."))


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


class TestNoVerdictNote(unittest.TestCase):
    def test_quotes_what_came_back(self):
        note = loop._no_verdict_note(1, "HTTP 401: Invalid credentials.")
        self.assertIn("HTTP 401: Invalid credentials.", note)
        self.assertIn("no `VERDICT:` line", note)

    def test_survives_empty_output(self):
        self.assertIn("(no output)", loop._no_verdict_note(2, ""))

    def test_long_output_is_truncated(self):
        note = loop._no_verdict_note(1, "x" * 10000)
        self.assertIn("truncated", note)
        self.assertLess(len(note), 3000)


class _FakeForgejo:
    """Records writes so a test can assert which ones happened."""

    def __init__(self):
        self.comments = []
        self.created_reviews = []

    def pull_request(self, *a):
        return {"state": "open", "merged": False, "title": "t", "body": "b",
                "head": {"ref": "feature"}, "base": {"ref": "main"}}

    def pull_commits(self, *a):
        return []

    def reviews(self, *a):
        return []

    def issue_comments(self, *a):
        return [{"body": body} for body in self.comments]

    def clone_url(self, owner, repo, token=None):
        return "http://example.invalid/%s/%s.git" % (owner, repo)

    def comment(self, owner, repo, index, body):
        self.comments.append(body)

    def create_review(self, owner, repo, index, event, body):
        self.created_reviews.append((event, body))


class _Cfg:
    bot_name = "pingpong-coder"
    bot_email = "pingpong-coder@local"
    coder_token = "t"
    max_rounds = 3
    max_diff_bytes = 60000
    review_timeout = fix_timeout = 60
    work_root = "/tmp/pingpong-test"
    reviewer_container = coder_container = "c"
    prompts_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts")


class TestRunRoundOnBrokenRuntime(unittest.TestCase):
    """An agent runtime that fails while exiting 0 must not become a review.

    The regression: Hermes printed `HTTP 401: Invalid credentials.` and exited 0,
    which was posted to the PR as a REQUEST_CHANGES review and drove a fix round.
    """

    def setUp(self):
        self.fj = _FakeForgejo()
        self.calls = []
        self._saved = (loop.gitops.prepare_clone, loop.gitops.changed_files,
                       loop.gitops.diff, loop.gitops.is_dirty,
                       loop.agents.review, loop.agents.fix)
        loop.gitops.prepare_clone = lambda *a, **k: ("/tmp/clone", "origin/main")
        loop.gitops.changed_files = lambda *a, **k: ["discount.py"]
        loop.gitops.diff = lambda *a, **k: "+ some change"
        loop.gitops.is_dirty = lambda *a, **k: False
        loop.agents.fix = lambda *a, **k: self.calls.append("fix") or "fixed"

    def tearDown(self):
        (loop.gitops.prepare_clone, loop.gitops.changed_files, loop.gitops.diff,
         loop.gitops.is_dirty, loop.agents.review, loop.agents.fix) = self._saved

    def test_error_text_is_not_posted_as_a_review(self):
        loop.agents.review = lambda *a, **k: "HTTP 401: Invalid credentials."
        result = loop.run_round(_Cfg(), self.fj, "o", "r", 1, log=lambda m: None)

        self.assertEqual(result["action"], "errored")
        self.assertEqual(self.fj.created_reviews, [])       # no review event at all
        self.assertEqual(len(self.fj.comments), 1)
        self.assertIn("HTTP 401", self.fj.comments[0])
        self.assertEqual(self.calls, [])                     # and no round was spent fixing

    def test_a_real_request_changes_still_posts_and_fixes(self):
        loop.agents.review = lambda *a, **k: "Needs work.\n\nVERDICT: REQUEST_CHANGES"
        loop.run_round(_Cfg(), self.fj, "o", "r", 1, log=lambda m: None)

        self.assertEqual(self.fj.created_reviews[0][0], forgejo.REQUEST_CHANGES)
        self.assertEqual(self.calls, ["fix"])


class TestCommentCommand(unittest.TestCase):
    """A human comment mentioning any of the bot names starts a round."""

    BOTS = ("pingpong-reviewer", "pingpong-coder")

    def check(self, body, sender="marcin", pull_request={}, action="created"):
        payload = {"action": action, "sender": {"login": sender},
                   "issue": {"number": 9, "pull_request": pull_request},
                   "comment": {"body": body}}
        return webhook.should_run("issue_comment", payload, "pingpong-coder", self.BOTS)

    def test_any_of_the_three_names_triggers(self):
        for body in ("@pingpong", "@pingpong-reviewer have another look",
                     "please @pingpong-coder", "@PingPong AGAIN"):
            self.assertTrue(self.check(body)[0], body)

    def test_ordinary_comment_does_not(self):
        self.assertFalse(self.check("looks good to me")[0])
        self.assertFalse(self.check("emailed pingpong@example.com")[0])

    def test_the_bots_own_comment_does_not(self):
        # Otherwise the marker comment the reset posts would trigger a round,
        # which would post another marker.
        for bot in self.BOTS:
            self.assertFalse(self.check("@pingpong", sender=bot)[0], bot)

    def test_plain_issue_does_not(self):
        self.assertFalse(self.check("@pingpong", pull_request=None)[0])

    def test_only_on_creation(self):
        self.assertFalse(self.check("@pingpong", action="edited")[0])


class TestRoundBudgetReset(unittest.TestCase):
    """The marker comment is the state store for the reset."""

    def commits(self, n):
        return [{"commit": {"author": {"email": "pingpong-coder@local"}}}] * n

    def test_baseline_subtracts_banked_rounds(self):
        self.assertEqual(forgejo.rounds_done(self.commits(3), "pingpong-coder@local"), 3)
        self.assertEqual(
            forgejo.rounds_done(self.commits(3), "pingpong-coder@local", baseline=3), 0)

    def test_baseline_is_read_from_the_marker(self):
        comments = [{"body": "please fix"},
                    {"body": "Round budget reset.\n\n" + forgejo.RESET_MARKER % 2}]
        self.assertEqual(forgejo.rounds_baseline(comments), 2)

    def test_no_marker_means_no_baseline(self):
        self.assertEqual(forgejo.rounds_baseline([{"body": "hi"}]), 0)
        self.assertEqual(forgejo.rounds_baseline([]), 0)

    def test_highest_marker_wins(self):
        # A smaller number would hand out more rounds than the human asked for.
        comments = [{"body": forgejo.RESET_MARKER % 5}, {"body": forgejo.RESET_MARKER % 2}]
        self.assertEqual(forgejo.rounds_baseline(comments), 5)

    def test_a_stale_baseline_never_goes_negative(self):
        self.assertEqual(
            forgejo.rounds_done(self.commits(1), "pingpong-coder@local", baseline=4), 0)

"""Tests for cli_mcp.filter — command allow/deny logic."""

import pytest

from cli_mcp.filter import check_command, check_paths, glob_match


class TestGlobMatch:
    def test_wildcard_suffix(self):
        assert glob_match("ps aux", "ps *")

    def test_no_match(self):
        assert not glob_match("rm -rf /", "ps *")

    def test_case_insensitive(self):
        assert glob_match("PS AUX", "ps *")

    def test_double_wildcard_segment(self):
        assert glob_match("rm -rf /tmp/foo", "rm -rf /tmp/foo")


class TestCheckCommand:
    LINUX_RULES = {
        "deny": [
            "rm *",
            "rmdir *",
            "shutdown*",
            "reboot",
            "halt",
            "poweroff",
            "mkfs*",
            "dd *",
        ],
        "allow": [
            "ps *",
            "df *",
            "du *",
            "free *",
            "uptime",
            "cat *",
            "head *",
            "tail *",
        ],
    }

    def test_deny_takes_precedence(self):
        allowed, reason = check_command("rm -rf /tmp/foo", self.LINUX_RULES)
        assert not allowed
        assert "deny rule" in reason

    def test_allow_match(self):
        allowed, reason = check_command("ps aux", self.LINUX_RULES)
        assert allowed
        assert reason == "OK"

    def test_default_deny(self):
        allowed, reason = check_command("useradd alice", self.LINUX_RULES)
        assert not allowed
        assert "does not match any allow rule" in reason

    def test_empty_command(self):
        allowed, reason = check_command("", self.LINUX_RULES)
        assert not allowed
        assert "Empty command" in reason

    def test_whitespace_command(self):
        allowed, reason = check_command("   ", self.LINUX_RULES)
        assert not allowed

    def test_command_with_args_allowed(self):
        allowed, _ = check_command(
            'ps aux duration 3600 max-results 10',
            self.LINUX_RULES,
        )
        assert allowed

    def test_case_insensitive_deny(self):
        allowed, _ = check_command("RM -RF /TMP/FOO", self.LINUX_RULES)
        assert not allowed

    def test_case_insensitive_allow(self):
        allowed, _ = check_command("PS AUX", self.LINUX_RULES)
        assert allowed

    def test_no_rules(self):
        allowed, _ = check_command("anything", {})
        assert not allowed


class TestCheckPaths:
    def test_no_deny_passes(self):
        ok, _ = check_paths("/etc/passwd", [])
        assert ok

    def test_absolute_path_denied(self):
        ok, reason = check_paths("/etc/shadow", ["/etc/shadow"])
        assert not ok
        assert "/etc/shadow" in reason

    def test_glob_pattern(self):
        ok, _ = check_paths("/var/log/secure -n 5", ["/var/log/*"])
        assert not ok

    def test_relative_path_caught(self):
        ok, _ = check_paths("./secret.key", ["*.key"])
        assert not ok
        ok, _ = check_paths("../etc/passwd", ["../etc/*"])
        assert not ok

    def test_non_path_token_ignored(self):
        # "etcpasswd" doesn't start with /, ./, or ../ so it's not checked.
        ok, _ = check_paths("etcpasswd", ["/etc/passwd"])
        assert ok

    def test_safe_path_allowed(self):
        ok, _ = check_paths("/var/log/messages", ["/etc/shadow"])
        assert ok


class TestCheckPathsBypasses:
    """Regression tests for traversal and flag-attached path bypasses.

    Each of these returned (True, 'OK') before normalization was added — the
    deny rule was trivially defeated by respelling the same target.
    """

    DENY = ["/etc/shadow"]

    @pytest.mark.parametrize(
        "args",
        [
            "/etc/shadow",
            "//etc/shadow",
            "///etc/shadow",
            "/etc/./shadow",
            "/etc/../etc/shadow",
            "/etc/../../etc/shadow",
            "/./etc/shadow",
        ],
    )
    def test_traversal_spellings_denied(self, args):
        ok, reason = check_paths(args, self.DENY)
        assert not ok
        assert "deny rule" in reason

    @pytest.mark.parametrize(
        "args",
        [
            "--file=/etc/shadow",
            "-f/etc/shadow",
            "--file=//etc/shadow",
            "--file=/etc/../etc/shadow",
            "file=/etc/shadow",
        ],
    )
    def test_flag_attached_paths_denied(self, args):
        ok, reason = check_paths(args, self.DENY)
        assert not ok
        assert "deny rule" in reason

    def test_glob_deny_survives_traversal(self):
        ok, _ = check_paths("/var/log/../../etc/shadow", ["/etc/*"])
        assert not ok

    def test_normalization_does_not_over_deny(self):
        # Normalizing must not make unrelated paths match.
        for safe in ("/var/log/messages", "--color=auto", "-n", "/etc/hosts"):
            ok, _ = check_paths(safe, self.DENY)
            assert ok, safe

    def test_literal_spelling_still_matches_unnormalized_rule(self):
        # A rule written against a non-canonical spelling keeps working,
        # because both forms are tested.
        ok, _ = check_paths("/etc/./shadow", ["/etc/./shadow"])
        assert not ok

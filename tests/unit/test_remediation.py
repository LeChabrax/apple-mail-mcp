"""Unit tests for failure-to-remedy mapping.

The behaviour under test is not "does the code run" but "does a caller who
never read the docs learn what to do". Hence the assertions on content: the
account name has to appear, and the field has to be absent when the diagnosis
does not hold — a hint attached to every error is a hint everyone ignores.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from apple_mail_mcp import remediation as rem


class TestDiagnosis:
    @pytest.mark.parametrize(
        "error",
        [
            "Mail got an error: AppleEvent handler failed. (-10000)",
            "osascript timed out after 60s",
            "Operation TIMEOUT while reading body",
        ],
    )
    def test_recognises_slow_path_failures(self, error):
        assert rem.looks_like_missing_fast_path(error)

    @pytest.mark.parametrize(
        "error",
        [
            "Mailbox 'Archiv' not found",
            "message_not_found: 12345",
            "invalid date_from format",
            "",
        ],
    )
    def test_ignores_unrelated_failures(self, error):
        """A hint on every error trains the caller to skip the field."""
        assert not rem.looks_like_missing_fast_path(error)


class TestFastPathProbe:
    def _connector(self, password="secret", accounts=None):
        c = MagicMock()
        c.list_accounts.return_value = accounts if accounts is not None else [
            {"id": "UUID-1", "name": "Exchange", "email_addresses": ["h@lmp.com"]}
        ]
        c._get_imap_password_with_fallback.return_value = password
        return c

    def test_configured_account_reports_live(self):
        assert rem.fast_path_is_live(self._connector(), "Exchange") is True

    def test_account_matched_by_uuid_too(self):
        """Callers may pass either form, per the documented account contract."""
        assert rem.fast_path_is_live(self._connector(), "UUID-1") is True

    def test_missing_password_reports_not_live(self):
        assert rem.fast_path_is_live(self._connector(password=""), "Exchange") is False

    def test_keychain_error_reports_not_live(self):
        """An unverifiable state must show the hint, not hide it."""
        c = self._connector()
        c._get_imap_password_with_fallback.side_effect = RuntimeError("keychain locked")
        assert rem.fast_path_is_live(c, "Exchange") is False

    def test_unknown_account_reports_not_live(self):
        assert rem.fast_path_is_live(self._connector(), "Nope") is False

    def test_no_connector_reports_not_live(self):
        assert rem.fast_path_is_live(None, "Exchange") is False

    def test_no_account_falls_back_to_the_whole_machine(self):
        """Not "unknown, assume broken": see TestAccountlessTools for why."""
        assert rem.fast_path_is_live(self._connector(), None) is True


class TestSlowSuccess:
    """The case an error-only hook misses: nothing failed, everyone still loses.

    Measured 2026-09-02 on real accounts: same query, 16 s and zero results on
    an account without the fast path, 1.7 s and three results on one with it.
    """

    def _connector(self, live: bool):
        c = MagicMock()
        c.list_accounts.return_value = [
            {"id": "U", "name": "iCloud", "email_addresses": ["a@icloud.com"]}
        ]
        c._get_imap_password_with_fallback.return_value = "x" if live else ""
        return c

    def test_slow_success_gets_the_hint(self):
        out = rem.with_slow_path_remediation(
            {"success": True, "messages": [], "count": 0},
            elapsed_s=45.3,
            connector=self._connector(live=False),
            account="iCloud",
        )
        assert 'setup_imap(account="iCloud")' in out["remediation"]["fix"]

    def test_the_measured_duration_is_quoted_back(self):
        """« 45 s » is the number the user just lived through; it makes the case."""
        out = rem.with_slow_path_remediation(
            {"success": True},
            elapsed_s=45.3,
            connector=self._connector(live=False),
            account="iCloud",
        )
        assert "45 s" in out["remediation"]["problem"]

    def test_fast_success_stays_clean(self):
        out = rem.with_slow_path_remediation(
            {"success": True, "count": 3},
            elapsed_s=1.7,
            connector=self._connector(live=False),
            account="Lemediapositif",
        )
        assert "remediation" not in out

    def test_slow_but_already_configured_stays_clean(self):
        """Slow despite IMAP means a big mailbox, not a setup problem."""
        out = rem.with_slow_path_remediation(
            {"success": True},
            elapsed_s=45.0,
            connector=self._connector(live=True),
            account="iCloud",
        )
        assert "remediation" not in out

    def test_threshold_boundary(self):
        c = self._connector(live=False)
        just_under = rem.with_slow_path_remediation(
            {"success": True}, rem.SLOW_SUCCESS_SECONDS - 0.1, c, "iCloud"
        )
        at_limit = rem.with_slow_path_remediation(
            {"success": True}, rem.SLOW_SUCCESS_SECONDS, c, "iCloud"
        )
        assert "remediation" not in just_under
        assert "remediation" in at_limit


class TestAccountlessTools:
    """get_thread takes no account, and that nearly produced a false positive.

    Treating "no account given" as "not configured" would have shown the hint
    to every user whose accounts are all set up — the surest way to teach them
    to ignore it.
    """

    def _connector(self, live_names):
        c = MagicMock()
        c.list_accounts.return_value = [
            {"id": "U1", "name": "iCloud", "email_addresses": ["a@icloud.com"]},
            {"id": "U2", "name": "LMP", "email_addresses": ["a@lmp.com"]},
        ]
        c._get_imap_password_with_fallback.side_effect = (
            lambda name, email: "x" if name in live_names else ""
        )
        return c

    def test_one_configured_account_is_enough_to_stay_silent(self):
        """Measured on a real machine: LMP configured, iCloud not, 32 s thread."""
        assert rem.fast_path_is_live(self._connector({"LMP"}), None) is True

    def test_no_configured_account_shows_the_hint(self):
        assert rem.fast_path_is_live(self._connector(set()), None) is False

    def test_unlistable_accounts_show_the_hint(self):
        c = MagicMock()
        c.list_accounts.side_effect = RuntimeError("Mail not running")
        assert rem.fast_path_is_live(c, None) is False

    def test_operation_wording_follows_the_caller(self):
        """« Cette recherche » on a thread rebuild reads as a bug in the message."""
        out = rem.with_slow_path_remediation(
            {"success": True},
            33.5,
            self._connector(set()),
            None,
            "La reconstruction de ce fil",
        )
        assert out["remediation"]["problem"].startswith("La reconstruction de ce fil")

    def test_missing_account_says_where_to_find_it(self):
        """A placeholder alone leaves the caller guessing; name the lookup."""
        out = rem.with_slow_path_remediation(
            {"success": True}, 33.5, self._connector(set()), None
        )
        assert "imap_status()" in out["remediation"]["first"]

    def test_named_account_needs_no_lookup_step(self):
        out = rem.with_slow_path_remediation(
            {"success": True}, 33.5, self._connector(set()), "iCloud"
        )
        assert "first" not in out["remediation"]


class TestRemediationContent:
    def test_names_the_account(self):
        """« Lance setup_imap » est un conseil ; avec le compte, c'est une action."""
        out = rem.imap_setup_remediation("Exchange")
        assert 'setup_imap(account="Exchange")' == out["fix"]
        assert '"Exchange"' in out["cli"]

    def test_falls_back_to_a_placeholder(self):
        assert "<nom du compte>" in rem.imap_setup_remediation(None)["fix"]

    def test_states_what_the_user_must_do(self):
        """The password prompt is the one manual step; hiding it makes it a surprise."""
        out = rem.imap_setup_remediation("Exchange")
        assert "mot de passe" in out["user_action"].lower()
        assert "verify" in out


class TestAttachment:
    def _connector(self, live: bool):
        c = MagicMock()
        c.list_accounts.return_value = [
            {"id": "U", "name": "Exchange", "email_addresses": ["h@lmp.com"]}
        ]
        c._get_imap_password_with_fallback.return_value = "x" if live else ""
        return c

    def test_attaches_on_a_matching_failure(self):
        out = rem.with_remediation(
            {"success": False, "error": "AppleEvent handler failed (-10000)"},
            connector=self._connector(live=False),
            account="Exchange",
        )
        assert 'setup_imap(account="Exchange")' in out["remediation"]["fix"]

    def test_silent_when_already_configured(self):
        """Pointing at setup_imap on a configured account is a dead end."""
        out = rem.with_remediation(
            {"success": False, "error": "timed out"},
            connector=self._connector(live=True),
            account="Exchange",
        )
        assert "remediation" not in out

    def test_silent_on_an_unrelated_failure(self):
        out = rem.with_remediation(
            {"success": False, "error": "Mailbox not found"},
            connector=self._connector(live=False),
            account="Exchange",
        )
        assert "remediation" not in out

    def test_success_is_never_touched(self):
        payload = {"success": True, "messages": []}
        assert rem.with_remediation(payload, connector=self._connector(live=False)) == payload

    def test_wrapping_preserves_the_original_error(self):
        """Callers still parse error/error_type; the hint is additive."""
        out = rem.with_remediation(
            {"success": False, "error": "timed out", "error_type": "unknown"},
            connector=self._connector(live=False),
            account="Exchange",
        )
        assert out["error"] == "timed out"
        assert out["error_type"] == "unknown"
        assert out["success"] is False

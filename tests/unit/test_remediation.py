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

    def test_no_connector_or_account_reports_not_live(self):
        assert rem.fast_path_is_live(None, "Exchange") is False
        assert rem.fast_path_is_live(self._connector(), None) is False


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

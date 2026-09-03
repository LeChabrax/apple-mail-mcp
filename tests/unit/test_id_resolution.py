"""The search → read handoff: RFC ids, and saying so when a read fails.

Measured on a real account 2026-09-03: `search_messages` returned
`calendar-...@google.com`, `get_messages` answered `success: true, count: 0`.
Nothing errored. The most natural two-call sequence a caller can write was
broken, and the only available conclusion was "that message does not exist".

These tests pin the three decisions that fixed it.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from apple_mail_mcp.exceptions import MailMessageNotFoundError


@pytest.fixture
def srv():
    import apple_mail_mcp.server as server
    return server


class TestRfcIdFallback:
    def test_rfc_id_is_resolved_and_read(self, srv):
        """The bug in one test: an RFC id must not read as a missing message.

        No account here, so IMAP cannot be addressed and the AppleScript
        resolution runs — the path that still has to work on an unconfigured
        machine.
        """
        message = {"id": "269502", "subject": "Invitation"}
        mail = MagicMock()
        mail.list_accounts.return_value = []
        mail.get_message.return_value = message
        mail.find_message_by_message_id.return_value = "269502"

        with patch.object(srv, "mail", mail):
            out, missing = srv._resolve_id_list_to_messages(
                ["calendar-abc@google.com"],
                include_content=True,
                account=None,
                mailbox=None,
            )

        assert [m["id"] for m in out] == ["269502"]
        assert missing == []

    def test_lookup_skipped_without_an_at_sign(self, srv):
        """Mail's own ids are numeric; translating one would be a wasted scan."""
        mail = MagicMock()
        mail.get_message.side_effect = MailMessageNotFoundError("nope")

        with patch.object(srv, "mail", mail):
            out, missing = srv._resolve_id_list_to_messages(
                ["12345"], include_content=True, account=None, mailbox=None
            )

        mail.find_message_by_message_id.assert_not_called()
        assert out == [] and missing == ["12345"]

    def test_brackets_and_whitespace_are_stripped(self, srv):
        """Measured: search can return ' <PR3P...OUTLOOK.COM>', header-verbatim."""
        mail = MagicMock()
        mail.get_message.side_effect = [
            MailMessageNotFoundError("not found"),
            {"id": "999"},
        ]
        mail.find_message_by_message_id.return_value = "999"

        with patch.object(srv, "mail", mail):
            srv._resolve_id_list_to_messages(
                [" <PR3P195MB0490@OUTLOOK.COM>"],
                include_content=True,
                account=None,
                mailbox=None,
            )

        mail.find_message_by_message_id.assert_called_once_with(
            "PR3P195MB0490@OUTLOOK.COM"
        )

    def test_account_hints_are_not_forwarded(self, srv):
        """They re-enable the IMAP path, which matches on the RFC header — and
        by then we hold Mail's numeric id, which that path cannot find."""
        mail = MagicMock()
        mail.get_message.side_effect = [
            MailMessageNotFoundError("not found"),
            {"id": "269502"},
        ]
        mail.find_message_by_message_id.return_value = "269502"

        with patch.object(srv, "mail", mail):
            srv._resolve_id_list_to_messages(
                ["x@y.com"],
                include_content=True,
                account="Lemediapositif",
                mailbox="INBOX",
            )

        retry_kwargs = mail.get_message.call_args_list[1].kwargs
        assert "account" not in retry_kwargs
        assert "mailbox" not in retry_kwargs

    def test_unresolvable_id_is_reported_missing(self, srv):
        mail = MagicMock()
        mail.get_message.side_effect = MailMessageNotFoundError("not found")
        mail.find_message_by_message_id.return_value = None

        with patch.object(srv, "mail", mail):
            out, missing = srv._resolve_id_list_to_messages(
                ["ghost@nowhere.com"],
                include_content=True,
                account=None,
                mailbox=None,
            )

        assert out == [] and missing == ["ghost@nowhere.com"]


class TestImapIsPreferred:
    """Reading over IMAP instead of driving Mail message by message.

    Measured on a real machine (11 accounts, 270 mailboxes): the AppleScript
    resolution costs 27.8 s for ONE targeted mailbox and times out at 60 s over
    the whole set, while the IMAP read of the same message takes 1.0 s. Same
    three ids end to end: 236.8 s and nothing read, against 3.2 s and all three.
    """

    def test_rfc_id_skips_the_doomed_applescript_lookup(self, srv):
        """A lookup that cannot match must not be paid for."""
        mail = MagicMock()
        mail.get_message.return_value = {"id": "269502"}

        with patch.object(srv, "mail", mail):
            out, missing = srv._resolve_id_list_to_messages(
                ["calendar-abc@google.com"],
                include_content=True,
                account="LMP",
                mailbox="INBOX",
            )

        assert [m["id"] for m in out] == ["269502"] and missing == []
        # One call, straight to IMAP — not a failed pass then a retry.
        assert mail.get_message.call_count == 1
        assert mail.get_message.call_args.kwargs["account"] == "LMP"

    def test_numeric_id_still_uses_the_direct_path(self, srv):
        """Mail's own ids resolve there; sending them to IMAP would be wrong."""
        mail = MagicMock()
        mail.get_message.return_value = {"id": "269502"}

        with patch.object(srv, "mail", mail):
            srv._resolve_id_list_to_messages(
                ["269502"], include_content=True, account="LMP", mailbox="INBOX"
            )

        mail.find_message_by_message_id.assert_not_called()

    def test_inbox_assumed_when_no_mailbox_given(self, srv):
        mail = MagicMock()
        mail.get_message.return_value = {"id": "1"}

        with patch.object(srv, "mail", mail):
            srv._resolve_id_list_to_messages(
                ["x@y.com"], include_content=True, account="LMP", mailbox=None
            )

        assert mail.get_message.call_args.kwargs["mailbox"] == "INBOX"

    def test_named_account_is_tried_alone(self, srv):
        assert srv._accounts_to_try("LMP") == ["LMP"]

    def test_candidate_accounts_are_computed_once(self, srv):
        """Probing every keychain entry costs 28.9 s; it must not repeat."""
        mail = MagicMock()
        mail.list_accounts.return_value = [{"name": "LMP"}, {"name": "Other"}]

        with patch.object(srv, "mail", mail), patch.object(
            srv, "_IMAP_ACCOUNTS_CACHE", None
        ), patch(
            "apple_mail_mcp.remediation.fast_path_is_live", return_value=True
        ):
            srv._accounts_to_try(None)
            srv._accounts_to_try(None)
            srv._accounts_to_try(None)

        assert mail.list_accounts.call_count == 1

    def test_recent_account_is_tried_first(self, srv):
        """Each miss is a full IMAP round-trip, so order decides the cost."""
        mail = MagicMock()
        mail.list_accounts.return_value = [{"name": "Other"}, {"name": "LMP"}]

        with patch.object(srv, "mail", mail), patch.object(
            srv, "_IMAP_ACCOUNTS_CACHE", None
        ), patch.object(srv, "_ACCOUNT_MRU", []), patch(
            "apple_mail_mcp.remediation.fast_path_is_live", return_value=True
        ):
            srv._remember_account("LMP")
            assert srv._accounts_to_try(None)[0] == "LMP"

    def test_account_memory_is_bounded(self, srv):
        with patch.object(srv, "_ACCOUNT_MRU", []):
            for i in range(20):
                srv._remember_account(f"acct{i}")
            assert len(srv._ACCOUNT_MRU) <= 8
            assert srv._ACCOUNT_MRU[-1] == "acct19"


class TestAttachmentDegradation:
    def test_attachment_failure_still_returns_the_message(self, srv):
        """Separate bug, measured on one real message: include_attachments=True
        raises "not found" where False returns it. The body is what was asked
        for; reporting an existing message as missing is the worse answer."""
        mail = MagicMock()
        mail.list_accounts.return_value = []
        mail.get_message.side_effect = [
            MailMessageNotFoundError("not found"),   # with attachments
            {"id": "269502", "subject": "OK"},        # without
        ]
        mail.find_message_by_message_id.return_value = "269502"

        with patch.object(srv, "mail", mail):
            out, missing = srv._resolve_id_list_to_messages(
                ["x@y.com"],
                include_content=True,
                account=None,
                mailbox=None,
                include_attachments=True,
            )

        assert [m["id"] for m in out] == ["269502"]
        assert missing == []
        assert mail.get_message.call_args_list[-1].kwargs["include_attachments"] is False

    def test_no_second_attempt_when_attachments_were_not_asked_for(self, srv):
        """Without attachments there is nothing to degrade: fail honestly."""
        mail = MagicMock()
        mail.list_accounts.return_value = []
        mail.get_message.side_effect = MailMessageNotFoundError("not found")
        mail.find_message_by_message_id.return_value = "269502"

        with patch.object(srv, "mail", mail):
            out, missing = srv._resolve_id_list_to_messages(
                ["x@y.com"],
                include_content=True,
                account=None,
                mailbox=None,
                include_attachments=False,
            )

        assert out == [] and missing == ["x@y.com"]
        assert mail.get_message.call_count == 1


class TestPartialResultsAreNamed:
    def test_unreadable_ids_surface_in_the_response(self, srv):
        """Dropping ids silently is what made an id mismatch look like
        "this message does not exist"."""
        mail = MagicMock()
        mail.get_message.side_effect = [
            {"id": "1", "subject": "ok"},
            MailMessageNotFoundError("not found"),
        ]
        mail.find_message_by_message_id.return_value = None
        get = getattr(srv.get_messages, "fn", srv.get_messages)

        with patch.object(srv, "mail", mail):
            out = get(["1", "ghost@nowhere.com"])

        assert out["count"] == 1
        assert out["unreadable_ids"] == ["ghost@nowhere.com"]
        assert "note" in out

    def test_a_clean_read_carries_no_noise(self, srv):
        mail = MagicMock()
        mail.get_message.return_value = {"id": "1", "subject": "ok"}
        get = getattr(srv.get_messages, "fn", srv.get_messages)

        with patch.object(srv, "mail", mail):
            out = get(["1"])

        assert out["count"] == 1
        assert "unreadable_ids" not in out
        assert "note" not in out

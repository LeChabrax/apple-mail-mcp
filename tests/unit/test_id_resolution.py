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
        """The bug in one test: an RFC id must not read as a missing message."""
        message = {"id": "269502", "subject": "Invitation"}
        mail = MagicMock()
        mail.get_message.side_effect = [
            MailMessageNotFoundError("not found (-2700)"),
            message,
        ]
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


class TestAttachmentDegradation:
    def test_attachment_failure_still_returns_the_message(self, srv):
        """Separate bug, measured on one real message: include_attachments=True
        raises "not found" where False returns it. The body is what was asked
        for; reporting an existing message as missing is the worse answer."""
        mail = MagicMock()
        mail.get_message.side_effect = [
            MailMessageNotFoundError("not found"),   # direct lookup
            MailMessageNotFoundError("not found"),   # retry, with attachments
            {"id": "269502", "subject": "OK"},        # retry, without
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
        assert mail.get_message.call_args_list[2].kwargs["include_attachments"] is False

    def test_no_second_attempt_when_attachments_were_not_asked_for(self, srv):
        """Without attachments there is nothing to degrade: fail honestly."""
        mail = MagicMock()
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
        assert mail.get_message.call_count == 2


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

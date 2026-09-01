"""Unit tests for smart mailbox plist manipulation.

These tests matter more than most in this repo: every other mutating operation
goes through Mail.app, which validates its own input. This one writes a file
Mail parses at launch, and a malformed write makes Mail drop every smart
mailbox the user ever made, silently. So the safety gates (Mail running,
backup, plutil validation, preserving foreign entries) are tested as behaviour,
not as implementation detail.
"""
from __future__ import annotations

import plistlib
import subprocess
from unittest.mock import patch

import pytest

from apple_mail_mcp import smart_mailboxes as sm


@pytest.fixture
def plist_file(tmp_path):
    return tmp_path / "SyncedSmartMailboxes.plist"


@pytest.fixture
def mail_quit():
    """Pretend Mail is closed. Every write test needs this."""
    with patch.object(sm, "mail_is_running", return_value=False):
        yield


class TestBuildCriterion:
    def test_leaf_defaults_to_contains(self):
        crit = sm.build_criterion({"field": "from", "value": "acme.com"})
        assert crit["Header"] == "From"
        assert crit["Expression"] == "acme.com"
        assert crit["Qualifier"] == "Contains"
        assert crit["CriterionUniqueId"]

    def test_each_criterion_gets_a_unique_id(self):
        a = sm.build_criterion({"field": "from", "value": "x.com"})
        b = sm.build_criterion({"field": "from", "value": "x.com"})
        assert a["CriterionUniqueId"] != b["CriterionUniqueId"]

    def test_compound_nests_children(self):
        crit = sm.build_criterion(
            {
                "all": False,
                "criteria": [
                    {"field": "from", "value": "a.com"},
                    {"field": "from", "value": "b.com"},
                ],
            }
        )
        assert crit["Header"] == "Compound"
        assert crit["AllCriteriaMustBeSatisfied"] is False
        assert len(crit["Criteria"]) == 2

    def test_compound_nests_recursively(self):
        """AND inside OR: the whole point of building the plist by hand."""
        crit = sm.build_criterion(
            {
                "all": False,
                "criteria": [
                    {"field": "from", "value": "a.com"},
                    {
                        "all": True,
                        "criteria": [
                            {"field": "from", "value": "agency.com"},
                            {"field": "subject", "value": "Acme"},
                        ],
                    },
                ],
            }
        )
        inner = crit["Criteria"][1]
        assert inner["Header"] == "Compound"
        assert inner["AllCriteriaMustBeSatisfied"] is True
        assert len(inner["Criteria"]) == 2

    @pytest.mark.parametrize(
        "spec",
        [
            {"field": "nope", "value": "x"},
            {"field": "from", "value": ""},
            {"field": "from"},
            {"field": "from", "value": "x", "operator": "sorta_matches"},
            {"criteria": []},
        ],
    )
    def test_invalid_specs_are_refused(self, spec):
        with pytest.raises(sm.SmartMailboxError):
            sm.build_criterion(spec)


class TestBuildSmartMailbox:
    def test_shape_matches_mail_schema(self):
        mb = sm.build_smart_mailbox("Acme", [{"field": "from", "value": "acme.com"}])
        assert mb["MailboxName"] == "Acme"
        assert mb["MailboxType"] == 7
        assert mb["MailboxChildren"] == []
        assert mb["MailboxID"]

    def test_omit_criteria_added_by_default(self):
        mb = sm.build_smart_mailbox("Acme", [{"field": "from", "value": "acme.com"}])
        headers = {c.get("Header") for c in mb["MailboxCriteria"]}
        assert {"NotInJunkMailbox", "NotInTrashMailbox", "NotInASpecialMailbox"} <= headers

    def test_omit_criteria_can_be_disabled(self):
        mb = sm.build_smart_mailbox(
            "Acme",
            [{"field": "from", "value": "acme.com"}],
            omit_junk_trash_sent=False,
        )
        assert len(mb["MailboxCriteria"]) == 1

    def test_any_logic_does_not_leak_into_omit_criteria(self):
        """The regression this wrapper exists for.

        With match_logic='any' and a flat criteria list, "omit junk" would
        become one of the OR branches, so junk mail matching nothing else would
        still land in the mailbox.
        """
        mb = sm.build_smart_mailbox(
            "Acme",
            [{"field": "from", "value": "a.com"}, {"field": "from", "value": "b.com"}],
            match_logic="any",
        )
        assert mb["MailboxAllCriteriaMustBeSatisfied"] is True
        user_block = mb["MailboxCriteria"][0]
        assert user_block["Name"] == "user criteria"
        assert user_block["AllCriteriaMustBeSatisfied"] is False

    @pytest.mark.parametrize(
        "name,criteria,logic",
        [
            ("", [{"field": "from", "value": "x"}], "all"),
            ("   ", [{"field": "from", "value": "x"}], "all"),
            ("Acme", [], "all"),
            ("Acme", [{"field": "from", "value": "x"}], "maybe"),
        ],
    )
    def test_invalid_input_refused(self, name, criteria, logic):
        with pytest.raises(sm.SmartMailboxError):
            sm.build_smart_mailbox(name, criteria, match_logic=logic)


class TestReadWrite:
    def test_missing_file_reads_as_empty(self, plist_file):
        assert sm.read_smart_mailboxes(plist_file) == []

    def test_legacy_dict_wrapper_is_understood(self, plist_file):
        with open(plist_file, "wb") as fh:
            plistlib.dump({"version": 1, "mailboxes": [{"MailboxName": "Old"}]}, fh)
        assert sm.read_smart_mailboxes(plist_file)[0]["MailboxName"] == "Old"

    def test_unexpected_root_type_refuses_rather_than_guessing(self, plist_file):
        with open(plist_file, "wb") as fh:
            plistlib.dump({"unrelated": True}, fh)
        with pytest.raises(sm.SmartMailboxError):
            sm.read_smart_mailboxes(plist_file)

    def test_roundtrip_is_lossless(self, plist_file, mail_quit):
        mb = sm.build_smart_mailbox("Acme", [{"field": "from", "value": "acme.com"}])
        sm.write_smart_mailboxes([mb], plist_file)
        assert sm.read_smart_mailboxes(plist_file) == [mb]

    def test_written_file_passes_plutil(self, plist_file, mail_quit):
        mb = sm.build_smart_mailbox("Acme", [{"field": "from", "value": "acme.com"}])
        sm.write_smart_mailboxes([mb], plist_file)
        assert subprocess.run(["plutil", "-lint", str(plist_file)]).returncode == 0

    def test_existing_entries_survive_an_append(self, plist_file, mail_quit):
        """Foreign entries must come back byte-identical: we did not author them."""
        foreign = {"MailboxName": "Made in the UI", "MailboxID": "X", "Custom": [1, 2]}
        with open(plist_file, "wb") as fh:
            plistlib.dump([foreign], fh)

        mine = sm.build_smart_mailbox("Acme", [{"field": "from", "value": "acme.com"}])
        sm.write_smart_mailboxes(sm.read_smart_mailboxes(plist_file) + [mine], plist_file)

        result = sm.read_smart_mailboxes(plist_file)
        assert result[0] == foreign
        assert len(result) == 2

    def test_backup_taken_before_overwrite(self, plist_file, mail_quit):
        with open(plist_file, "wb") as fh:
            plistlib.dump([{"MailboxName": "Before"}], fh)

        out = sm.write_smart_mailboxes([], plist_file)

        assert out["backup"] is not None
        with open(out["backup"], "rb") as fh:
            assert plistlib.load(fh)[0]["MailboxName"] == "Before"

    def test_no_backup_when_there_was_no_file(self, plist_file, mail_quit):
        assert sm.write_smart_mailboxes([], plist_file)["backup"] is None


class TestSafetyGates:
    def test_write_refused_while_mail_runs(self, plist_file):
        with patch.object(sm, "mail_is_running", return_value=True):
            with pytest.raises(sm.MailIsRunningError):
                sm.write_smart_mailboxes([], plist_file)

    def test_refusal_leaves_no_trace(self, plist_file):
        """No backup, no temp file: a rejected call must be a no-op."""
        with patch.object(sm, "mail_is_running", return_value=True):
            with pytest.raises(sm.MailIsRunningError):
                sm.write_smart_mailboxes([], plist_file)
        assert list(plist_file.parent.iterdir()) == []

    def test_unverifiable_mail_state_is_not_treated_as_closed(self):
        with patch.object(sm.subprocess, "run", side_effect=OSError("nope")):
            with pytest.raises(sm.SmartMailboxError):
                sm.mail_is_running()

    def test_lint_failure_leaves_original_intact(self, plist_file, mail_quit):
        with open(plist_file, "wb") as fh:
            plistlib.dump([{"MailboxName": "Original"}], fh)

        failed = subprocess.CompletedProcess([], 1, b"", b"bad plist")
        with patch.object(sm.subprocess, "run", return_value=failed):
            with pytest.raises(sm.SmartMailboxError, match="invalide"):
                sm.write_smart_mailboxes([], plist_file)

        assert sm.read_smart_mailboxes(plist_file)[0]["MailboxName"] == "Original"

    def test_temp_file_removed_on_failure(self, plist_file, mail_quit):
        failed = subprocess.CompletedProcess([], 1, b"", b"bad plist")
        with patch.object(sm.subprocess, "run", return_value=failed):
            with pytest.raises(sm.SmartMailboxError):
                sm.write_smart_mailboxes([], plist_file)
        assert not list(plist_file.parent.glob("*.tmp-*"))


class TestPlistPath:
    def test_highest_version_wins(self, tmp_path):
        for v in ("V2", "V9", "V10"):
            (tmp_path / v / "MailData").mkdir(parents=True)
        assert "V10" in str(sm.plist_path(tmp_path))

    def test_missing_root_is_an_error(self, tmp_path):
        with pytest.raises(sm.SmartMailboxError):
            sm.plist_path(tmp_path / "absent")

    def test_no_version_dir_is_an_error(self, tmp_path):
        with pytest.raises(sm.SmartMailboxError):
            sm.plist_path(tmp_path)


class TestDescribe:
    def test_reports_user_logic_not_mailbox_logic(self):
        """MailboxAllCriteriaMustBeSatisfied is always True; reporting it would
        tell every caller 'all', including for mailboxes built with 'any'."""
        mb = sm.build_smart_mailbox(
            "Acme",
            [{"field": "from", "value": "a.com"}, {"field": "from", "value": "b.com"}],
            match_logic="any",
        )
        assert sm.describe(mb)["match_logic"] == "any"

    def test_omit_plumbing_hidden_from_summary(self):
        mb = sm.build_smart_mailbox("Acme", [{"field": "from", "value": "acme.com"}])
        lines = sm.describe(mb)["criteria"]
        assert lines == ["from contains 'acme.com'"]

    def test_nested_group_is_readable(self):
        mb = sm.build_smart_mailbox(
            "Acme",
            [
                {
                    "all": False,
                    "criteria": [
                        {"field": "from", "value": "a.com"},
                        {"field": "from", "value": "b.com"},
                    ],
                }
            ],
        )
        lines = sm.describe(mb)["criteria"]
        assert lines[0] == "(OU)"
        assert "a.com" in lines[1]

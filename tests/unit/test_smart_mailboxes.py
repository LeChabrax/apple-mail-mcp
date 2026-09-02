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


class TestICloudMirror:
    """Regression cover for the failure only a real Mail relaunch revealed.

    Writing the local file alone returns success, reads back correctly, and is
    silently discarded when Mail next starts. Every one of these tests would
    have passed before the fix if they only checked the local path — so they
    assert on the iCloud path specifically.
    """

    def test_both_files_written_by_default(self, tmp_path, mail_quit):
        icloud = tmp_path / "icloud" / "SyncedSmartMailboxes.plist"
        local = tmp_path / "local" / "SyncedSmartMailboxes.plist"
        with patch.object(sm, "icloud_plist_path", return_value=icloud), patch.object(
            sm, "plist_path", return_value=local
        ):
            out = sm.write_smart_mailboxes(
                [sm.build_smart_mailbox("A", [{"field": "from", "value": "a.com"}])]
            )

        assert icloud.exists() and local.exists()
        assert out["paths"] == [str(icloud), str(local)]

    def test_icloud_written_first(self, tmp_path, mail_quit):
        """The authoritative copy must be correct even if the second write dies."""
        icloud = tmp_path / "icloud" / "SyncedSmartMailboxes.plist"
        local = tmp_path / "local" / "SyncedSmartMailboxes.plist"
        with patch.object(sm, "icloud_plist_path", return_value=icloud), patch.object(
            sm, "plist_path", return_value=local
        ):
            assert sm.write_smart_mailboxes([])["path"] == str(icloud)

    def test_local_only_when_mail_not_in_icloud(self, tmp_path, mail_quit):
        local = tmp_path / "local" / "SyncedSmartMailboxes.plist"
        with patch.object(sm, "icloud_plist_path", return_value=None), patch.object(
            sm, "plist_path", return_value=local
        ):
            out = sm.write_smart_mailboxes([])
        assert out["paths"] == [str(local)]

    def test_explicit_path_writes_only_that_file(self, tmp_path, mail_quit):
        """A named target is an instruction, not a hint: no surprise second write."""
        explicit = tmp_path / "explicit.plist"
        icloud = tmp_path / "icloud" / "SyncedSmartMailboxes.plist"
        with patch.object(sm, "icloud_plist_path", return_value=icloud):
            out = sm.write_smart_mailboxes([], explicit)
        assert out["paths"] == [str(explicit)]
        assert not icloud.exists()

    def test_read_prefers_icloud(self, tmp_path):
        """Reading the mirror could list mailboxes Mail is about to discard."""
        icloud = tmp_path / "icloud" / "SyncedSmartMailboxes.plist"
        icloud.parent.mkdir()
        with open(icloud, "wb") as fh:
            plistlib.dump([{"MailboxName": "FromICloud"}], fh)

        local = tmp_path / "local" / "SyncedSmartMailboxes.plist"
        local.parent.mkdir()
        with open(local, "wb") as fh:
            plistlib.dump([{"MailboxName": "FromLocal"}], fh)

        with patch.object(sm, "icloud_plist_path", return_value=icloud), patch.object(
            sm, "plist_path", return_value=local
        ):
            assert sm.read_smart_mailboxes()[0]["MailboxName"] == "FromICloud"

    def test_icloud_path_none_without_container(self, tmp_path):
        with patch.object(sm.Path, "home", return_value=tmp_path):
            assert sm.icloud_plist_path() is None

    def test_icloud_path_picks_highest_version(self, tmp_path):
        container = tmp_path / "Library" / "Mobile Documents" / "com~apple~mail" / "Data"
        for v in ("V2", "V4"):
            (container / v).mkdir(parents=True)
        with patch.object(sm.Path, "home", return_value=tmp_path):
            assert "V4" in str(sm.icloud_plist_path())


class TestHierarchy:
    """Folders (type 8) vs mailboxes (type 7), and finding things inside them."""

    def _mb(self, name="Acme", value="acme.com"):
        return sm.build_smart_mailbox(name, [{"field": "from", "value": value}])

    def test_no_parent_lands_at_root(self):
        tree = sm.insert_mailbox([], self._mb())
        assert len(tree) == 1
        assert not sm.is_folder(tree[0])

    def test_missing_parent_is_created(self):
        """Filing ten clients in a loop must not make the first one special."""
        tree = sm.insert_mailbox([], self._mb(), parent="Clients")
        assert sm.is_folder(tree[0])
        assert tree[0]["MailboxName"] == "Clients"
        assert tree[0]["MailboxChildren"][0]["MailboxName"] == "Acme"

    def test_existing_parent_is_reused(self):
        tree = sm.insert_mailbox([], self._mb("Acme", "acme.com"), parent="Clients")
        tree = sm.insert_mailbox(tree, self._mb("Bayard", "bayard.com"), parent="Clients")
        assert len(tree) == 1
        assert len(tree[0]["MailboxChildren"]) == 2

    def test_folder_carries_no_criteria(self):
        """A folder that matched something would double every client's mail."""
        folder = sm.build_folder("Clients")
        assert "MailboxCriteria" not in folder
        assert folder["MailboxType"] == 8

    def test_smart_mailbox_refused_as_parent(self):
        tree = [self._mb("Acme", "acme.com")]
        with pytest.raises(sm.SmartMailboxError, match="pas un dossier"):
            sm.insert_mailbox(tree, self._mb("Sub", "sub.com"), parent="Acme")

    def test_empty_folder_name_refused(self):
        with pytest.raises(sm.SmartMailboxError):
            sm.build_folder("  ")

    def test_find_reaches_into_folders(self):
        tree = sm.insert_mailbox([], self._mb("Bayard", "bayard.com"), parent="Clients")
        entry, siblings, matches = sm.find_mailbox(tree, name="Bayard")
        assert entry["MailboxName"] == "Bayard"
        assert siblings is tree[0]["MailboxChildren"]
        assert len(matches) == 1

    def test_find_reports_every_namesake(self):
        """Deleting by an ambiguous name must be refusable, so all hits surface."""
        tree = sm.insert_mailbox([], self._mb("Acme", "a.com"), parent="Clients")
        tree = sm.insert_mailbox(tree, self._mb("Acme", "b.com"))
        _, _, matches = sm.find_mailbox(tree, name="Acme")
        assert len(matches) == 2

    def test_find_returns_none_when_absent(self):
        entry, _, matches = sm.find_mailbox([], name="Nope")
        assert entry is None and matches == []

    def test_siblings_list_allows_removal_at_depth(self):
        tree = sm.insert_mailbox([], self._mb("Acme", "acme.com"), parent="Clients")
        entry, siblings, _ = sm.find_mailbox(tree, name="Acme")
        siblings.remove(entry)
        assert tree[0]["MailboxChildren"] == []

    def test_count_includes_nested_entries(self):
        """After filing 2 clients in a folder the user expects 3, not 1."""
        tree = sm.insert_mailbox([], self._mb("A", "a.com"), parent="Clients")
        tree = sm.insert_mailbox(tree, self._mb("B", "b.com"), parent="Clients")
        assert sm.count_entries(tree) == 3

    def test_describe_renders_a_folder_with_its_children(self):
        tree = sm.insert_mailbox([], self._mb("Acme", "acme.com"), parent="Clients")
        out = sm.describe(tree[0])
        assert out["type"] == "folder"
        assert out["children"][0]["name"] == "Acme"
        assert out["children"][0]["type"] == "smart_mailbox"

    def test_insert_does_not_mutate_the_caller_list(self):
        original = [self._mb("Acme", "acme.com")]
        sm.insert_mailbox(original, self._mb("B", "b.com"), parent="Clients")
        assert len(original) == 1


class TestEdit:
    def test_rename_keeps_the_id(self):
        """The id is what a caller stored to find this mailbox again."""
        mb = sm.build_smart_mailbox("Acme", [{"field": "from", "value": "acme.com"}])
        before = mb["MailboxID"]
        sm.rename_mailbox(mb, "Acme Group")
        assert mb["MailboxName"] == "Acme Group"
        assert mb["MailboxID"] == before

    def test_rename_refuses_empty(self):
        mb = sm.build_smart_mailbox("Acme", [{"field": "from", "value": "acme.com"}])
        with pytest.raises(sm.SmartMailboxError):
            sm.rename_mailbox(mb, "   ")

    def test_replace_criteria_keeps_identity_and_children(self):
        mb = sm.build_smart_mailbox("Acme", [{"field": "from", "value": "old.com"}])
        mb["MailboxChildren"] = [{"MailboxName": "keep me"}]
        before = (mb["MailboxID"], mb["MailboxName"])

        sm.replace_criteria(mb, [{"field": "from", "value": "new.com"}])

        assert (mb["MailboxID"], mb["MailboxName"]) == before
        assert mb["MailboxChildren"] == [{"MailboxName": "keep me"}]
        assert sm.describe(mb)["criteria"] == ["from contains 'new.com'"]

    def test_replace_criteria_updates_logic(self):
        mb = sm.build_smart_mailbox("Acme", [{"field": "from", "value": "a.com"}])
        sm.replace_criteria(
            mb,
            [{"field": "from", "value": "a.com"}, {"field": "from", "value": "b.com"}],
            match_logic="any",
        )
        assert sm.describe(mb)["match_logic"] == "any"

    def test_replace_criteria_refuses_empty(self):
        mb = sm.build_smart_mailbox("Acme", [{"field": "from", "value": "a.com"}])
        with pytest.raises(sm.SmartMailboxError):
            sm.replace_criteria(mb, [])


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

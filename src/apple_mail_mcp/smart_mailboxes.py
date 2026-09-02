"""Smart mailboxes (boîtes intelligentes) via direct plist manipulation.

WHY THIS EXISTS
---------------
Smart mailboxes are the one Mail.app feature with no AppleScript surface at all
(``docs/reference/APPLESCRIPT_GOTCHAS.md``: "Smart mailbox access — Not exposed
to AppleScript"). Every other mailbox operation in this server goes through
``mail_connector``; this one cannot. The definitions live in a plist that Mail
reads once at launch:

    ~/Library/Mail/V<N>/MailData/SyncedSmartMailboxes.plist

So the only way to create one programmatically is to edit that file. That makes
this module structurally riskier than the rest of the server, and the safety
rules below are not optional decoration.

WHY SMART MAILBOXES RATHER THAN RULES
-------------------------------------
A rule with ``move_to`` empties the inbox; a rule with ``copy_to`` duplicates
every message and doubles the mailbox quota. A smart mailbox is a saved search:
the message stays exactly where it is, in the inbox, and the folder is a live
view over it. For "I want to see everything in my inbox AND have one folder per
client", it is the only mechanism that satisfies both halves.

HARD SAFETY RULES (each one earned)
-----------------------------------
1. **Mail must be quit.** Mail holds this file in memory and rewrites it on
   quit; writing underneath a running Mail loses the edit at best, corrupts the
   file at worst. We refuse rather than race.
2. **Backup before every write.** Timestamped copy next to the original. A bad
   write costs the user every smart mailbox they ever made.
3. **Validate before installing.** The new plist is written to a temp file and
   checked with ``plutil -lint``; the real file is only replaced once the temp
   one parses. An invalid plist makes Mail drop all smart mailboxes silently.
4. **Never rewrite entries we did not create.** Existing mailboxes are parsed
   and re-serialised untouched; unknown keys are preserved verbatim.
5. **Write the iCloud copy too.** Measured on a real machine: writing only the
   local file looks like it worked and is thrown away at the next Mail launch.
   See ``icloud_plist_path``.

PLIST SCHEMA (reverse-engineered, no public Apple documentation)
----------------------------------------------------------------
Root is an ``<array>`` of mailbox dicts. Each dict:

    MailboxName                       str    display name
    MailboxID                         str    UUID, must be unique
    MailboxType                       int    7 for a top-level smart mailbox
    MailboxAllCriteriaMustBeSatisfied bool   AND across MailboxCriteria
    MailboxChildren                   array  nesting, we emit []
    MailboxCriteria                   array  criterion dicts

A criterion is either a leaf::

    {CriterionUniqueId: UUID, Header: "From", Expression: "acme.com",
     Qualifier: "Contains"}

or a compound node grouping others::

    {CriterionUniqueId: UUID, Header: "Compound", AllCriteriaMustBeSatisfied: bool,
     Criteria: [ ...leaves or compounds... ], Name: "user criteria"}

Compounds nest, which is how this module expresses AND/OR trees that the Mail
UI itself cannot build (the UI offers a single flat any/all).

The three "omit" criteria (junk / trash / sent) correspond to the checkboxes in
the Mail UI and are added by default, because a client folder that also matches
the user's own sent mail and their trash is not what anyone means by "mail from
this client".
"""
from __future__ import annotations

import logging
import os
import plistlib
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Mail.app rewrites this file on quit; see rule 1 above.
_PLIST_NAME = "SyncedSmartMailboxes.plist"

# MailboxType 7 = a smart mailbox. Type 8 is a *folder* of smart mailboxes: it
# carries no criteria of its own and exists only to hold MailboxChildren. That
# distinction is what makes "Clients" a real group in the sidebar rather than a
# mailbox whose name happens to contain a slash.
_MAILBOX_TYPE_SMART = 7
_MAILBOX_TYPE_FOLDER = 8

# Observed on real installations. Mail recomputes these flags itself, so the
# value only has to be plausible on first read.
_IMAP_MAILBOX_ATTRIBUTES = 17

# Leaf criteria we know how to build. Mail understands more (attachments,
# priority, group membership); the ones below are the text and address headers
# that a per-client or per-topic mailbox actually needs.
FIELDS = {
    "from": "From",
    "to": "To",
    "cc": "Cc",
    "subject": "Subject",
    "body": "Body",
    "any_recipient": "AnyRecipient",
}

# Mail's own qualifier spellings. "Contains" is the default because it is what
# a domain match wants: "acme.com" should match "sales@acme.com".
QUALIFIERS = {
    "contains": "Contains",
    "does_not_contain": "DoesNotContain",
    "begins_with": "BeginsWith",
    "ends_with": "EndsWith",
    "equals": "IsEqualTo",
    "not_equals": "IsNotEqualTo",
}

_SPECIAL_MAILBOX_SENT = 3


class SmartMailboxError(RuntimeError):
    """Any failure that must abort before touching the real plist."""


class MailIsRunningError(SmartMailboxError):
    """Mail.app is running: it would overwrite our edit on quit."""


def mail_is_running() -> bool:
    """True when Mail.app has a live process.

    ``pgrep -x`` matches the exact process name, so "Mail Assistant" or a user
    file named Mail does not count as Mail.app.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-x", "Mail"], capture_output=True, timeout=5
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError) as exc:
        # Unable to tell. Claiming "not running" here would let us write
        # underneath a live Mail, which is the exact failure this guards.
        raise SmartMailboxError(
            "Impossible de vérifier si Mail est en cours d'exécution"
        ) from exc


def plist_path(mail_root: Path | None = None) -> Path:
    """Locate SyncedSmartMailboxes.plist for the current Mail data version.

    The version directory (V10 on current macOS) changes across major releases,
    so it is discovered rather than hardcoded; the highest version wins when
    an upgrade left older ones behind.
    """
    root = mail_root or Path.home() / "Library" / "Mail"
    if not root.is_dir():
        raise SmartMailboxError(f"Dossier Mail introuvable : {root}")

    versions = sorted(
        (p for p in root.iterdir() if p.is_dir() and p.name.startswith("V")),
        key=lambda p: _version_sort_key(p.name),
        reverse=True,
    )
    if not versions:
        raise SmartMailboxError(f"Aucun dossier de version Mail (V*) dans {root}")

    return versions[0] / "MailData" / _PLIST_NAME


def icloud_plist_path() -> Path | None:
    """The iCloud copy, which outranks the local one. None when not synced.

    MEASURED, 2026-09-01, and the reason this function exists: writing only the
    local file silently loses the edit. A smart mailbox written to
    ``~/Library/Mail/V10/MailData/`` was gone after relaunching Mail — the file
    was back to an empty array — while the same content written to BOTH paths
    survived. The "Synced" in the filename is literal: with Mail in iCloud, the
    local file is a mirror that Mail overwrites from
    ``~/Library/Mobile Documents/com~apple~mail/Data/V*/`` at launch.

    This is invisible without relaunching Mail: the local write succeeds, the
    file reads back correctly, and every check passes right up until Mail
    starts and throws it away.

    Returns None when the container is absent (Mail not in iCloud), which is a
    legitimate configuration, not an error: the local file is then authoritative.
    """
    container = Path.home() / "Library" / "Mobile Documents" / "com~apple~mail" / "Data"
    if not container.is_dir():
        return None

    versions = sorted(
        (p for p in container.iterdir() if p.is_dir() and p.name.startswith("V")),
        key=lambda p: _version_sort_key(p.name),
        reverse=True,
    )
    if not versions:
        return None

    return versions[0] / _PLIST_NAME


def _version_sort_key(name: str) -> int:
    """V10 must sort above V9, so compare numerically and not as text."""
    try:
        return int(name[1:])
    except ValueError:
        return -1


def read_smart_mailboxes(path: Path | None = None) -> list[dict[str, Any]]:
    """Parse the plist. A missing file means "no smart mailboxes yet", not an error.

    Reads the iCloud copy when there is one, because that is the version Mail
    will keep: reading the local mirror could report mailboxes that Mail is
    about to discard at its next launch.
    """
    if path is None:
        path = icloud_plist_path()
    target = path or plist_path()
    if not target.exists():
        return []

    try:
        with open(target, "rb") as fh:
            data = plistlib.load(fh)
    except Exception as exc:
        raise SmartMailboxError(f"Plist illisible ({target}) : {exc}") from exc

    if data is None:
        return []
    if isinstance(data, list):
        return data
    # Pre-El Capitan layout: a dict wrapping the array under "mailboxes".
    if isinstance(data, dict) and isinstance(data.get("mailboxes"), list):
        return data["mailboxes"]

    raise SmartMailboxError(
        f"Format de plist inattendu ({type(data).__name__}), écriture refusée"
    )


def _backup(target: Path) -> Path | None:
    """Timestamped copy beside the original. None when there was nothing to save."""
    if not target.exists():
        return None
    backup = target.with_suffix(f".plist.backup-{int(time.time())}")
    shutil.copy2(target, backup)
    logger.info("Sauvegarde du plist : %s", backup)
    return backup


def _write_one(mailboxes: list[dict[str, Any]], target: Path) -> Path | None:
    """Validate then atomically replace a single plist. Returns its backup path."""
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = _backup(target)

    tmp = target.with_suffix(f".plist.tmp-{os.getpid()}")
    try:
        with open(tmp, "wb") as fh:
            plistlib.dump(mailboxes, fh, fmt=plistlib.FMT_XML)

        # plutil is the same parser Mail uses. Validating the temp file means a
        # malformed write can never reach the real path.
        lint = subprocess.run(
            ["plutil", "-lint", str(tmp)], capture_output=True, timeout=10
        )
        if lint.returncode != 0:
            raise SmartMailboxError(
                f"Plist généré invalide, écriture annulée : "
                f"{lint.stderr.decode(errors='replace').strip()}"
            )

        os.replace(tmp, target)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        tmp.unlink(missing_ok=True)

    return backup


def write_smart_mailboxes(
    mailboxes: list[dict[str, Any]], path: Path | None = None
) -> dict[str, Any]:
    """Atomically replace the plist(s), refusing every unsafe precondition.

    Writes BOTH the iCloud copy and the local mirror when Mail is synced. See
    ``icloud_plist_path``: writing only the local one is silently undone at the
    next Mail launch. The iCloud copy goes first, so a failure midway leaves the
    authoritative file already correct rather than only the mirror.

    Order matters elsewhere too: refuse on a running Mail *before* taking any
    backup, so a rejected call leaves no trace at all.
    """
    if mail_is_running():
        raise MailIsRunningError(
            "Mail.app est ouvert. Le quitter (⌘Q) avant d'écrire : Mail réécrit "
            "ce fichier en quittant et effacerait la modification."
        )

    # An explicit path is a caller (or a test) naming its target: honour it and
    # do not silently also write a second file it never asked about.
    if path is not None:
        backup = _write_one(mailboxes, path)
        return {
            "path": str(path),
            "paths": [str(path)],
            "backup": str(backup) if backup else None,
            "count": count_entries(mailboxes),
        }

    targets = [p for p in (icloud_plist_path(), plist_path()) if p is not None]
    backups = [_write_one(mailboxes, t) for t in targets]

    return {
        "path": str(targets[0]),
        "paths": [str(t) for t in targets],
        "backup": str(backups[0]) if backups[0] else None,
        "backups": [str(b) for b in backups if b],
        "count": count_entries(mailboxes),
    }


def count_entries(mailboxes: list[dict[str, Any]]) -> int:
    """Total across the whole tree.

    Counting the root list only would report "1 mailbox" after filing ten
    clients under a folder, which is the number the user is checking against.
    """
    total = 0
    for entry in mailboxes:
        total += 1
        children = entry.get("MailboxChildren")
        if isinstance(children, list):
            total += count_entries(children)
    return total


def _new_id() -> str:
    return str(uuid.uuid4()).upper()


def build_criterion(spec: dict[str, Any]) -> dict[str, Any]:
    """Turn one criterion spec into Mail's plist shape, leaf or compound.

    A compound is recognised by carrying ``criteria``; that is what allows a
    caller to nest AND inside OR and express what the Mail UI cannot.
    """
    if "criteria" in spec:
        children = spec.get("criteria") or []
        if not children:
            raise SmartMailboxError("Un critère composé exige au moins un sous-critère")
        return {
            "CriterionUniqueId": _new_id(),
            "Header": "Compound",
            "AllCriteriaMustBeSatisfied": bool(spec.get("all", True)),
            "Criteria": [build_criterion(child) for child in children],
        }

    field = str(spec.get("field", "")).strip().lower()
    if field not in FIELDS:
        raise SmartMailboxError(
            f"Champ inconnu : {field!r}. Attendu : {', '.join(sorted(FIELDS))}"
        )

    value = spec.get("value")
    if value is None or str(value).strip() == "":
        raise SmartMailboxError(f"Le critère sur {field!r} n'a pas de valeur")

    operator = str(spec.get("operator", "contains")).strip().lower()
    if operator not in QUALIFIERS:
        raise SmartMailboxError(
            f"Opérateur inconnu : {operator!r}. Attendu : "
            f"{', '.join(sorted(QUALIFIERS))}"
        )

    return {
        "CriterionUniqueId": _new_id(),
        "Header": FIELDS[field],
        "Expression": str(value),
        "Qualifier": QUALIFIERS[operator],
    }


def _omit_criteria() -> list[dict[str, Any]]:
    """Exclude junk, trash and sent — the three UI checkboxes.

    Without them a "mail from this client" folder also shows the user's own
    replies and everything they deleted, which is not what the name promises.
    """
    return [
        {
            "CriterionUniqueId": _new_id(),
            "Header": "NotInJunkMailbox",
            "Name": "omit junk",
        },
        {
            "CriterionUniqueId": _new_id(),
            "Header": "NotInTrashMailbox",
            "Name": "omit trash",
        },
        {
            "CriterionUniqueId": _new_id(),
            "Header": "NotInASpecialMailbox",
            "Name": "omit sent",
            "SpecialMailboxType": _SPECIAL_MAILBOX_SENT,
        },
    ]


def build_smart_mailbox(
    name: str,
    criteria: list[dict[str, Any]],
    match_logic: str = "all",
    omit_junk_trash_sent: bool = True,
) -> dict[str, Any]:
    """Assemble one smart mailbox dict, ready to append to the plist array."""
    if not name or not name.strip():
        raise SmartMailboxError("Le nom de la boîte intelligente est vide")
    if not criteria:
        raise SmartMailboxError("Une boîte intelligente exige au moins un critère")

    logic = str(match_logic).strip().lower()
    if logic not in ("all", "any"):
        raise SmartMailboxError(f"match_logic doit valoir 'all' ou 'any', reçu {logic!r}")

    # User criteria are wrapped in a single compound so that the omit-* entries
    # (which are always AND) do not get folded into an OR over the user's own
    # conditions. Without the wrapper, match_logic="any" would also make "omit
    # junk" optional, and junk would reappear in the mailbox.
    user_block = {
        "CriterionUniqueId": _new_id(),
        "Header": "Compound",
        "AllCriteriaMustBeSatisfied": logic == "all",
        "Criteria": [build_criterion(c) for c in criteria],
        "Name": "user criteria",
    }

    mailbox_criteria = [user_block]
    if omit_junk_trash_sent:
        mailbox_criteria.extend(_omit_criteria())

    return {
        "MailboxName": name.strip(),
        "MailboxID": _new_id(),
        "MailboxType": _MAILBOX_TYPE_SMART,
        "IMAPMailboxAttributes": _IMAP_MAILBOX_ATTRIBUTES,
        "MailboxAllCriteriaMustBeSatisfied": True,
        "MailboxChildren": [],
        "MailboxCriteria": mailbox_criteria,
    }


def build_folder(name: str) -> dict[str, Any]:
    """A container that groups smart mailboxes in the sidebar.

    Type 8, no criteria: it matches nothing itself, it only holds children.
    Without it, "Clients/Acme" is a single mailbox with a slash in its name,
    which is a naming convention, not a group the user can collapse.
    """
    if not name or not name.strip():
        raise SmartMailboxError("Le nom du dossier est vide")
    return {
        "MailboxName": name.strip(),
        "MailboxID": _new_id(),
        "MailboxType": _MAILBOX_TYPE_FOLDER,
        "IMAPMailboxAttributes": _IMAP_MAILBOX_ATTRIBUTES,
        "MailboxChildren": [],
    }


def is_folder(mailbox: dict[str, Any]) -> bool:
    return mailbox.get("MailboxType") == _MAILBOX_TYPE_FOLDER


def find_mailbox(
    mailboxes: list[dict[str, Any]],
    mailbox_id: str | None = None,
    name: str | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]]:
    """Locate one entry anywhere in the tree.

    Returns ``(entry, siblings, matches)``. ``siblings`` is the list that
    actually holds it, so a caller can remove or reorder it without knowing how
    deep it was. ``matches`` carries every hit, because deleting by name when
    two mailboxes share it must be refused, not resolved arbitrarily.
    """
    matches: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []

    def walk(container: list[dict[str, Any]]) -> None:
        for entry in container:
            hit = (
                entry.get("MailboxID") == mailbox_id
                if mailbox_id
                else entry.get("MailboxName") == name
            )
            if hit:
                matches.append((entry, container))
            children = entry.get("MailboxChildren")
            if isinstance(children, list):
                walk(children)

    walk(mailboxes)
    if not matches:
        return None, mailboxes, []
    entry, siblings = matches[0]
    return entry, siblings, [m[0] for m in matches]


def insert_mailbox(
    mailboxes: list[dict[str, Any]],
    mailbox: dict[str, Any],
    parent: str | None = None,
) -> list[dict[str, Any]]:
    """Add an entry at the root, or inside ``parent``, creating it if needed.

    Creating the missing parent rather than failing is deliberate: the caller
    files ten clients into "Clients" in a loop, and requiring a separate folder
    call first would make the first iteration a special case.
    """
    if not parent:
        return mailboxes + [mailbox]

    result = [dict(m) for m in mailboxes]
    entry, _, _ = find_mailbox(result, name=parent)

    if entry is None:
        folder = build_folder(parent)
        folder["MailboxChildren"] = [mailbox]
        return result + [folder]

    if not is_folder(entry):
        raise SmartMailboxError(
            f"{parent!r} est une boîte intelligente, pas un dossier : "
            f"elle ne peut pas contenir d'autres boîtes"
        )

    children = entry.get("MailboxChildren")
    entry["MailboxChildren"] = ([] if not isinstance(children, list) else children) + [
        mailbox
    ]
    return result


def rename_mailbox(mailbox: dict[str, Any], new_name: str) -> dict[str, Any]:
    """Change the display name in place. Ids are untouched: they are the anchor."""
    if not new_name or not new_name.strip():
        raise SmartMailboxError("Le nouveau nom est vide")
    mailbox["MailboxName"] = new_name.strip()
    return mailbox


def replace_criteria(
    mailbox: dict[str, Any],
    criteria: list[dict[str, Any]],
    match_logic: str = "all",
    omit_junk_trash_sent: bool = True,
) -> dict[str, Any]:
    """Swap a mailbox's criteria while keeping its id, name and children.

    Rebuilt rather than patched: criteria carry unique ids and nested compounds,
    so editing one branch in place is where a half-valid tree would come from.
    Keeping the id matters because it is what the caller stored to find this
    mailbox again.
    """
    rebuilt = build_smart_mailbox(
        name=mailbox.get("MailboxName") or "sans nom",
        criteria=criteria,
        match_logic=match_logic,
        omit_junk_trash_sent=omit_junk_trash_sent,
    )
    mailbox["MailboxCriteria"] = rebuilt["MailboxCriteria"]
    mailbox["MailboxAllCriteriaMustBeSatisfied"] = rebuilt[
        "MailboxAllCriteriaMustBeSatisfied"
    ]
    return mailbox


def describe(mailbox: dict[str, Any]) -> dict[str, Any]:
    """Human-readable summary of one entry, for list_smart_mailboxes."""
    if is_folder(mailbox):
        return {
            "name": mailbox.get("MailboxName"),
            "id": mailbox.get("MailboxID"),
            "type": "folder",
            "children": [
                describe(c) for c in (mailbox.get("MailboxChildren") or [])
            ],
        }
    return {
        "name": mailbox.get("MailboxName"),
        "id": mailbox.get("MailboxID"),
        "type": "smart_mailbox",
        "match_logic": _summarise_logic(mailbox),
        "criteria": _summarise_criteria(mailbox.get("MailboxCriteria") or []),
    }


def _summarise_logic(mailbox: dict[str, Any]) -> str:
    """Report the user block's logic, not the mailbox-level AND.

    The mailbox level is always AND (it joins user criteria with the omit-*
    entries); reporting it would tell the caller "all" for every mailbox,
    including the ones they created with match_logic="any".
    """
    for crit in mailbox.get("MailboxCriteria") or []:
        if crit.get("Header") == "Compound" and crit.get("Name") == "user criteria":
            return "all" if crit.get("AllCriteriaMustBeSatisfied", True) else "any"
    return "all" if mailbox.get("MailboxAllCriteriaMustBeSatisfied", True) else "any"


def _summarise_criteria(criteria: list[dict[str, Any]]) -> list[str]:
    """Flatten to readable lines, dropping the omit-* plumbing the user did not write."""
    _reverse_fields = {v: k for k, v in FIELDS.items()}
    _reverse_quals = {v: k for k, v in QUALIFIERS.items()}
    lines: list[str] = []

    def walk(node: dict[str, Any], depth: int) -> None:
        header = node.get("Header")
        if header == "Compound":
            children = node.get("Criteria") or []
            joiner = "ET" if node.get("AllCriteriaMustBeSatisfied", True) else "OU"
            if node.get("Name") == "user criteria" and depth == 0:
                for child in children:
                    walk(child, depth)
                return
            lines.append(f"{'  ' * depth}({joiner})")
            for child in children:
                walk(child, depth + 1)
            return
        if header in ("NotInJunkMailbox", "NotInTrashMailbox", "NotInASpecialMailbox"):
            return
        field = _reverse_fields.get(header, header)
        qualifier = _reverse_quals.get(node.get("Qualifier", ""), node.get("Qualifier", ""))
        lines.append(f"{'  ' * depth}{field} {qualifier} {node.get('Expression')!r}")

    for crit in criteria:
        walk(crit, 0)
    return lines

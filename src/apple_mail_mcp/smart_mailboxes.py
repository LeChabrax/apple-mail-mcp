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

# MailboxType 7 = top-level smart mailbox. Nested ones use 0, which we do not
# emit: the value is only meaningful inside a MailboxChildren array.
_MAILBOX_TYPE_SMART = 7

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
            "count": len(mailboxes),
        }

    targets = [p for p in (icloud_plist_path(), plist_path()) if p is not None]
    backups = [_write_one(mailboxes, t) for t in targets]

    return {
        "path": str(targets[0]),
        "paths": [str(t) for t in targets],
        "backup": str(backups[0]) if backups[0] else None,
        "backups": [str(b) for b in backups if b],
        "count": len(mailboxes),
    }


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


def describe(mailbox: dict[str, Any]) -> dict[str, Any]:
    """Human-readable summary of one entry, for list_smart_mailboxes."""
    return {
        "name": mailbox.get("MailboxName"),
        "id": mailbox.get("MailboxID"),
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

"""Turn a failure into the exact command that fixes it.

WHY THIS EXISTS
---------------
Measured on a teammate's machine, 2026-09-02. An Exchange account without the
IMAP fast path made every content search go through AppleScript, which either
timed out or returned Mail error -10000. The server reported, faithfully:

    {"success": false, "error": "...(-10000)", "error_type": "applescript_error"}

Technically correct, operationally useless. The assistant on the other side has
to *infer* that a one-line command would fix it. Sometimes it does, after
wasting the user's time; often it concludes the tool is broken, and the user
concludes the same.

The fix that already existed was `setup_imap`, shipped days earlier. Nothing in
the failure pointed at it. Documentation did not close the gap either: a README
is read once, at install, by someone who has no failure to connect it to.

So the remedy travels WITH the failure. `remediation` is a structured field on
the error itself — the one thing a caller cannot skip, cannot forget to read,
and cannot have missed because it was written before the problem existed.

DESIGN RULES
------------
- **Name the account.** "Run setup_imap" is advice; "run setup_imap on
  'Exchange'" is an action. The account is always in scope at the call site.
- **Never guess a cause we did not observe.** A remediation is attached only
  when the account genuinely has no live IMAP path. Attaching it to every
  AppleScript error would train callers to ignore the field.
- **Say what it costs.** The user types a password into a macOS window. Hiding
  that turns a 30-second fix into a surprise.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Mail's own error for "the Apple event handler failed". On a content read it
# almost always means the AppleScript path could not deliver the body — the
# exact case the IMAP fast path exists to avoid.
_APPLESCRIPT_HANDLER_FAILED = "-10000"

_SLOW_PATH_HINTS = (
    _APPLESCRIPT_HANDLER_FAILED,
    "timed out",
    "timeout",
    "AppleEvent handler failed",
)


def looks_like_missing_fast_path(error: str) -> bool:
    """True when this failure is the shape a missing IMAP fast path produces.

    Deliberately narrow. A wrong mailbox name or a deleted message also fail
    over AppleScript, and telling that user to configure IMAP would be noise
    that teaches everyone to skip the field.
    """
    text = (error or "").lower()
    return any(h.lower() in text for h in _SLOW_PATH_HINTS)


def fast_path_is_live(connector: Any, account: str | None) -> bool:
    """Whether `account` already has a stored IMAP password.

    Same probe `imap_status` uses, and deliberately the cheap half of it: a
    password in the keychain. Opening a real IMAP session to be sure would put
    a network round-trip on an error path that is already the slow case.

    Anything it cannot determine counts as "not live", so the hint is shown.
    The cost of a needless hint is one ignored line; the cost of hiding it is
    the failure this module exists for.
    """
    if not account or connector is None:
        return False

    probe = getattr(connector, "_get_imap_password_with_fallback", None)
    if probe is None:
        return False

    try:
        # The keychain entry is keyed on (account, email), so the email has to
        # be resolved first. An account the connector cannot even list is not
        # one we can claim is configured.
        email = _email_of(connector, account)
        if email is None:
            return False
        return bool(probe(account, email))
    except Exception as exc:  # noqa: BLE001
        logger.debug("IMAP password probe failed for %s: %s", account, exc)
        return False


def _email_of(connector: Any, account: str) -> str | None:
    """First address of `account`, matched by name or UUID as callers may pass either."""
    for entry in connector.list_accounts() or []:
        if account in (entry.get("name"), entry.get("id")):
            emails = entry.get("email_addresses") or []
            return emails[0] if emails else None
    return None


def imap_setup_remediation(account: str | None) -> dict[str, Any]:
    """The instruction to hand back, naming the account and its cost."""
    target = account or "<nom du compte>"
    return {
        "problem": (
            "Ce compte n'a pas la voie rapide IMAP : la lecture passe par "
            "Mail.app message par message, ce qui expire sur une recherche "
            "dans le corps des messages."
        ),
        "fix": f'setup_imap(account="{target}")',
        "cli": (
            'uvx --from "git+https://github.com/LeChabrax/apple-mail-mcp@main" '
            f'apple-mail-mcp setup-imap --account "{target}"'
        ),
        "user_action": (
            "Une fenêtre macOS demande le mot de passe DE LA BOÎTE MAIL (pas "
            "celui de la session Mac). Sur un hébergeur classique (OVH), c'est "
            "le mot de passe habituel ; iCloud et Gmail exigent un mot de passe "
            "applicatif."
        ),
        "expected_gain": (
            "Recherche dans le corps des messages : de plusieurs minutes ou un "
            "échec, à environ une seconde (mesuré 37 s contre 3 s sur une vraie "
            "boîte le 2026-08-27)."
        ),
        "verify": "imap_status()",
    }


# A search that returns after this long has not failed, but the user has
# already decided the tool is broken. Measured 2026-09-02 on a real iCloud
# account with no fast path: 45.3 s, success=true, zero results.
SLOW_SUCCESS_SECONDS = 10.0


def with_remediation(
    response: dict[str, Any],
    connector: Any = None,
    account: str | None = None,
) -> dict[str, Any]:
    """Attach the IMAP remedy to an error response when it actually applies.

    Called from the error paths of the read tools. Returns the response
    untouched whenever the diagnosis does not hold, so a caller can wrap
    unconditionally without having to decide.
    """
    if response.get("success"):
        return response
    if not looks_like_missing_fast_path(str(response.get("error", ""))):
        return response
    if fast_path_is_live(connector, account):
        # Already configured: the failure has some other cause, and pointing at
        # setup_imap here would send the user down a dead end.
        return response

    response["remediation"] = imap_setup_remediation(account)
    return response


def with_slow_path_remediation(
    response: dict[str, Any],
    elapsed_s: float,
    connector: Any = None,
    account: str | None = None,
) -> dict[str, Any]:
    """Attach the remedy to a SUCCESSFUL response that took far too long.

    The case that actually costs teams their trust, and the one an error-only
    hook misses entirely. Measured on a real account: a body search returned
    ``success: true`` after 45.3 s. Nothing failed, so no error path ran, so no
    remedy was offered — while the person watching the spinner concluded the
    connector was broken and stopped using it.

    A ``warnings`` entry already said as much in prose. It travelled at the
    bottom of a successful payload, in a list callers skim past on success.
    The same fact as a structured field, on the response the caller is already
    reading, is the difference between a fix applied and a tool abandoned.
    """
    if elapsed_s < SLOW_SUCCESS_SECONDS:
        return response
    if fast_path_is_live(connector, account):
        # Slow despite the fast path: a huge mailbox, a cold cache, something
        # else. Blaming IMAP setup here would send the user to a dead end.
        return response

    remedy = imap_setup_remediation(account)
    remedy["problem"] = (
        f"Cette recherche a pris {elapsed_s:.0f} s parce que ce compte n'a pas "
        f"la voie rapide IMAP : Mail.app est piloté message par message."
    )
    response["remediation"] = remedy
    return response

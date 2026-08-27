"""Etat du chemin rapide IMAP, compte par compte.

Il existe parce que le repli sur AppleScript est MUET. Quand l'IMAP echoue,
le connecteur journalise « IMAP failed ... falling back » et rend quand meme
un resultat, en minutes au lieu de secondes. Rien, ni dans Claude ni dans le
terminal, ne dit lequel des deux chemins a servi ni pourquoi. Trois pannes
distinctes (mot de passe accentue, LOGIN plus lent que le budget de
connexion, port absent) ont ainsi produit exactement le meme symptome.

Ce module repond en une passe : pour chaque compte, quel hote, quel port,
un mot de passe est-il enregistre, et une connexion reelle aboutit-elle.
Lecture seule. Aucun mot de passe demande, aucun affiche.

Le rapport porte aussi le commit installe : un serveur MCP demarre avant une
mise a jour continue de tourner sur l'ancien code, ce qui est invisible
autrement et se lit ici.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from .exceptions import MailKeychainError
from .imap_connector import CONNECT_TIMEOUT_S, _connect_imap

# Verdicts, du plus sain au plus casse.
OK = "ok"
NO_HOST = "pas_de_serveur_imap"
NO_PASSWORD = "mot_de_passe_absent"
AUTH_FAILED = "identifiants_refuses"
UNREACHABLE = "serveur_injoignable"
CRASHED = "erreur_client"

_VERDICT_HELP = {
    OK: "chemin rapide actif",
    NO_HOST: "Mail ne declare aucun serveur pour ce compte, rien a configurer",
    NO_PASSWORD: "lancer `apple-mail-mcp setup-imap --account <nom>`",
    AUTH_FAILED: "le serveur refuse ce mot de passe, le reconfigurer",
    UNREACHABLE: "serveur ou reseau injoignable, aucune conclusion sur le mot de passe",
    CRASHED: "echec cote client avant toute reponse du serveur, a remonter",
}


def installed_commit() -> str | None:
    """Renvoie le commit git installe, quand le paquet vient d'un depot.

    uv et pip deposent l'origine exacte dans ``direct_url.json`` a cote du
    paquet. C'est la seule facon de distinguer un serveur a jour d'un serveur
    demarre avant la derniere mise a jour, qui repond pourtant sans erreur.
    """
    try:
        from importlib.metadata import distribution

        dist = distribution("apple-mail-mcp")
        raw = dist.read_text("direct_url.json")
        if not raw:
            return None
        info = json.loads(raw)
        commit = (info.get("vcs_info") or {}).get("commit_id")
        if commit:
            return str(commit)
        # Installation editable depuis un dossier : pas de commit fige.
        if (info.get("dir_info") or {}).get("editable"):
            return "editable:" + str(Path(info.get("url", "")).name)
    except Exception:  # noqa: BLE001 — un diagnostic ne casse jamais
        return None
    return None


def probe_account(connector: Any, account: dict[str, Any]) -> dict[str, Any]:
    """Sonde un compte : resolution, Keychain, puis connexion reelle.

    La sonde va jusqu'au LOGIN parce que c'est la que tout s'est joue : un
    mot de passe present dans le Keychain ne prouve rien, il peut etre
    refuse, et un hote joignable peut n'avoir aucun port utilisable.
    """
    nom = account.get("name") or account.get("id") or "?"
    rapport: dict[str, Any] = {
        "account": nom,
        "account_type": account.get("account_type"),
        "email": None,
        "host": None,
        "port": None,
        "keychain": False,
        "verdict": None,
        "detail": None,
        "elapsed_s": None,
    }

    try:
        host, port, email = connector._resolve_imap_config(nom)
    except Exception as e:  # noqa: BLE001
        rapport["verdict"] = CRASHED
        rapport["detail"] = f"{type(e).__name__}: {e}"
        return rapport

    rapport.update(email=email, host=host or None, port=port or None)
    if not host:
        rapport["verdict"] = NO_HOST
        return rapport

    try:
        password = connector._get_imap_password_with_fallback(nom, email)
    except MailKeychainError as e:
        rapport["verdict"] = NO_PASSWORD
        rapport["detail"] = type(e).__name__
        return rapport
    rapport["keychain"] = True

    from imapclient.exceptions import IMAPClientError, LoginError

    debut = time.perf_counter()
    try:
        client = _connect_imap(host, port, CONNECT_TIMEOUT_S)
        try:
            client.socket().settimeout(30.0)
            client.login(email, password)
            rapport["verdict"] = OK
        finally:
            try:
                client.logout()
            except Exception:  # noqa: BLE001 — au revoir poli, sans plus
                pass
    except LoginError as e:
        rapport["verdict"] = AUTH_FAILED
        rapport["detail"] = str(e)[:200]
    except (OSError, IMAPClientError) as e:
        rapport["verdict"] = UNREACHABLE
        rapport["detail"] = f"{type(e).__name__}: {e}"[:200]
    except Exception as e:  # noqa: BLE001
        rapport["verdict"] = CRASHED
        rapport["detail"] = f"{type(e).__name__}: {e}"[:200]
    rapport["elapsed_s"] = round(time.perf_counter() - debut, 2)
    return rapport


def imap_status(connector: Any) -> dict[str, Any]:
    """Rapport complet : un verdict par compte, plus le commit installe."""
    comptes = connector.list_accounts()
    rapports = [probe_account(connector, a) for a in comptes]
    return {
        "commit": installed_commit(),
        "accounts": rapports,
        "fast_path_count": sum(1 for r in rapports if r["verdict"] == OK),
        "count": len(rapports),
        "verdict_help": _VERDICT_HELP,
    }


def main() -> int:
    """Commande `apple-mail-mcp status` : le meme rapport, en clair."""
    from .mail_connector import AppleMailConnector

    try:
        rapport = imap_status(AppleMailConnector())
    except Exception as e:  # noqa: BLE001
        print(f"Mail ne repond pas : {type(e).__name__} {e}", file=sys.stderr)
        print(
            "Si une fenetre macOS demande l'autorisation d'acceder a Mail, "
            "repondre oui, puis relancer.",
            file=sys.stderr,
        )
        return 1

    print(f"commit installe : {rapport['commit'] or 'inconnu'}")
    print(
        f"{rapport['fast_path_count']} compte(s) sur {rapport['count']} "
        f"en chemin rapide IMAP.\n"
    )
    for r in rapport["accounts"]:
        cible = f"{r['host']}:{r['port']}" if r["host"] else "aucun serveur"
        duree = f" en {r['elapsed_s']}s" if r["elapsed_s"] is not None else ""
        print(f"── {r['account']}  ({r['email'] or 'sans adresse'})")
        print(f"   serveur   : {cible}")
        print(f"   Keychain  : {'oui' if r['keychain'] else 'non'}")
        print(f"   verdict   : {r['verdict']}{duree}")
        print(f"   -> {_VERDICT_HELP.get(r['verdict'], '')}")
        if r["detail"]:
            print(f"   detail    : {r['detail']}")
        print()

    return 0 if rapport["fast_path_count"] else 1


if __name__ == "__main__":
    sys.exit(main())

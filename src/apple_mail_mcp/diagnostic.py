#!/usr/bin/env python3
"""Diagnostic à lancer sur un poste où le serveur "ne voit pas la boîte".

Répond à une seule question : le serveur trouve-t-il la bonne boîte de
réception de chaque compte configuré dans Mail, et y trouve-t-il des messages.

Lecture seule, aucune écriture, aucun mot de passe demandé.

    uvx --from git+https://github.com/LeChabrax/apple-mail-mcp@teams \\
        apple-mail-mcp-diagnose
"""

from __future__ import annotations

import sys


def main() -> int:
    try:
        from apple_mail_mcp.mail_connector import AppleMailConnector
    except ImportError:
        print("Le paquet apple-mail-mcp n'est pas installé dans cet environnement.")
        return 2

    c = AppleMailConnector()
    try:
        comptes = c.list_accounts()
    except Exception as e:  # noqa: BLE001
        print(f"Impossible de lister les comptes : {type(e).__name__} {e}")
        print("Si macOS a demandé l'autorisation d'accéder à Mail, répondre oui")
        print("puis relancer.")
        return 1

    if not comptes:
        print("Mail ne déclare aucun compte.")
        return 1

    print(f"{len(comptes)} compte(s) dans Mail.\n")
    souci = False

    for a in comptes:
        nom = a.get("name") or a.get("id") or "?"
        print(f"── {nom}")
        print(f"   type      : {a.get('account_type')}   actif : {a.get('enabled')}")

        alertes: list[str] = []
        boite = c.resolve_inbox_name(a["id"], on_warning=alertes.append)
        print(f"   réception : {boite!r}")
        for m in alertes:
            souci = True
            print(f"   ⚠  {m}")

        try:
            dossiers = c.list_mailboxes(a["id"])
            print(f"   dossiers  : {len(dossiers)}")
            if boite not in {d.get("name") for d in dossiers}:
                souci = True
                print(
                    f"   ⚠  {boite!r} n'est pas dans la liste des dossiers de ce"
                    f" compte. Une recherche dedans ne rendra jamais rien."
                )
        except Exception as e:  # noqa: BLE001
            souci = True
            print(f"   ⚠  dossiers illisibles : {type(e).__name__} {e}")

        try:
            trouves = c.search_messages(account=a["id"], limit=3)
            print(f"   messages  : {len(trouves)} sur les 3 demandés")
            if not trouves:
                souci = True
                print(
                    "   ⚠  aucun message. Soit la boîte est réellement vide,"
                    " soit ce n'est pas la bonne boîte."
                )
        except Exception as e:  # noqa: BLE001
            souci = True
            print(f"   ⚠  recherche en échec : {type(e).__name__} {e}")
        print()

    if souci:
        print("Des anomalies sont signalées ci-dessus. Envoyer cette sortie telle")
        print("quelle : les noms de dossiers qu'elle contient sont l'information")
        print("qui manque pour corriger.")
        return 1

    print("Tous les comptes répondent, boîte de réception trouvée et lisible.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

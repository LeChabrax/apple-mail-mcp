"""Trouver seul les bons parametres IMAP d'un compte, a partir du mot de passe.

Ce module existe a cause d'une soiree entiere, le 2026-08-27, passee a
configurer trois boites a la main. Chaque echec ressemblait au precedent — une
recherche lente, un refus d'authentification — et chacun avait une cause
differente :

  - Mail.app rendait `port` = 0 pour un compte Exchange natif, dont le serveur
    repond pourtant en IMAPS sur 993 ;
  - le LOGIN a tester n'est pas toujours l'adresse affichee : Mail expose
    `user name`, plusieurs `email addresses`, et le serveur n'en accepte qu'un ;
  - un mot de passe applicatif est exige par iCloud et Gmail, jamais par un
    hebergeur classique — la consigne inverse a envoye quelqu'un chercher une
    heure durant un reglage qui n'existe pas.

Une personne devant son terminal ne peut pas deviner laquelle s'applique. Une
machine peut les essayer toutes : c'est quelques secondes de connexions.

Ce qui reste irreductible : le mot de passe. Les identifiants de Mail.app
vivent dans le trousseau protege, lie a Mail.app par ACL — verifie, la ligne
de commande ne les lit pas. L'auto-configuration part donc TOUJOURS d'un mot
de passe fourni, et ne devine que le reste.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError, LoginError

from .imap_connector import CONNECT_TIMEOUT_S, OPERATION_TIMEOUT_S, _connect_imap

logger = logging.getLogger(__name__)

# Ports tentes quand celui de Mail.app ne repond pas — ou n'existe pas.
# 993 d'abord : c'est l'IMAPS standard, et le seul qu'un compte Exchange natif
# accepte alors que Mail.app n'en declare aucun.
PORTS_CANDIDATS = (993, 143)


@dataclass(frozen=True)
class Reglage:
    """Un jeu de parametres qui a reellement ouvert une session."""

    host: str
    port: int
    login: str
    origine_port: str
    origine_login: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "login": self.login,
            "origine_port": self.origine_port,
            "origine_login": self.origine_login,
        }


def candidats_login(user_name: str, emails: list[str]) -> list[str]:
    """Logins a essayer, du plus probable au moins probable, sans doublon.

    `user name` d'abord : c'est le credential que Mail.app envoie lui-meme,
    donc celui qui marche dans le cas general. Les adresses declarees
    ensuite, car sur certains comptes `user name` est vide ou porte une
    valeur que le serveur refuse (un Apple ID tiers, une adresse d'alias).
    """
    vus: set[str] = set()
    ordonnes: list[str] = []
    for valeur in [user_name, *emails]:
        v = (valeur or "").strip()
        if v and v.lower() not in vus:
            vus.add(v.lower())
            ordonnes.append(v)
    return ordonnes


def candidats_port(port_declare: int | None) -> list[tuple[int, str]]:
    """Ports a essayer, avec la provenance de chacun pour le rapport.

    Le port declare par Mail.app passe en premier quand il est utilisable.
    Zero n'en est pas un : c'est ce que Mail rend pour un compte qu'il ne
    pilote pas en IMAP, et s'y connecter echoue en « Can't assign requested
    address » — la panne du 2026-08-27.
    """
    candidats: list[tuple[int, str]] = []
    if port_declare and port_declare > 0:
        candidats.append((int(port_declare), "declare par Mail"))
    for p in PORTS_CANDIDATS:
        if all(p != c for c, _ in candidats):
            candidats.append((p, "essaye par defaut"))
    return candidats


def negocier(
    host: str,
    port_declare: int | None,
    user_name: str,
    emails: list[str],
    password: str,
    *,
    connect: Callable[[str, int, float], IMAPClient] = _connect_imap,
) -> tuple[Reglage | None, list[str]]:
    """Cherche le premier couple (port, login) qui ouvre vraiment une session.

    Renvoie ``(reglage, journal)``. Le journal liste chaque tentative et son
    issue : c'est lui qu'on montre quand rien ne marche, parce qu'il distingue
    « le serveur refuse ce mot de passe » de « ce port ne repond pas », deux
    diagnostics qu'on a confondus toute une soiree.

    Aucun mot de passe n'apparait dans le journal.
    """
    journal: list[str] = []
    if not host:
        journal.append("Mail ne declare aucun serveur pour ce compte")
        return None, journal

    logins = candidats_login(user_name, emails)
    if not logins:
        journal.append("aucun identifiant a essayer (ni user name ni adresse)")
        return None, journal

    for port, origine_port in candidats_port(port_declare):
        try:
            client = connect(host, port, CONNECT_TIMEOUT_S)
        except (OSError, IMAPClientError) as exc:
            journal.append(f"{host}:{port} ne repond pas ({type(exc).__name__})")
            continue

        try:
            # Le LOGIN est une operation serveur, pas une sonde de
            # joignabilite : une boite Hosted Exchange y met une dizaine de
            # secondes. Sous le budget de connexion, tout echouait.
            client.socket().settimeout(OPERATION_TIMEOUT_S)
            for login in logins:
                origine_login = (
                    "user name de Mail" if login == logins[0] else "adresse declaree"
                )
                try:
                    client.login(login, password)
                except LoginError:
                    journal.append(f"{host}:{port} refuse l'identifiant {login}")
                    continue
                except (OSError, IMAPClientError) as exc:
                    journal.append(
                        f"{host}:{port} a coupe pendant le login ({type(exc).__name__})"
                    )
                    break
                except Exception as exc:  # noqa: BLE001
                    # Un plantage cote client avant tout octet reseau — le mot
                    # de passe accentue en etait un. On le nomme au lieu de le
                    # confondre avec un refus du serveur.
                    journal.append(
                        f"{host}:{port} echec client sur {login} "
                        f"({type(exc).__name__})"
                    )
                    break
                journal.append(f"{host}:{port} accepte {login}")
                return (
                    Reglage(host, port, login, origine_port, origine_login),
                    journal,
                )
        finally:
            try:
                client.logout()
            except Exception:  # noqa: BLE001 — au revoir poli, sans plus
                pass

    return None, journal

"""Tests de la negociation des parametres IMAP.

Chaque cas ici est une panne reellement rencontree le 2026-08-27, dont la
cause etait indevinable pour la personne devant son terminal : toutes se
presentaient comme « ca ne marche pas ».
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from imapclient.exceptions import IMAPClientError, LoginError

from apple_mail_mcp.imap_autoconf import (
    candidats_login,
    candidats_port,
    negocier,
)


def _connect_factory(
    accepte: set[tuple[int, str]] | None = None,
    ports_morts: set[int] | None = None,
    erreur_client: Exception | None = None,
) -> tuple[Any, list[tuple[int, str]]]:
    """Un faux serveur : il n'accepte QUE les couples (port, login) donnes."""
    tentatives: list[tuple[int, str]] = []
    accepte = accepte or set()
    ports_morts = ports_morts or set()

    def connect(host: str, port: int, timeout: float) -> MagicMock:
        if port in ports_morts:
            raise OSError("Can't assign requested address")
        client = MagicMock()

        def login(identifiant: str, mdp: str) -> None:
            tentatives.append((port, identifiant))
            if erreur_client is not None:
                raise erreur_client
            if (port, identifiant) not in accepte:
                raise LoginError("[AUTHENTICATIONFAILED] Authentication failed.")

        client.login.side_effect = login
        return client

    return connect, tentatives


class TestCandidats:
    def test_le_user_name_passe_avant_les_adresses(self) -> None:
        # C'est le credential que Mail.app envoie lui-meme : dans le cas
        # general, c'est celui qui marche.
        assert candidats_login("u@ex.com", ["alias@ex.com", "u@ex.com"]) == [
            "u@ex.com",
            "alias@ex.com",
        ]

    def test_un_user_name_vide_laisse_la_place_aux_adresses(self) -> None:
        assert candidats_login("", ["a@ex.com"]) == ["a@ex.com"]

    def test_le_port_declare_passe_en_premier(self) -> None:
        assert candidats_port(143)[0] == (143, "declare par Mail")

    def test_le_port_zero_n_est_pas_un_port(self) -> None:
        # Mail rend 0 pour un compte qu'il ne pilote pas en IMAP. S'y
        # connecter echoue en « Can't assign requested address ».
        assert [p for p, _ in candidats_port(0)] == [993, 143]

    def test_aucun_doublon_quand_le_declare_est_deja_standard(self) -> None:
        assert [p for p, _ in candidats_port(993)] == [993, 143]


class TestNegociation:
    def test_compte_exchange_sans_port_declare(self) -> None:
        # Le cas exact du 2026-08-27 : Mail rend 0, le serveur repond en IMAPS.
        connect, _ = _connect_factory(accepte={(993, "h@ex.com")})
        reglage, journal = negocier(
            "ex2.mail.ovh.net", 0, "h@ex.com", ["h@ex.com"], "mdp",
            connect=connect,
        )
        assert reglage is not None
        assert (reglage.port, reglage.login) == (993, "h@ex.com")
        assert reglage.origine_port == "essaye par defaut"
        assert any("accepte" in ligne for ligne in journal)

    def test_le_serveur_choisit_l_identifiant(self) -> None:
        # Un Apple ID tiers, un alias SMTP : l'adresse affichee n'est pas
        # toujours celle que le serveur accepte.
        connect, tentatives = _connect_factory(accepte={(993, "vrai@ex.com")})
        reglage, _ = negocier(
            "imap.ex.com", 993, "affiche@ex.com", ["vrai@ex.com"], "mdp",
            connect=connect,
        )
        assert reglage is not None and reglage.login == "vrai@ex.com"
        # Il a bien essaye l'affiche d'abord, puis l'autre.
        assert tentatives == [(993, "affiche@ex.com"), (993, "vrai@ex.com")]

    def test_un_port_mort_n_arrete_pas_la_recherche(self) -> None:
        connect, _ = _connect_factory(
            accepte={(143, "u@ex.com")}, ports_morts={993}
        )
        reglage, journal = negocier(
            "imap.ex.com", None, "u@ex.com", [], "mdp", connect=connect
        )
        assert reglage is not None and reglage.port == 143
        assert any("ne repond pas" in ligne for ligne in journal)

    def test_un_mot_de_passe_refuse_partout_rend_le_detail(self) -> None:
        # Le journal doit distinguer « refuse » de « injoignable » : les deux
        # ont ete confondus toute une soiree.
        connect, _ = _connect_factory(accepte=set())
        reglage, journal = negocier(
            "imap.ex.com", 993, "u@ex.com", [], "mdp", connect=connect
        )
        assert reglage is None
        assert all("refuse l'identifiant" in ligne for ligne in journal)

    def test_un_plantage_client_est_nomme_comme_tel(self) -> None:
        # Le mot de passe accentue en etait un : ni un refus du serveur, ni un
        # probleme de reseau. Le confondre a produit un faux diagnostic.
        connect, _ = _connect_factory(
            erreur_client=UnicodeEncodeError("ascii", "é", 0, 1, "nope")
        )
        reglage, journal = negocier(
            "imap.ex.com", 993, "u@ex.com", [], "mdp", connect=connect
        )
        assert reglage is None
        assert any("echec client" in ligne for ligne in journal)

    def test_sans_serveur_rien_n_est_tente(self) -> None:
        connect, tentatives = _connect_factory()
        reglage, journal = negocier(
            "", 993, "u@ex.com", [], "mdp", connect=connect
        )
        assert reglage is None and tentatives == []
        assert "aucun serveur" in journal[0]

    def test_le_mot_de_passe_n_apparait_jamais_dans_le_journal(self) -> None:
        connect, _ = _connect_factory(accepte=set())
        _, journal = negocier(
            "imap.ex.com", 993, "u@ex.com", ["a@ex.com"], "s3cret-token",
            connect=connect,
        )
        assert "s3cret-token" not in " ".join(journal)

    @pytest.mark.parametrize("erreur", [OSError("coupé"), IMAPClientError("boom")])
    def test_une_coupure_pendant_le_login_passe_au_port_suivant(
        self, erreur: Exception
    ) -> None:
        connect, _ = _connect_factory(
            accepte={(143, "u@ex.com")}, erreur_client=None
        )
        appels: list[int] = []

        def connect_capricieux(host: str, port: int, timeout: float) -> MagicMock:
            appels.append(port)
            if port == 993:
                client = MagicMock()
                client.login.side_effect = erreur
                return client
            return connect(host, port, timeout)

        reglage, _ = negocier(
            "imap.ex.com", 993, "u@ex.com", [], "mdp", connect=connect_capricieux
        )
        assert reglage is not None and reglage.port == 143
        assert appels == [993, 143]

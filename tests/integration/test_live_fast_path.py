"""Suite LIVE : le chemin rapide IMAP, contre un vrai Mail et un vrai serveur.

Pourquoi elle existe, en une phrase : les 1497 tests de ce depot sont mockes,
et AUCUN n'a vu passer les quatre pannes du 2026-08-27, qui ont coute une
soiree a trois personnes. Chacune n'existe que face a un vrai serveur :

  - un mot de passe accentue tuait le LOGIN avant tout octet reseau ;
  - un LOGIN de 9,7 s (OVH Hosted Exchange) sortait du budget de 3 s ;
  - Mail.app rend `port` = 0 pour un compte Exchange natif ;
  - la boite de reception se resolvait via Mail.app AVANT le choix du chemin,
    ce qui faisait expirer la recherche alors que l'IMAP repondait en 0,5 s.

Un mock repond ce qu'on lui dit de repondre. Ces tests-la interrogent le
serveur.

LECTURE SEULE. Aucun envoi, aucune suppression, aucun mot de passe saisi :
les identifiants viennent du trousseau deja configure sur le poste.

Ils ne tournent pas par defaut — ils dependent des comptes du poste :

    APPLE_MAIL_MCP_LIVE=1 pytest tests/integration/test_live_fast_path.py \\
        --run-integration

Sans compte en chemin rapide, chaque test se DECLARE ignore. Il ne passe
jamais au vert par defaut : un faux vert est pire qu'un rouge.
"""

from __future__ import annotations

import os
import time
from typing import Any

import pytest

from apple_mail_mcp.imap_status import OK, imap_status
from apple_mail_mcp.mail_connector import AppleMailConnector

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("APPLE_MAIL_MCP_LIVE") != "1",
        reason="APPLE_MAIL_MCP_LIVE != '1' (suite live, depend des comptes du poste)",
    ),
]

# Au-dela, une recherche cesse d'etre utilisable dans une conversation. Le
# budget AppleScript etant de 60 s, ce seuil separe franchement les deux
# chemins : on ne mesure pas une nuance, on constate lequel a servi.
SEUIL_CHEMIN_RAPIDE_S = 15.0

# L'ouverture d'un fil reste, elle, sur AppleScript : `get_thread` resout son
# ancre en balayant les comptes, et la recherche par Message-ID n'y est pas
# indexee (~20 s par boite, cf. APPLESCRIPT_GOTCHAS.md). Mesure du 2026-08-27 :
# 27,9 s pour « chercher puis ouvrir le fil ». C'est lent, ce n'est plus casse.
# Le seuil separe donc les deux : ce que le chemin rapide doit tenir, et ce qui
# reste a porter sur IMAP (ImapConnector.get_message sait deja chercher par
# en-tete Message-ID, il manque la resolution de l'ancre et ses en-tetes).
SEUIL_OUVERTURE_FIL_S = 55.0


@pytest.fixture(scope="module")
def connector() -> AppleMailConnector:
    return AppleMailConnector()


@pytest.fixture(scope="module")
def rapport(connector: AppleMailConnector) -> dict[str, Any]:
    return imap_status(connector)


@pytest.fixture(scope="module")
def compte_rapide(rapport: dict[str, Any]) -> str:
    """Un compte dont la connexion IMAP aboutit reellement, ou skip."""
    for r in rapport["accounts"]:
        if r["verdict"] == OK:
            return str(r["account"])
    pytest.skip(
        "aucun compte en chemin rapide sur ce poste : lancer "
        "`apple-mail-mcp setup-imap --account <nom>` d'abord"
    )


class TestEtatDesBoites:
    """`imap_status` est le seul endroit qui dit quel chemin sert."""

    # KILL-TARGET: src/apple_mail_mcp/mail_connector.py:1889
    def test_un_compte_annonce_ok_repond_vraiment_vite(
        self,
        connector: AppleMailConnector,
        rapport: dict[str, Any],
        compte_rapide: str,
    ) -> None:
        vise = next(
            r for r in rapport["accounts"] if r["account"] == compte_rapide
        )
        assert vise["verdict"] == OK
        assert vise["keychain"] is True

        # Ce test couvre le CHEMIN, pas la sonde : retirer le LOGIN de
        # `probe_account` le laisse vert, puisque la recherche derriere ouvre
        # sa propre connexion — verifie par mutation. Les verdicts de la sonde
        # sont couverts hors ligne, et prouves rouges, par
        # tests/unit/test_imap_status.py. Ce qu'il prouve, lui : un compte
        # annonce `ok` repond effectivement vite. C'est ce qui manquait toute
        # la soiree — le rapport disait « chemin rapide actif » pendant que la
        # recherche expirait.
        debut = time.perf_counter()
        connector.search_messages(compte_rapide, limit=1)
        assert time.perf_counter() - debut < SEUIL_CHEMIN_RAPIDE_S

    # Le port absent (Mail rend 0 pour un compte Exchange natif) n'est PAS
    # teste ici : sur un poste sans compte de ce type, l'assertion serait
    # vraie quoi qu'il arrive — verifie par mutation, elle survivait au
    # retrait du correctif. Elle est couverte hors ligne, et prouvee rouge,
    # par test_resolve_imap_config_missing_port_assumes_imaps.

    # KILL-TARGET: src/apple_mail_mcp/imap_status.py:60
    def test_le_rapport_nomme_le_code_qui_tourne(
        self, rapport: dict[str, Any]
    ) -> None:
        # Sur une installation editable (un poste de developpement), le commit
        # vient d'une autre branche du code et le test survit au retrait de
        # celle qui compte — verifie par mutation. Il ne prouve quelque chose
        # que sur une installation git, celle des postes de l'equipe.
        if str(rapport["commit"] or "").startswith("editable:"):
            pytest.skip("installation editable : le commit git ne s'applique pas")
        # Un serveur MCP demarre avant une mise a jour continue de servir
        # l'ancien code sans qu'aucune autre sortie ne le montre. On a passe
        # une partie de la soiree a le croire a jour.
        assert rapport["commit"], "le rapport doit nommer la version installee"


class TestCheminRapide:
    """Ce qui se mesure : quel chemin a reellement servi."""

    # KILL-TARGET: src/apple_mail_mcp/mail_connector.py:1889
    def test_une_recherche_sans_boite_precisee_reste_rapide(
        self, connector: AppleMailConnector, compte_rapide: str
    ) -> None:
        # Le piege exact du 2026-08-27 : la boite de reception etait demandee
        # a Mail.app AVANT le choix du chemin. Sur un compte lent, ce seul
        # aller-retour brulait les 60 s et la recherche expirait, alors que
        # `imap_status` annoncait « chemin rapide actif, 0,52 s ».
        debut = time.perf_counter()
        resultats = connector.search_messages(compte_rapide, limit=3)
        duree = time.perf_counter() - debut

        assert isinstance(resultats, list)
        assert duree < SEUIL_CHEMIN_RAPIDE_S, (
            f"{duree:.1f}s sans preciser la boite : la resolution est repassee "
            f"par Mail.app avant le chemin rapide"
        )

    # KILL-TARGET: src/apple_mail_mcp/mail_connector.py:1889
    def test_la_boite_est_resolue_sans_rien_demander_a_mail_app(
        self, connector: AppleMailConnector, compte_rapide: str, monkeypatch
    ) -> None:
        appels: list[str] = []
        vrai = connector.resolve_inbox_name
        monkeypatch.setattr(
            connector,
            "resolve_inbox_name",
            lambda a, **k: (appels.append(a), vrai(a, **k))[1],
        )

        connector.search_messages(compte_rapide, limit=1)

        assert appels == [], (
            "le chemin rapide a interroge Mail.app pour nommer la boite, "
            "ce qu'il est precisement cense eviter"
        )

    # KILL-TARGET: src/apple_mail_mcp/imap_connector.py:860
    def test_une_recherche_accentuee_passe_bien_par_le_serveur(
        self, connector: AppleMailConnector, compte_rapide: str
    ) -> None:
        # « cafe » avec accent levait UnicodeEncodeError avant tout octet
        # reseau. Pour une equipe qui ecrit en francais, c'est la moitie des
        # recherches.
        #
        # Ce test interroge le connecteur IMAP DIRECTEMENT, sans passer par
        # AppleMailConnector : le filet de repli ajoute ce soir rattrape
        # justement cette exception, si bien qu'un test passant par la couche
        # du dessus restait vert meme avec le correctif retire — verifie par
        # mutation, il survivait. Le filet protege la personne, il ne doit pas
        # proterger le test.
        from apple_mail_mcp.imap_connector import ImapConnector

        host, port, email = connector._resolve_imap_config(compte_rapide)
        mdp = connector._get_imap_password_with_fallback(compte_rapide, email)
        imap = ImapConnector(host, port, email, mdp)

        resultats = imap.search_messages(
            mailbox=imap.resolve_inbox(), text_contains="é", limit=3
        )
        assert isinstance(resultats, list)

    # Le LOGIN lent (OVH Hosted Exchange, 9,7 s mesures) n'est PAS teste ici :
    # il n'est reproductible que sur un serveur qui repond lentement, et sur un
    # poste dont les comptes repondent vite le test survit au retrait du
    # correctif — verifie par mutation. Il est couvert hors ligne, et prouve
    # rouge, par test_connect_short_then_operation_timeout_raised_before_login
    # et test_operation_timeout_applied_before_login.


class TestParcoursEquipe:
    """Ce que quelqu'un fait vraiment : chercher, puis lire le fil."""

    # KILL-TARGET: src/apple_mail_mcp/mail_connector.py:1889
    def test_chercher_puis_ouvrir_le_fil_du_premier_resultat(
        self, connector: AppleMailConnector, compte_rapide: str
    ) -> None:
        debut = time.perf_counter()
        trouves = connector.search_messages(compte_rapide, limit=3)
        if not trouves:
            pytest.skip("boite vide sur ce poste, rien a enchainer")

        premier = trouves[0]
        assert premier.get("subject") is not None
        assert premier.get("id") or premier.get("message_id")

        fil = connector.get_thread(
            premier.get("message_id") or str(premier["id"])
        )
        duree = time.perf_counter() - debut

        # Un fil contient au moins le message d'origine. Une liste vide
        # signifierait qu'on a resolu le mauvais identifiant.
        assert isinstance(fil, list) and fil
        # Le vrai defaut que ce parcours a revele : le chemin IMAP rend dans
        # `id` le Message-ID RFC, que la resolution d'ancre ne cherchait pas.
        # « cherche puis ouvre le fil » levait MailMessageNotFoundError des que
        # le chemin rapide etait actif, et un `reply_to` sur le meme id etait
        # refuse pour la meme raison.
        assert duree < SEUIL_OUVERTURE_FIL_S, (
            f"ouverture du fil en {duree:.1f}s : au-dela du budget AppleScript, "
            f"donc le balayage n'aboutit plus du tout"
        )

    def test_les_comptes_lents_repondent_quand_meme(
        self, connector: AppleMailConnector, rapport: dict[str, Any]
    ) -> None:
        # Le repli AppleScript est la raison pour laquelle rien n'est jamais
        # casse, seulement lent. Il doit rester fonctionnel : un compte sans
        # mot de passe enregistre repond, plus lentement.
        lents = [
            r for r in rapport["accounts"] if r["verdict"] != OK and r["host"]
        ]
        if not lents:
            pytest.skip("tous les comptes sont en chemin rapide sur ce poste")

        resultats = connector.search_messages(lents[0]["account"], limit=1)
        assert isinstance(resultats, list)

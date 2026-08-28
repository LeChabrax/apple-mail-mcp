"""Tests de la fenetre de saisie du mot de passe.

Elle existe pour que le mot de passe n'ait pas a etre ecrit dans la
conversation, ou il resterait enregistre. Ce qui se verifie ici : ce qui est
saisi arrive intact, et une annulation ne se confond pas avec une panne.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from apple_mail_mcp.password_prompt import (
    DialogueIndisponible,
    demander_mot_de_passe,
)


def _runner(stdout: str = "", stderr: str = "", code: int = 0) -> MagicMock:
    r = MagicMock()
    r.return_value = subprocess.CompletedProcess(
        args=[], returncode=code, stdout=stdout, stderr=stderr
    )
    return r


class TestSaisie:
    def test_le_mot_de_passe_saisi_est_rendu(self) -> None:
        run = _runner("button returned:OK, text returned:motdepasse")
        assert demander_mot_de_passe("Gmail", "a@b.com", runner=run) == "motdepasse"

    def test_une_virgule_dans_le_mot_de_passe_ne_le_tronque_pas(self) -> None:
        # Format reel d'osascript, verifie le 2026-08-28 :
        # « button returned:OK, text returned:abc,def, gave up:false ».
        # Couper sur les virgules perdrait la moitie du mot de passe, et le
        # compte serait configure avec une valeur fausse — un echec
        # d'authentification qui n'aurait aucune explication visible.
        run = _runner("button returned:OK, text returned:a,b,c, gave up:false")
        assert demander_mot_de_passe("X", "a@b.com", runner=run) == "a,b,c"

    def test_les_caracteres_accentues_traversent(self) -> None:
        run = _runner("button returned:OK, text returned:mot-de-passé-é")
        assert demander_mot_de_passe("X", "a@b.com", runner=run) == "mot-de-passé-é"


class TestRefus:
    def test_annuler_rend_none_sans_lever(self) -> None:
        # Cliquer Annuler est une reponse, pas une panne : on ne redemande pas.
        run = _runner(stderr="execution error: User canceled. (-128)", code=1)
        assert demander_mot_de_passe("X", "a@b.com", runner=run) is None

    def test_fenetre_laissee_sans_reponse(self) -> None:
        run = _runner("button returned:, text returned:, gave up:true")
        assert demander_mot_de_passe("X", "a@b.com", runner=run) is None

    def test_saisie_vide_vaut_annulation(self) -> None:
        # Une chaine vide ne configurerait rien : la traiter comme un mot de
        # passe ecrirait une entree Keychain morte.
        run = _runner("button returned:OK, text returned:")
        assert demander_mot_de_passe("X", "a@b.com", runner=run) is None

    @pytest.mark.parametrize(
        "panne",
        [
            OSError("osascript introuvable"),
            subprocess.TimeoutExpired(cmd="osascript", timeout=1),
        ],
    )
    def test_une_session_sans_interface_se_signale(self, panne: Exception) -> None:
        # Sans fenetre possible, l'appelant doit pouvoir proposer autre chose
        # plutot que de croire a une annulation.
        run = MagicMock(side_effect=panne)
        with pytest.raises(DialogueIndisponible):
            demander_mot_de_passe("X", "a@b.com", runner=run)

    def test_un_refus_de_pilotage_se_signale(self) -> None:
        run = _runner(stderr="Not authorized to send Apple events", code=1)
        with pytest.raises(DialogueIndisponible):
            demander_mot_de_passe("X", "a@b.com", runner=run)


class TestScript:
    def test_le_compte_et_l_adresse_sont_affiches(self) -> None:
        run = _runner("button returned:OK, text returned:x")
        demander_mot_de_passe("Exchange", "hugues@ex.com", runner=run)
        script = run.call_args.args[0][2]
        assert "Exchange" in script and "hugues@ex.com" in script

    def test_la_saisie_est_masquee(self) -> None:
        run = _runner("button returned:OK, text returned:x")
        demander_mot_de_passe("X", "a@b.com", runner=run)
        assert "with hidden answer" in run.call_args.args[0][2]

    def test_un_nom_de_compte_avec_guillemets_ne_casse_pas_le_script(self) -> None:
        # Un nom de compte est saisi par la personne dans Mail : il peut
        # contenir un guillemet, qui terminerait la chaine AppleScript.
        run = _runner("button returned:OK, text returned:x")
        demander_mot_de_passe('Boite "perso"', "a@b.com", runner=run)
        script = run.call_args.args[0][2]
        assert '\\"perso\\"' in script

    def test_le_delai_borne_l_attente(self) -> None:
        run = _runner("button returned:, text returned:, gave up:true")
        demander_mot_de_passe("X", "a@b.com", delai_s=30, runner=run)
        assert "giving up after 30" in run.call_args.args[0][2]
        # Le sous-processus attend un peu plus que la fenetre, sinon il serait
        # tue avant qu'elle n'ait rendu sa reponse.
        assert run.call_args.kwargs["timeout"] > 30

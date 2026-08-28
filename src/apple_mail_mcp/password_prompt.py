"""Demander un mot de passe dans une fenetre macOS, pas dans la conversation.

Configurer une boite exige un mot de passe, et il n'existe aucun moyen de
l'obtenir autrement : les identifiants de Mail.app vivent dans le trousseau
protege, lie a Mail.app par ACL — verifie, la ligne de commande ne les lit pas.

Restait le choix de l'endroit ou le taper. Ecrit dans la conversation, il y
reste enregistre. Tape dans un terminal, il n'y figure pas, mais il faut
ouvrir un terminal, ce que personne ne fait.

Ce module ouvre une fenetre systeme a saisie masquee, sur le Mac de la
personne. Le mot de passe va du clavier au trousseau sans passer par la
conversation, sans etre journalise, et sans que quiconque ait a quitter Claude.

Le dialogue est affiche par Mail, deja autorise a etre pilote puisque c'est
tout ce que ce serveur fait. Passer par System Events demanderait une
autorisation macOS de plus, pour le meme resultat.
"""

from __future__ import annotations

import logging
import subprocess

from .utils import escape_applescript_string, sanitize_input

logger = logging.getLogger(__name__)

# Au-dela, la fenetre est restee sans reponse : la personne est partie, ou ne
# l'a jamais vue. On rend la main plutot que de bloquer l'appel indefiniment.
DELAI_REPONSE_S = 120


class DialogueIndisponible(RuntimeError):
    """Aucune fenetre n'a pu etre affichee — session sans interface, macOS qui
    refuse le pilotage. L'appelant redemande alors le mot de passe autrement."""


def demander_mot_de_passe(
    account: str,
    email: str,
    *,
    delai_s: int = DELAI_REPONSE_S,
    runner: object | None = None,
) -> str | None:
    """Ouvre la fenetre et rend ce qui a ete saisi.

    Args:
        account: Nom du compte, affiche pour que la personne sache lequel.
        email: Adresse concernee, meme raison.
        delai_s: Secondes avant abandon.
        runner: Couture de test. Production : ``subprocess.run``.

    Returns:
        Le mot de passe saisi, ou ``None`` si la fenetre a ete annulee ou
        laissee sans reponse. Une chaine vide compte comme une annulation :
        elle ne configurerait rien.

    Raises:
        DialogueIndisponible: aucune fenetre n'a pu s'afficher.
    """
    lancer = runner or subprocess.run
    titre = escape_applescript_string(sanitize_input(f"Boite {account}"))
    texte = escape_applescript_string(
        sanitize_input(
            f"Mot de passe de la boite {email}.\n\n"
            f"C'est celui du fournisseur de messagerie, pas celui de votre "
            f"session Mac. Il est enregistre dans le trousseau et n'apparait "
            f"nulle part ailleurs."
        )
    )
    script = (
        f'tell application "Mail" to display dialog "{texte}" '
        f'default answer "" with hidden answer with title "{titre}" '
        f"giving up after {int(delai_s)}"
    )

    try:
        result = lancer(  # type: ignore[operator]
            ["/usr/bin/osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=delai_s + 15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DialogueIndisponible(str(exc)) from exc

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        # -128 : la personne a clique Annuler. C'est une reponse, pas une
        # panne : on ne redemande pas, on n'insiste pas.
        if "-128" in stderr or "User canceled" in stderr:
            return None
        raise DialogueIndisponible(stderr or f"osascript a rendu {result.returncode}")

    sortie = (result.stdout or "").strip()
    if "gave up:true" in sortie:
        return None
    # `display dialog` rend « button returned:OK, text returned:<saisie> », et
    # la saisie peut contenir des virgules : on coupe sur la premiere
    # occurrence du marqueur, jamais sur les separateurs.
    marqueur = "text returned:"
    if marqueur not in sortie:
        return None
    valeur = sortie.split(marqueur, 1)[1]
    # Un « , gave up:false » final peut suivre la saisie.
    for suffixe in (", gave up:false", ", gave up:true"):
        if valeur.endswith(suffixe):
            valeur = valeur[: -len(suffixe)]
    return valeur or None

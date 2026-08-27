"""Tests du rapport d'etat IMAP.

Il existe parce que le repli AppleScript est muet : ces tests verrouillent le
fait que chaque cause d'echec porte un verdict DISTINCT. Un rapport qui dirait
"pas ok" pour trois pannes differentes ne servirait a rien — c'est exactement
la situation qu'il corrige.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from imapclient.exceptions import IMAPClientError, LoginError

from apple_mail_mcp.exceptions import MailKeychainEntryNotFoundError
from apple_mail_mcp.imap_status import (
    AUTH_FAILED,
    CRASHED,
    NO_HOST,
    NO_PASSWORD,
    OK,
    UNREACHABLE,
    imap_status,
    probe_account,
)

COMPTE = {"name": "Exchange", "id": "UUID-1", "account_type": "unknown"}


def _connector(
    *, host: str = "ex2.mail.ovh.net", port: int = 993, password: Any = "pw"
) -> MagicMock:
    c = MagicMock()
    c.list_accounts.return_value = [COMPTE]
    c._resolve_imap_config.return_value = (host, port, "h@example.com")
    if isinstance(password, Exception):
        c._get_imap_password_with_fallback.side_effect = password
    else:
        c._get_imap_password_with_fallback.return_value = password
    return c


class TestVerdicts:
    @patch("apple_mail_mcp.imap_status._connect_imap")
    def test_login_reussi_rend_ok(self, connect: MagicMock) -> None:
        r = probe_account(_connector(), COMPTE)
        assert r["verdict"] == OK
        assert (r["host"], r["port"]) == ("ex2.mail.ovh.net", 993)
        assert r["keychain"] is True
        connect.return_value.login.assert_called_once()

    def test_sans_serveur_ne_sonde_rien(self) -> None:
        # Un compte POP ou local n'a rien a configurer : le distinguer d'un
        # compte mal configure evite d'envoyer quelqu'un chercher un mot de
        # passe qui ne servira jamais.
        c = _connector(host="", port=0)
        r = probe_account(c, COMPTE)
        assert r["verdict"] == NO_HOST
        c._get_imap_password_with_fallback.assert_not_called()

    def test_mot_de_passe_absent(self) -> None:
        c = _connector(password=MailKeychainEntryNotFoundError("absent"))
        r = probe_account(c, COMPTE)
        assert r["verdict"] == NO_PASSWORD
        assert r["keychain"] is False

    @patch("apple_mail_mcp.imap_status._connect_imap")
    def test_identifiants_refuses(self, connect: MagicMock) -> None:
        connect.return_value.login.side_effect = LoginError("AUTHENTICATIONFAILED")
        r = probe_account(_connector(), COMPTE)
        assert r["verdict"] == AUTH_FAILED
        assert "AUTHENTICATIONFAILED" in r["detail"]

    @patch("apple_mail_mcp.imap_status._connect_imap")
    def test_serveur_injoignable(self, connect: MagicMock) -> None:
        # Le cas du port 0 : rien ne se conclut sur le mot de passe.
        connect.side_effect = OSError("Can't assign requested address")
        r = probe_account(_connector(port=0), COMPTE)
        assert r["verdict"] == UNREACHABLE

    @patch("apple_mail_mcp.imap_status._connect_imap")
    def test_protocole_injoignable(self, connect: MagicMock) -> None:
        connect.side_effect = IMAPClientError("boom")
        assert probe_account(_connector(), COMPTE)["verdict"] == UNREACHABLE

    @patch("apple_mail_mcp.imap_status._connect_imap")
    def test_crash_client(self, connect: MagicMock) -> None:
        # Le cas du mot de passe accentue avant correction : ni un refus du
        # serveur, ni un probleme de reseau.
        connect.return_value.login.side_effect = UnicodeEncodeError(
            "ascii", "passé", 4, 5, "ordinal not in range(128)"
        )
        assert probe_account(_connector(), COMPTE)["verdict"] == CRASHED

    def test_resolution_en_echec_ne_leve_pas(self) -> None:
        c = _connector()
        c._resolve_imap_config.side_effect = RuntimeError("Mail ne repond pas")
        r = probe_account(c, COMPTE)
        assert r["verdict"] == CRASHED
        assert "RuntimeError" in r["detail"]

    @patch("apple_mail_mcp.imap_status._connect_imap")
    def test_le_mot_de_passe_ne_sort_jamais_du_rapport(
        self, connect: MagicMock
    ) -> None:
        r = probe_account(_connector(password="s3cret-token"), COMPTE)
        assert "s3cret-token" not in str(r)


class TestRapportComplet:
    @patch("apple_mail_mcp.imap_status._connect_imap")
    def test_compte_les_comptes_en_chemin_rapide(self, connect: MagicMock) -> None:
        c = _connector()
        c.list_accounts.return_value = [COMPTE, {"name": "Local", "id": "UUID-2"}]
        c._resolve_imap_config.side_effect = [
            ("ex2.mail.ovh.net", 993, "h@example.com"),
            ("", 0, "local@example.com"),
        ]
        rapport = imap_status(c)
        assert rapport["count"] == 2
        assert rapport["fast_path_count"] == 1
        assert [a["verdict"] for a in rapport["accounts"]] == [OK, NO_HOST]

    @patch("apple_mail_mcp.imap_status._connect_imap")
    def test_le_rapport_porte_le_commit_installe(self, connect: MagicMock) -> None:
        # Un serveur MCP demarre avant une mise a jour continue de tourner sur
        # l'ancien code sans qu'aucune autre sortie ne le montre.
        assert "commit" in imap_status(_connector())

"""Notification channel used to deliver the recovery token.

Provider abstraction: self-hosted SMTP (stdlib smtplib), console (air-gapped fallback - the
operator reads the token on standard output, NEVER in the structured log) and memory (tests).

The clear-text token is NEVER written to the structured log nor persisted - only delivered through the channel.
"""

from __future__ import annotations

import logging
import sys
from typing import Protocol

from ..config import get_settings

_log = logging.getLogger(__name__)


class ProvedorNotificacao(Protocol):
    def enviar_token_recuperacao(self, email: str, token: str) -> None: ...


class ProvedorConsole:
    """Air-gapped fallback: prints the token on standard output for an operator/admin to pick up.

    It goes straight to stdout (not to the structured logger), outside the audit trail and the logs.
    """

    def enviar_token_recuperacao(self, email: str, token: str) -> None:
        print(f"[password-recovery] token for {email}: {token}", file=sys.stdout, flush=True)


class ProvedorMemoria:
    """For tests: keeps what would have been delivered, with no external effect."""

    def __init__(self) -> None:
        self.enviados: list[tuple[str, str]] = []

    def enviar_token_recuperacao(self, email: str, token: str) -> None:
        self.enviados.append((email, token))


def montar_email_acesso(token: str, app_url: str | None) -> tuple[str, str]:
    """Subject + body (English) of the access / password-reset e-mail. With `app_url`, it points to the
    reset-screen LINK; without it (air-gapped), it delivers the code for manual use."""
    assunto = "Access to the cFlow data platform"
    if app_url:
        link = f"{app_url.rstrip('/')}/redefinir?token={token}"
        corpo = (
            "Hello!\n\n"
            "You have been given access to the cFlow data platform, where your team queries "
            "reports and data analyses.\n\n"
            "To set your password and sign in:\n\n"
            f"1. Open this link (valid for a limited time):\n   {link}\n"
            "2. Create a password with at least 12 characters and confirm it.\n"
            f"3. Done! Sign in at {app_url.rstrip('/')} with your e-mail and the new password.\n\n"
            "If you did not request this access, ignore this e-mail.\n\n"
            "- The cFlow team\n"
        )
    else:
        corpo = (
            "Hello!\n\nYou have been given access to the cFlow data platform.\n\n"
            "Use the following code on the set-password screen (it expires soon):\n\n"
            f"{token}\n\n- The cFlow team\n"
        )
    return assunto, corpo


class ProvedorSMTP:
    """SMTP via stdlib smtplib. Supports STARTTLS (`smtp_tls`) and authentication
    (`smtp_user`/`smtp_password`) for relays that require it (e.g. port 587)."""

    def enviar_token_recuperacao(self, email: str, token: str) -> None:
        import smtplib
        from email.message import EmailMessage

        cfg = get_settings()
        assunto, corpo = montar_email_acesso(token, cfg.app_url)
        msg = EmailMessage()
        msg["From"] = cfg.smtp_remetente or cfg.smtp_user or "no-reply@example.com"
        msg["To"] = email
        msg["Subject"] = assunto
        msg.set_content(corpo)
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_porta, timeout=20) as s:
            if cfg.smtp_tls:
                s.starttls()
            if cfg.smtp_user and cfg.smtp_password:
                s.login(cfg.smtp_user, cfg.smtp_password)
            s.send_message(msg)


_provedor: ProvedorNotificacao | None = None


def definir_provedor(p: ProvedorNotificacao | None) -> None:
    """Set the active provider (used in tests/configuration)."""
    global _provedor
    _provedor = p


def provedor_atual() -> ProvedorNotificacao:
    """Active provider: the injected one, else SMTP if configured, else console (air-gapped)."""
    if _provedor is not None:
        return _provedor
    if get_settings().smtp_host:
        return ProvedorSMTP()
    return ProvedorConsole()


def entregar_token(email: str, token: str) -> bool:
    """Deliver the token through the channel. A channel failure **does not propagate**: returns False.

    Sending happens OUTSIDE the business transaction (the token is already persisted as a hash). If
    the exception propagated, the caller would return 500 after the commit: the resource would exist,
    the clear-text token would be lost (it only lived in memory) and a retry would hit "already
    exists". The caller decides what to tell the requester - and there is a resend path that
    generates a new token.

    The token NEVER enters the log; the e-mail does because it is already in the audit trail and identifies the case.
    """
    try:
        provedor_atual().enviar_token_recuperacao(email, token)
        return True
    except Exception:
        _log.warning(
            "failed to deliver token through the notification channel",
            exc_info=True,
            extra={"extra_campos": {"destinatario": email}},
        )
        return False

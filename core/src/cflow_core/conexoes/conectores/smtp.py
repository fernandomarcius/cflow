"""SMTP connector (stdlib `smtplib`, run in a thread). The only place in the product that speaks
SMTP: whoever builds the message (`cflow_core.execucao.no_email`) knows no protocol, and this
module knows nothing about pipelines or DuckDB.

config (no secret): {host, porta?, seguranca? ('starttls'|'ssl'|'nenhuma'), usuario?, remetente?,
    remetente_nome?, timeout_segundos?, tentativas?, espera_tentativa_segundos?}
The password comes from the vault, is used only for login and never appears in logs, errors or responses.
"""

from __future__ import annotations

import asyncio
import smtplib
import time
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formataddr

from ...errors import ErroAplicacao
from .base import MensagemEmail, ResultadoTeste

PORTA_PADRAO = {"ssl": 465, "starttls": 587, "nenhuma": 25}
SEGURANCAS = ("starttls", "ssl", "nenhuma")
TIMEOUT_PADRAO = 60
TENTATIVAS_PADRAO = 3
ESPERA_PADRAO = 5


def _seguranca(config: dict) -> str:
    seg = config.get("seguranca") or "starttls"
    return seg if seg in SEGURANCAS else "starttls"


def _porta(config: dict) -> int:
    return int(config.get("porta") or PORTA_PADRAO[_seguranca(config)])


def _remetente(config: dict) -> str:
    """Envelope `From`: explicit `remetente`, otherwise the authentication user. With neither there
    is no way to send: fail loudly rather than send an empty `From`."""
    de = (config.get("remetente") or config.get("usuario") or "").strip()
    if not de:
        raise ErroAplicacao(
            codigo="smtp_sem_remetente",
            mensagem="SMTP connection has no sender: set 'remetente' or 'usuario'.",
            status=400,
        )
    return de


def _cabecalho(valor: str) -> str:
    """Neutralises header injection: CR/LF inside a header becomes a NEW header on the network (a
    data-driven subject could add Bcc:). Collapses them into a space."""
    return " ".join(str(valor).replace("\r", " ").replace("\n", " ").split())


def _causa(exc: Exception) -> str:
    """SANITISED cause (never the server banner or the credential)."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "authentication refused"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "recipient refused by the server"
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return "sender refused by the server"
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return "feature not supported by the server (check security/TLS)"
    if isinstance(exc, smtplib.SMTPConnectError):
        return "host unreachable"
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return "connection closed by the server"
    if isinstance(exc, TimeoutError):
        return "timed out"
    if isinstance(exc, OSError):
        return "host unreachable"
    if isinstance(exc, smtplib.SMTPException):
        return "SMTP server refused the operation"
    return "failed to talk to the SMTP server"


def _abrir(config: dict, senha: str | None, timeout: int) -> smtplib.SMTP:
    """Connects, applies TLS and authenticates. Returns the open session (the caller closes it)."""
    host, porta, seg = config.get("host") or "", _porta(config), _seguranca(config)
    if seg == "ssl":
        cli: smtplib.SMTP = smtplib.SMTP_SSL(host, porta, timeout=timeout)
    else:
        cli = smtplib.SMTP(host, porta, timeout=timeout)
        if seg == "starttls":
            cli.ehlo()
            cli.starttls()
    cli.ehlo()
    usuario = (config.get("usuario") or "").strip()
    if usuario and senha:
        cli.login(usuario, senha)
    return cli


def _fechar(cli: smtplib.SMTP) -> None:
    """Closes without turning a goodbye error into a send failure (the message was already accepted)."""
    try:
        cli.quit()
    except (smtplib.SMTPException, OSError):
        cli.close()


def montar_mime(config: dict, mensagem: MensagemEmail) -> EmailMessage:
    """Builds the MIME message (pure, no network). Public so tests can check what would be sent."""
    de = _remetente(config)
    nome = mensagem.remetente_nome or config.get("remetente_nome") or ""
    msg = EmailMessage()
    msg["From"] = formataddr((_cabecalho(nome), de)) if nome else de
    msg["To"] = ", ".join(_cabecalho(e) for e in mensagem.para)
    if mensagem.cc:
        msg["Cc"] = ", ".join(_cabecalho(e) for e in mensagem.cc)
    # `Bcc` does NOT go into the MIME (it would be visible to everyone). It goes only in the envelope (`sendmail`).
    if mensagem.responder_para:
        msg["Reply-To"] = _cabecalho(mensagem.responder_para)
    msg["Subject"] = _cabecalho(mensagem.assunto)
    # Explicit `quoted-printable`: otherwise Python picks `8bit` and puts raw UTF-8 bytes on the
    # network, which only works on servers with 8BITMIME. On an older relay non-ASCII characters arrive
    # broken, and the failure shows up in a third party's inbox, not in our log.
    msg.set_content(
        mensagem.corpo, subtype="html" if mensagem.corpo_html else "plain", cte="quoted-printable"
    )
    for anexo in mensagem.anexos:
        principal, _, secundario = anexo.tipo_mime.partition("/")
        msg.add_attachment(
            anexo.conteudo,
            maintype=principal or "application",
            subtype=secundario or "octet-stream",
            filename=_cabecalho(anexo.nome),
        )
    return msg


def _enviar_sync(
    config: dict, senha: str | None, mensagem: MensagemEmail, timeout: int, tentativas: int
) -> dict:
    """Sends with TRANSPORT retries. Refused credentials/recipients are NOT retried: repeating does
    not change the outcome and burns execution time."""
    envelope = [*mensagem.para, *mensagem.cc, *mensagem.cco]
    mime = montar_mime(config, mensagem)
    espera = float(config.get("espera_tentativa_segundos", ESPERA_PADRAO) or 0)
    ultima: Exception | None = None
    for tentativa in range(1, max(1, tentativas) + 1):
        cli = None
        try:
            cli = _abrir(config, senha, timeout)
            recusados = cli.send_message(mime, from_addr=_remetente(config), to_addrs=envelope)
            return {
                "aceitos": [e for e in envelope if e not in recusados],
                "recusados": sorted(recusados),
                "tentativas": tentativa,
            }
        except (
            smtplib.SMTPAuthenticationError,
            smtplib.SMTPRecipientsRefused,
            smtplib.SMTPSenderRefused,
            smtplib.SMTPNotSupportedError,
        ) as exc:
            raise ErroAplicacao(
                codigo="email_falha", mensagem=f"Failed to send e-mail: {_causa(exc)}.", status=400
            ) from exc
        except (smtplib.SMTPException, OSError) as exc:
            ultima = exc
            if tentativa < max(1, tentativas) and espera > 0:
                time.sleep(espera)
        finally:
            if cli is not None:
                _fechar(cli)
    raise ErroAplicacao(
        codigo="email_falha",
        mensagem=(
            f"Failed to send e-mail after {max(1, tentativas)} attempt(s): "
            f"{_causa(ultima) if ultima else 'unknown cause'}."
        ),
        status=400,
    ) from ultima


class ConectorSmtp:
    """E-mail server. NOT a data source (no schema browsing and no sampling)."""

    async def testar(self, config: dict, senha: str | None) -> ResultadoTeste:
        """Connects, applies TLS, authenticates and does NOT send a message: testing must not become spam."""

        def _testar() -> None:
            cli = _abrir(config, senha, int(config.get("timeout_segundos") or TIMEOUT_PADRAO))
            try:
                cli.noop()
            finally:
                _fechar(cli)

        try:
            await asyncio.to_thread(_testar)
            return ResultadoTeste(ok=True)
        except ErroAplicacao:
            raise
        except Exception as exc:  # noqa: BLE001 — sanitised cause
            return ResultadoTeste(ok=False, causa=_causa(exc))

    async def enviar(
        self,
        config: dict,
        senha: str | None,
        mensagem: MensagemEmail,
        *,
        timeout: int | None = None,
        tentativas: int | None = None,
    ) -> dict:
        # smtplib is blocking: run it in a thread so the worker loop is not stalled.
        return await asyncio.to_thread(
            _enviar_sync,
            config,
            senha,
            mensagem,
            int(timeout or config.get("timeout_segundos") or TIMEOUT_PADRAO),
            int(tentativas or config.get("tentativas") or TENTATIVAS_PADRAO),
        )

    async def schemas_tabelas(self, config: dict, senha: str | None) -> list[dict]:
        raise ErroAplicacao(
            codigo="nao_aplicavel",
            mensagem="SMTP connection is an e-mail destination, not a data source (no browsing).",
            status=400,
        )

    async def colunas(
        self, config: dict, senha: str | None, schema: str, tabela: str
    ) -> list[dict]:
        raise ErroAplicacao(
            codigo="nao_aplicavel",
            mensagem="SMTP connection is an e-mail destination, not a data source (no columns).",
            status=400,
        )

    async def amostra(
        self, config: dict, senha: str | None, sql: str, limite: int, params: dict | None = None
    ) -> tuple[list[str], list[dict]]:
        raise ErroAplicacao(
            codigo="nao_aplicavel",
            mensagem="SMTP connection is an e-mail destination, not a data source (no sample).",
            status=400,
        )


def validar_config(config: dict) -> list[str]:
    """Validates the SMTP connection config (pure). Returns messages; empty = valid.
    Called when the connection is created/edited, before any network attempt."""
    problemas: list[str] = []
    host = (config.get("host") or "").strip()
    if not host:
        problemas.append("Set the SMTP server host.")
    elif any(c in host for c in ("/", "@", " ", ":")):
        problemas.append("The host must be just the server address (no scheme, port, '/' or '@').")
    porta = config.get("porta")
    if porta is not None and (
        not isinstance(porta, int) or isinstance(porta, bool) or not 1 <= porta <= 65535
    ):
        problemas.append("The port must be an integer between 1 and 65535.")
    seg = config.get("seguranca")
    if seg is not None and seg not in SEGURANCAS:
        problemas.append(f"Invalid security setting (use {', '.join(SEGURANCAS)}).")
    for campo, lo, hi in (
        ("timeout_segundos", 1, 600),
        ("tentativas", 1, 10),
        ("espera_tentativa_segundos", 0, 60),
    ):
        v = config.get(campo)
        if v is not None and (not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi):
            problemas.append(f"'{campo}' must be an integer between {lo} and {hi}.")
    for campo in ("remetente", "usuario"):
        v = config.get(campo)
        if v:
            try:
                Address(addr_spec=str(v).strip())
            except (ValueError, IndexError):
                problemas.append(f"'{campo}' is not a valid e-mail address.")
    return problemas

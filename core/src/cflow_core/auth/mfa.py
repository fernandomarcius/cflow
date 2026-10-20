"""TOTP MFA (RFC 6238) - stdlib implementation, no new dependency (works air-gapped).

The TOTP secret is sensitive: it is kept encrypted in the vault, never in clear text in metadata or logs.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

PASSO = 30  # seconds
DIGITOS = 6


def gerar_segredo() -> str:
    """Base32 secret (no padding) for the authenticator app."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(segredo_b32: str, contador: int) -> str:
    chave = base64.b32decode(segredo_b32 + "=" * ((-len(segredo_b32)) % 8))
    h = hmac.new(chave, struct.pack(">Q", contador), hashlib.sha1).digest()
    desloc = h[-1] & 0x0F
    cod = (struct.unpack(">I", h[desloc : desloc + 4])[0] & 0x7FFFFFFF) % (10**DIGITOS)
    return str(cod).zfill(DIGITOS)


def totp_atual(segredo: str, momento: float | None = None) -> str:
    """Current TOTP code (useful for tests)."""
    return _hotp(segredo, int((momento if momento is not None else time.time()) // PASSO))


def verificar_totp(
    segredo: str, codigo: str, *, janela: int = 1, momento: float | None = None
) -> bool:
    """Validate the code, tolerating ±`janela` steps (clock skew)."""
    agora = int((momento if momento is not None else time.time()) // PASSO)
    alvo = str(codigo).strip().zfill(DIGITOS)
    return any(
        hmac.compare_digest(_hotp(segredo, agora + i), alvo) for i in range(-janela, janela + 1)
    )


def uri_provisionamento(email: str, segredo: str, emissor: str = "cFlow") -> str:
    """otpauth:// URI for the QR code / authenticator app."""
    return (
        f"otpauth://totp/{quote(emissor)}:{quote(email)}"
        f"?secret={segredo}&issuer={quote(emissor)}&period={PASSO}&digits={DIGITOS}"
    )

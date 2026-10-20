"""Password hashing (Argon2id) and policy. Clear-text passwords are never persisted or logged."""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError

from ..config import get_settings
from ..errors import ErroAplicacao

_ph = PasswordHasher()


def validar_politica(senha: str) -> None:
    """Validate the password policy (minimum length). Raises ErroAplicacao on violation."""
    minimo = get_settings().senha_min
    if len(senha) < minimo:
        raise ErroAplicacao(
            codigo="senha_fraca",
            mensagem=f"The password must be at least {minimo} characters long.",
            status=400,
        )


def gerar_hash(senha: str) -> str:
    """Generate the Argon2id hash of the password."""
    return _ph.hash(senha)


def verificar(senha_hash: str | None, senha: str) -> bool:
    """Check the password against the hash. False if it does not match or there is no hash."""
    if not senha_hash:
        return False
    try:
        return _ph.verify(senha_hash, senha)
    except (VerifyMismatchError, VerificationError):
        return False

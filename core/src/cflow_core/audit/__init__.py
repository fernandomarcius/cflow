"""Append-only audit service."""

from .events import Acao, AtorTipo
from .service import (
    registrar,
    registrar_login,
    registrar_login_falha,
    registrar_logout,
    registrar_permissao,
    registrar_recuperacao_iniciada,
    registrar_senha_trocada,
)

__all__ = [
    "Acao",
    "AtorTipo",
    "registrar",
    "registrar_login",
    "registrar_login_falha",
    "registrar_logout",
    "registrar_permissao",
    "registrar_recuperacao_iniciada",
    "registrar_senha_trocada",
]

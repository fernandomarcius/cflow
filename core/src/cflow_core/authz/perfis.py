"""Profiles and the ordering of permission levels."""

from __future__ import annotations

from enum import StrEnum


class Perfil(StrEnum):
    ADMIN = "admin"
    ESPECIALISTA = "especialista"
    OPERADOR = "operador"
    AUDITOR = "auditor"
    # INTEGRATION account: signs in only by API token, runs what it has permission for and
    # follows progress - it does not create, edit or publish anything.
    SERVICO = "servico"


class Nivel(StrEnum):
    VER = "ver"
    EXECUTAR = "executar"
    EDITAR = "editar"
    ADMINISTRAR = "administrar"


# Cumulative order: administrar ⊇ editar ⊇ executar ⊇ ver
_ORDEM = {Nivel.VER: 0, Nivel.EXECUTAR: 1, Nivel.EDITAR: 2, Nivel.ADMINISTRAR: 3}


def rank(nivel: str) -> int:
    """Position of the level in the cumulative order."""
    return _ORDEM[Nivel(nivel)]


def cobre(nivel_concedido: str, nivel_exigido: str) -> bool:
    """True if the granted level covers (>=) the required one."""
    return rank(nivel_concedido) >= rank(nivel_exigido)


def gere_identidade_acesso(perfil: str) -> bool:
    """FULL identity/access management - groups, permissions and profile changes: admin only.

    Do not confuse with `pode_gerir_usuario`: the specialist creates and manages `operador` users,
    but never changes anyone's profile. That distinction prevents two-step escalation.
    """
    return perfil == Perfil.ADMIN


# Target profiles the SPECIALIST can create and manage. It NEVER includes `admin` or
# `especialista`: otherwise the specialist could create a peer (or an admin) and segregation would
# break indirectly, without ever going through `atribuir_perfil` - which stays admin-only.
GERIDOS_PELO_ESPECIALISTA: frozenset[str] = frozenset({Perfil.OPERADOR})


def pode_gerir_usuario(ator_perfil: str, alvo_perfil: str) -> bool:
    """Whether the actor can create/manage (resend access, fix e-mail, suspend/restore) a user with
    the given profile. Changing someone's PROFILE stays out of here - it is admin-only, and that is
    what prevents `operador -> admin` escalation by someone who could only create operators.
    """
    if ator_perfil == Perfil.ADMIN:
        return True
    if ator_perfil == Perfil.ESPECIALISTA:
        return alvo_perfil in GERIDOS_PELO_ESPECIALISTA
    return False

"""Audit vocabulary: actions and actor type. Values are Portuguese identifiers stored in the database."""

from __future__ import annotations

from enum import StrEnum


class Acao(StrEnum):
    """Action recorded in the trail: resource writes + security events."""

    CRIAR = "criar"
    EDITAR = "editar"
    PUBLICAR = "publicar"
    EXCLUIR = "excluir"
    LOGIN = "login"
    LOGIN_FALHA = "login_falha"
    LOGOUT = "logout"
    RECUPERACAO_INICIADA = "recuperacao_iniciada"
    SENHA_TROCADA = "senha_trocada"
    PERMISSAO = "permissao"
    EXPORTAR = "exportar"
    RETENCAO_ALTERADA = "retencao_alterada"
    EXECUTAR = "executar"


class AtorTipo(StrEnum):
    """Origin of the actor of an action."""

    USUARIO = "usuario"  # ator_id is set
    SISTEMA = "sistema"  # internal platform action; ator_id is null
    ANONIMO = "anonimo"  # request without identity; ator_id is null

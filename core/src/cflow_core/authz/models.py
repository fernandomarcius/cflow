"""Identity and access tables (SQLAlchemy Core), for runtime queries.

The canonical DDL lives in the migrations. Here we declare the Table objects.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, MetaData, Table, Text
from sqlalchemy.dialects.postgresql import ENUM, UUID

metadata = MetaData()

# Enums already created by the migrations - create_type=False avoids re-emitting CREATE TYPE.
# `servico`: integration account that signs in only by API token.
perfil_enum = ENUM(
    "admin", "especialista", "operador", "auditor", "servico", name="perfil", create_type=False
)
tipo_sujeito_enum = ENUM("usuario", "grupo", name="tipo_sujeito", create_type=False)
nivel_enum = ENUM(
    "ver", "executar", "editar", "administrar", name="nivel_permissao", create_type=False
)

usuarios = Table(
    "usuarios",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("email", Text, nullable=False, unique=True),
    Column("nome", Text, nullable=False),
    Column("perfil", perfil_enum, nullable=False),
    Column("ativo", Boolean, nullable=False),
    Column("senha_hash", Text, nullable=True),
    Column("ultimo_acesso", DateTime(timezone=True), nullable=True),
    Column("criado_em", DateTime(timezone=True), nullable=False),
)

grupos = Table(
    "grupos",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("nome", Text, nullable=False, unique=True),
    Column("descricao", Text, nullable=True),
)

usuarios_grupos = Table(
    "usuarios_grupos",
    metadata,
    Column("usuario_id", UUID(as_uuid=True), primary_key=True),
    Column("grupo_id", UUID(as_uuid=True), primary_key=True),
)

permissoes = Table(
    "permissoes",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("sujeito_tipo", tipo_sujeito_enum, nullable=False),
    Column("sujeito_id", UUID(as_uuid=True), nullable=False),
    Column("recurso_tipo", Text, nullable=False),
    Column("recurso_id", UUID(as_uuid=True), nullable=False),
    Column("nivel", nivel_enum, nullable=False),
    Column("concedido_por", UUID(as_uuid=True), nullable=True),
    Column("criado_em", DateTime(timezone=True), nullable=False),
)

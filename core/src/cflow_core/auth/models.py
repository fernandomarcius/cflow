"""Authentication tables (SQLAlchemy Core). The canonical DDL lives in the migrations."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    MetaData,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import INET, UUID

metadata = MetaData()

sessoes = Table(
    "sessoes",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("usuario_id", UUID(as_uuid=True), nullable=False),
    Column("token_hash", Text, nullable=False, unique=True),
    Column("criada_em", DateTime(timezone=True), nullable=False),
    Column("ultima_atividade_em", DateTime(timezone=True), nullable=False),
    Column("expira_em", DateTime(timezone=True), nullable=False),
    Column("revogada_em", DateTime(timezone=True), nullable=True),
)

tokens_recuperacao = Table(
    "tokens_recuperacao",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("usuario_id", UUID(as_uuid=True), nullable=False),
    Column("token_hash", Text, nullable=False),
    Column("criado_em", DateTime(timezone=True), nullable=False),
    Column("expira_em", DateTime(timezone=True), nullable=False),
    Column("usado_em", DateTime(timezone=True), nullable=True),
)

tentativas_login = Table(
    "tentativas_login",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("email_tentado", Text, nullable=False),
    Column("ip", INET, nullable=True),
    Column("ocorrido_em", DateTime(timezone=True), nullable=False),
    Column("sucesso", Boolean, nullable=False),
)

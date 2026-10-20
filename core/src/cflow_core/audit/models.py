"""Tables (SQLAlchemy Core) used at runtime. The canonical DDL lives in the Alembic
migrations (explicit SQL); here we declare the Table objects for safe INSERT/SELECT.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID

metadata = MetaData()

# Audit trail - APPEND-ONLY (cflow_app can only SELECT/INSERT; see the migrations).
auditoria = Table(
    "auditoria",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("ocorrido_em", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("ator_id", UUID(as_uuid=True), nullable=True),
    Column("ator_tipo", Text, nullable=False),  # 'usuario' | 'sistema' | 'anonimo'
    Column("acao", Text, nullable=False),
    Column("recurso_tipo", Text, nullable=False),
    Column("recurso_id", UUID(as_uuid=True), nullable=True),
    Column("antes", JSONB, nullable=True),
    Column("depois", JSONB, nullable=True),
    Column("request_id", String, nullable=True),
    Column("ip", INET, nullable=True),
)

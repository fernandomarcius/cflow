"""Core pipeline tables (canonical DDL in the migrations)."""

from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, Integer, MetaData, Table, Text, text
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID

metadata = MetaData()

status_versao_enum = ENUM(
    "rascunho", "publicada", "arquivada", name="status_versao", create_type=False
)

pipelines = Table(
    "pipelines",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("nome", Text, nullable=False),
    Column("descricao", Text, nullable=True),
    Column("dono_id", UUID(as_uuid=True), nullable=True),
    Column("versao_publicada_id", UUID(as_uuid=True), nullable=True),
    Column("criado_em", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("sistema", Boolean, nullable=False, server_default=text("false")),
    # Medallion layer: 'bronze'|'silver'|'gold' or NULL (no layer).
    # Orthogonal to `sistema`: the layer is a listing tab, `sistema` is admin-only visibility.
    Column("camada", Text, nullable=True),
    # Group: the set the pipeline belongs to ('load' | 'reference' | 'export') or NULL. Its own
    # column, separate from `camada`: a pipeline can produce gold AND belong to a load group,
    # and `camada` holds a single value.
    Column("grupo", Text, nullable=True),
    # Active flag: blocks EXECUTION (not editing/publishing) on every trigger; the check lives
    # in `engine.criar_execucao`, not here. Default `true`: a new pipeline runs.
    Column("ativo", Boolean, nullable=False, server_default=text("true")),
)

pipeline_versoes = Table(
    "pipeline_versoes",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("pipeline_id", UUID(as_uuid=True), nullable=False),
    Column("numero", Integer, nullable=False),
    Column("definicao", JSONB, nullable=False),
    Column("status", status_versao_enum, nullable=False),
    Column("nota_mudanca", Text, nullable=True),
    Column("autor_id", UUID(as_uuid=True), nullable=True),
    Column("criado_em", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)

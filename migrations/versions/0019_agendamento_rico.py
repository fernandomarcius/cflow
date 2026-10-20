"""Rich schedule: target (pipeline x page), parameters (with relative-period token), friendly
recurrence (`agenda`) + validity window, and last-trigger state.

`intervalo_segundos` becomes nullable (legacy): recurrence now lives in `agenda` (jsonb).

Revision ID: 0019
Revises: 0018
Create Date: 2026-06-22
"""

from __future__ import annotations

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

DDL = [
    "ALTER TABLE agendamentos ADD COLUMN nome text",
    # Target page (portal): the scheduled execution pre-warms the daily cache. NULL = only runs the pipeline.
    "ALTER TABLE agendamentos ADD COLUMN pagina_id uuid REFERENCES paginas(id) ON DELETE SET NULL",
    # Parameter values of the trigger; dates may come as a relative-period token via '@periodo'.
    "ALTER TABLE agendamentos ADD COLUMN parametros jsonb NOT NULL DEFAULT '{}'::jsonb",
    # Friendly recurrence: {tipo: horaria|diaria|semanal|mensal, a_cada_horas?, horario?, dias_semana?, dia_mes?}.
    "ALTER TABLE agendamentos ADD COLUMN agenda jsonb NOT NULL DEFAULT '{}'::jsonb",
    "ALTER TABLE agendamentos ADD COLUMN vigencia_inicio date",
    "ALTER TABLE agendamentos ADD COLUMN vigencia_fim date",
    "ALTER TABLE agendamentos ADD COLUMN ultimo_status text",
    "ALTER TABLE agendamentos ADD COLUMN ultima_execucao_id uuid",
    "ALTER TABLE agendamentos ALTER COLUMN intervalo_segundos DROP NOT NULL",
]


def upgrade() -> None:
    for c in DDL:
        op.execute(c)


def downgrade() -> None:
    for c in [
        "ALTER TABLE agendamentos ALTER COLUMN intervalo_segundos SET NOT NULL",
        "ALTER TABLE agendamentos DROP COLUMN IF EXISTS ultima_execucao_id",
        "ALTER TABLE agendamentos DROP COLUMN IF EXISTS ultimo_status",
        "ALTER TABLE agendamentos DROP COLUMN IF EXISTS vigencia_fim",
        "ALTER TABLE agendamentos DROP COLUMN IF EXISTS vigencia_inicio",
        "ALTER TABLE agendamentos DROP COLUMN IF EXISTS agenda",
        "ALTER TABLE agendamentos DROP COLUMN IF EXISTS parametros",
        "ALTER TABLE agendamentos DROP COLUMN IF EXISTS pagina_id",
        "ALTER TABLE agendamentos DROP COLUMN IF EXISTS nome",
    ]:
        op.execute(c)

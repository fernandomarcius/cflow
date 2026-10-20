"""T024 — política de retry por tipo de nó (Temporal). Puro (constrói RetryPolicy, sem servidor).

Invariante D3: nós de EFEITO (escrita_sql/http/email) NÃO retentam (maximum_attempts=1) — uma
re-entrega de activity após o commit externo não pode disparar de novo um efeito não idempotente
(e um e-mail enviado duas vezes não tem como ser desfeito). Os demais nós retentam pouco (2 — erro
de SQL/fonte é determinístico). erro de domínio (`ErroAplicacao`) nunca é retentado, e a
activity do nó tem heartbeat — pod morto libera o nó em minutos, não em 30.
"""

from __future__ import annotations

from datetime import timedelta

from cflow_worker.activities import HEARTBEAT_TIMEOUT, INTERVALO_HEARTBEAT_S
from cflow_worker.workflow import NAO_RETENTAVEIS, TIPOS_EFEITO, politica_para


def test_nos_de_efeito_nao_retentam():
    assert set(TIPOS_EFEITO) == {"escrita_sql", "http", "email"}
    for tipo in TIPOS_EFEITO:
        assert politica_para(tipo).maximum_attempts == 1


def test_nos_normais_retentam_pouco():
    for tipo in ("fonte", "sql", "guiada", "comparacao", "python", "saida", None):
        assert politica_para(tipo).maximum_attempts == 2


def test_erro_de_dominio_nao_e_retentado():
    """Parâmetro inválido, snapshot ausente, SQL que não compila: a 2ª tentativa só dobra o tempo
    até a falha aparecer. O que vale retentar é infra, que chega como outra exceção."""
    assert "ErroAplicacao" in NAO_RETENTAVEIS
    for tipo in ("sql", "email"):
        assert "ErroAplicacao" in politica_para(tipo).non_retryable_error_types


def test_heartbeat_bate_bem_antes_do_timeout():
    assert HEARTBEAT_TIMEOUT == timedelta(minutes=2)
    assert INTERVALO_HEARTBEAT_S * 3 < HEARTBEAT_TIMEOUT.total_seconds()

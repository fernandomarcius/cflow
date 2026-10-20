"""Owned quarantine: per-source summary, nuanced retention and chronic-source signal.

Quarantine used to be written and nobody read it - no screen, no alert, no report and no retention.
What these tests pin down is the distinction that makes reading worthwhile: **quarantine whose
slice later landed in bronze is already-resolved noise; quarantine whose slice never landed is
data that does not exist.** It is the same line that decides what retention may delete - and the
second half must never be deleted.

No network: nothing here downloads anything. State is seeded straight into the tables, because
what is being proven is the read path, not the ingestion engine (that one has `test_ingestao.py`).
"""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import text

from cflow_core.db import transacao
from cflow_core.ingestao import repositorio as repo

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration


# --- Harness ----------------------------------------------------------------------------------


def _contrato(dataset: str) -> dict:
    return {
        "classe": "http_file",
        "dataset": dataset,
        "cadencia": "manual",
        "janela": {"tipo": "mensal", "inicio": "2026-01"},
        "acesso": {"url_padrao": "https://example.com/{ano}{mes}.csv"},
        "formato": {"tipo": "csv", "encoding": "latin-1", "delimitador": ";"},
    }


async def _semear_fonte(app_engine, *, nome: str, dataset: str) -> uuid.UUID:
    async with app_engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO ingestao_fontes (nome, dataset, classe, contrato) "
                    "VALUES (:n, :d, 'http_file', CAST(:c AS jsonb)) RETURNING id"
                ),
                {"n": nome, "d": dataset, "c": json.dumps(_contrato(dataset))},
            )
        ).scalar_one()


async def _semear_execucao(app_engine, fonte_id, *, dias_atras: int = 0, concluida: bool = True):
    fim = "now() - make_interval(days => CAST(:d AS int))" if concluida else "NULL"
    async with app_engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO ingestao_execucoes (fonte_id, status, origem, iniciada_em, "
                    "concluida_em) VALUES (:f, :s, 'manual', "
                    f"now() - make_interval(days => CAST(:d AS int)), {fim}) RETURNING id"
                ),
                {
                    "f": fonte_id,
                    "s": "concluida" if concluida else "executando",
                    "d": dias_atras,
                },
            )
        ).scalar_one()


async def _semear_quarentena(
    app_engine,
    fonte_id,
    *,
    chave_lote: str,
    codigo: str = "parse_falhou",
    dias_atras: int = 0,
    execucao_id=None,
) -> None:
    async with app_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO ingestao_quarentena (fonte_id, execucao_id, chave_lote, etapa, "
                "codigo, mensagem, criado_em) VALUES (:f, :e, :c, 'parse', :co, 'falhou', "
                "now() - make_interval(days => CAST(:d AS int)))"
            ),
            {"f": fonte_id, "e": execucao_id, "c": chave_lote, "co": codigo, "d": dias_atras},
        )


async def _semear_lote_bronze(app_engine, fonte_id, *, chave_lote: str, sha: str = "s1") -> None:
    async with app_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO ingestao_lotes (fonte_id, chave_lote, sha256, status, bronze_uri, "
                "ingestion_date) VALUES (:f, :c, :s, 'bronze', :u, :dt)"
            ),
            {
                "f": fonte_id,
                "c": chave_lote,
                "s": sha,
                "u": f"file:///lake/bronze/{chave_lote}/dados.parquet",
                "dt": date(2026, 1, 1),
            },
        )


async def _semear_fatia(app_engine, execucao_id, fonte_id, *, chave_lote: str, desfecho: str):
    async with app_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO ingestao_fatias (execucao_id, fonte_id, chave_lote, desfecho) "
                "VALUES (:e, :f, :c, :d)"
            ),
            {"e": execucao_id, "f": fonte_id, "c": chave_lote, "d": desfecho},
        )


async def _leitura(owner_engine, cliente, email="reader.quarantine@example.com"):
    return await usuario_logado(owner_engine, cliente, email=email, perfil="especialista")


# --- Per-source summary -------------------------------------------------------------------


async def test_resumo_agrega_por_fonte_e_separa_buraco_de_ruido(
    cliente, owner_engine, app_engine
) -> None:
    """Happy path: two quarantined slices, one already resolved - and the summary says which is which."""
    fonte = await _semear_fonte(app_engine, nome="Registry feed", dataset="registry_monthly")
    # 2025 fell into quarantine and THEN landed in bronze: already-resolved noise.
    await _semear_quarentena(app_engine, fonte, chave_lote="2025", dias_atras=40)
    await _semear_lote_bronze(app_engine, fonte, chave_lote="2025")
    # 2026 fell twice and never landed: that is the hole.
    await _semear_quarentena(app_engine, fonte, chave_lote="2026", dias_atras=20)
    await _semear_quarentena(
        app_engine, fonte, chave_lote="2026", codigo="schema_divergente", dias_atras=2
    )

    _, headers = await _leitura(owner_engine, cliente)
    r = await cliente.get("/ingestao/quarentena/resumo", headers=headers)
    assert r.status_code == 200, r.text
    (item,) = r.json()["fontes"]

    assert item["dataset"] == "registry_monthly"
    assert item["total"] == 3
    assert item["ultimos_7d"] == 1
    assert item["ultimos_30d"] == 2
    assert item["primeiro_em"] < item["ultimo_em"]
    # Top-3 codes, most frequent first.
    assert [c["codigo"] for c in item["codigos"]] == ["parse_falhou", "schema_divergente"]
    assert item["codigos"][0]["contagem"] == 2
    # The field that changes the decision: 3 quarantine rows, ONE slice with no data in bronze.
    assert item["fatias_sem_lote_bom"] == 1


async def test_resumo_nao_conta_fatia_no_futuro_como_buraco(cliente, owner_engine, app_engine):
    """A slice that has not happened yet is not missing data - same discipline as `404 = not published`.

    Without this cut, every future period would enter the count and the number that matters would
    drown in non-events (a DLQ flooded with non-events)."""
    fonte = await _semear_fonte(app_engine, nome="Quotes feed", dataset="quotes_daily")
    futuro = date.today().replace(day=1) + timedelta(days=400)
    await _semear_quarentena(app_engine, fonte, chave_lote=futuro.strftime("%Y-%m"))

    _, headers = await _leitura(owner_engine, cliente)
    r = await cliente.get("/ingestao/quarentena/resumo", headers=headers)
    (item,) = r.json()["fontes"]
    assert item["total"] == 1
    assert item["fatias_sem_lote_bom"] == 0


async def test_resumo_filtra_por_fonte_e_404_em_fonte_inexistente(
    cliente, owner_engine, app_engine
) -> None:
    """Error: a source that does not exist returns 404 in the standard envelope, before summarizing anything."""
    a = await _semear_fonte(app_engine, nome="Source A", dataset="ds_a")
    b = await _semear_fonte(app_engine, nome="Source B", dataset="ds_b")
    await _semear_quarentena(app_engine, a, chave_lote="2026-01")
    await _semear_quarentena(app_engine, b, chave_lote="2026-01")

    _, headers = await _leitura(owner_engine, cliente)
    r = await cliente.get(f"/ingestao/quarentena/resumo?fonte_id={a}", headers=headers)
    assert r.status_code == 200
    assert [i["dataset"] for i in r.json()["fontes"]] == ["ds_a"]

    r = await cliente.get(f"/ingestao/quarentena/resumo?fonte_id={uuid.uuid4()}", headers=headers)
    assert r.status_code == 404
    assert r.json()["erro"]["codigo"] == "nao_encontrado"
    assert "not found" in r.json()["erro"]["mensagem"]


async def test_resumo_respeita_o_rbac_da_borda(cliente, owner_engine) -> None:
    """Reading is allowed for specialist/admin/auditor; operator sees no ingestion at all."""
    _, aud = await usuario_logado(
        owner_engine, cliente, email="aud.quarantine@example.com", perfil="auditor"
    )
    assert (await cliente.get("/ingestao/quarentena/resumo", headers=aud)).status_code == 200

    _, op = await usuario_logado(
        owner_engine, cliente, email="op.quarantine@example.com", perfil="operador"
    )
    r = await cliente.get("/ingestao/quarentena/resumo", headers=op)
    assert r.status_code == 403
    assert r.json()["erro"]["codigo"] == "nao_autorizado"


async def test_resumo_nao_escreve_na_trilha(cliente, owner_engine, app_engine) -> None:
    """The summary is a READ: the audit trail is for writes, and a query generates no record."""
    fonte = await _semear_fonte(app_engine, nome="Source C", dataset="ds_c")
    await _semear_quarentena(app_engine, fonte, chave_lote="2026-01")
    _, headers = await _leitura(owner_engine, cliente)
    assert (await cliente.get("/ingestao/quarentena/resumo", headers=headers)).status_code == 200

    async with app_engine.connect() as conn:
        n = (
            await conn.execute(
                text("SELECT count(*) FROM auditoria WHERE recurso_tipo = 'ingestao_fonte'")
            )
        ).scalar_one()
    assert n == 0


# --- Retention: never deletes the evidence of the hole ----------------------------------------


async def test_expurgo_apaga_so_a_quarentena_ja_resolvida(app_engine) -> None:
    """Three old rows; only the one that later got a good bronze batch goes away.

    The row of a slice WITHOUT a good batch stays at any age: deleting it deletes the only evidence
    that the period never arrived. And the recent one stays because it is still useful operational
    history."""
    fonte = await _semear_fonte(app_engine, nome="Source D", dataset="ds_d")
    await _semear_quarentena(app_engine, fonte, chave_lote="2019", dias_atras=400)  # resolved
    await _semear_lote_bronze(app_engine, fonte, chave_lote="2019")
    await _semear_quarentena(app_engine, fonte, chave_lote="2020", dias_atras=400)  # hole
    await _semear_quarentena(app_engine, fonte, chave_lote="2021", dias_atras=5)  # recent
    await _semear_lote_bronze(app_engine, fonte, chave_lote="2021", sha="s2")

    async with transacao() as conn:
        simulado = await repo.expurgar_quarentena_resolvida(conn, manter_dias=180, simular=True)
    assert simulado == 1

    # `--simular` must not remove anything: a dry run that deletes is worse than no dry run.
    async with app_engine.connect() as conn:
        antes = (await conn.execute(text("SELECT count(*) FROM ingestao_quarentena"))).scalar_one()
    assert antes == 3

    async with transacao() as conn:
        removidas = await repo.expurgar_quarentena_resolvida(conn, manter_dias=180)
    assert removidas == 1

    async with app_engine.connect() as conn:
        restantes = [
            r[0]
            for r in (
                await conn.execute(
                    text("SELECT chave_lote FROM ingestao_quarentena ORDER BY chave_lote")
                )
            ).all()
        ]
    assert restantes == ["2020", "2021"]


async def test_expurgo_nao_apaga_buraco_por_mais_velho_que_seja(app_engine) -> None:
    """The error this code exists to prevent: pure age-based retention eats the evidence."""
    fonte = await _semear_fonte(app_engine, nome="Source E", dataset="ds_e")
    await _semear_quarentena(app_engine, fonte, chave_lote="2015", dias_atras=4000)

    async with transacao() as conn:
        assert await repo.expurgar_quarentena_resolvida(conn, manter_dias=1) == 0

    async with app_engine.connect() as conn:
        n = (await conn.execute(text("SELECT count(*) FROM ingestao_quarentena"))).scalar_one()
    assert n == 1


# --- Chronic-source signal --------------------------------------------------------------


async def test_fonte_cronica_e_quem_nao_traz_nada_ha_execucoes_seguidas(app_engine) -> None:
    """Three consecutive runs with only quarantine and no `novo` - that is the chronic source."""
    cronica = await _semear_fonte(app_engine, nome="Chronic source", dataset="ds_cronica")
    for mes, dias in ((3, 90), (4, 60), (5, 30)):
        ex = await _semear_execucao(app_engine, cronica, dias_atras=dias)
        await _semear_fatia(
            app_engine, ex, cronica, chave_lote=f"2026-{mes:02d}", desfecho="quarentena"
        )

    # Neighbor with the SAME history, but whose last run brought new data: not chronic.
    boa = await _semear_fonte(app_engine, nome="Good source", dataset="ds_boa")
    for mes, dias, desfecho in ((3, 90, "quarentena"), (4, 60, "quarentena"), (5, 30, "novo")):
        ex = await _semear_execucao(app_engine, boa, dias_atras=dias)
        await _semear_fatia(app_engine, ex, boa, chave_lote=f"2026-{mes:02d}", desfecho=desfecho)

    async with transacao() as conn:
        cronicas = await repo.fontes_cronicamente_quarentenadas(conn, minimo_execucoes=3)

    assert [c["dataset"] for c in cronicas] == ["ds_cronica"]
    assert cronicas[0]["execucoes_consecutivas"] == 3
    assert cronicas[0]["fatias_quarentena"] == 3
    assert cronicas[0]["desde_em"] < cronicas[0]["ultima_em"]


async def test_fonte_com_poucas_execucoes_ainda_nao_e_cronica(app_engine) -> None:
    """The error the minimum avoids: two consecutive failures are an incident, not chronic - and an
    alert that screams at the first stumble is an alert nobody reads at the third."""
    fonte = await _semear_fonte(app_engine, nome="New source", dataset="ds_nova")
    for mes, dias in ((1, 20), (2, 10)):
        ex = await _semear_execucao(app_engine, fonte, dias_atras=dias)
        await _semear_fatia(
            app_engine, ex, fonte, chave_lote=f"2026-{mes:02d}", desfecho="quarentena"
        )

    async with transacao() as conn:
        assert await repo.fontes_cronicamente_quarentenadas(conn, minimo_execucoes=3) == []


async def test_execucao_em_andamento_nao_inventa_cronica(app_engine) -> None:
    """A run still in progress does not count: it may not have reached the good slices yet."""
    fonte = await _semear_fonte(app_engine, nome="Running source", dataset="ds_rodando")
    for mes, dias in ((4, 60), (5, 30)):
        ex = await _semear_execucao(app_engine, fonte, dias_atras=dias)
        await _semear_fatia(
            app_engine, ex, fonte, chave_lote=f"2026-{mes:02d}", desfecho="quarentena"
        )
    agora = await _semear_execucao(app_engine, fonte, dias_atras=0, concluida=False)
    await _semear_fatia(app_engine, agora, fonte, chave_lote="2026-06", desfecho="quarentena")

    async with transacao() as conn:
        assert await repo.fontes_cronicamente_quarentenadas(conn, minimo_execucoes=3) == []

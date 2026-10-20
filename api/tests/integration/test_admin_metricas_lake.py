"""The lake in the admin panel, the per-source freshness SLO and readiness.

The panel advertised itself as "health and usage metrics" and **ignored the data lake entirely**:
no sources, no ingestion runs, no batches, no quarantine, no watermark age. Half of the platform -
precisely the half that talks to the outside world - had no numbers at all.

What these tests pin:

1. the `lake` block exists and counts what is in the database (not a mock);
2. the freshness SLO is **per source**, and tells the THREE states that matter apart - on time,
   late and **no applicable SLA**. Collapsing the third into "late" would paint red every
   `manual` source, and an alert that always screams is an alert nobody reads;
3. the SLA comes from the SAME cadence map that schedules ingestion
   (`ingestao.service.INTERVALO`) - a second table would diverge the day a cadence changed;
4. readiness answers for database + lake + workflow engine, and `/saude` **does not change format**
   (the CD health step greps for `"status":"ok"` literally).

No network: the state is seeded directly into the tables.
"""

from __future__ import annotations

import json
import uuid
from datetime import date

import pytest
from sqlalchemy import text

from cflow_core.admin import service as admin_service

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration


# --- Harness ------------------------------------------------------------------------------------


def _contrato(dataset: str, *, extra: dict | None = None) -> dict:
    c = {
        "classe": "http_file",
        "dataset": dataset,
        "cadencia": "manual",
        "janela": {"tipo": "mensal", "inicio": "2026-01"},
        "acesso": {"url_padrao": "https://example.invalid/{ano}{mes}.csv"},
        "formato": {"tipo": "csv", "encoding": "latin-1", "delimitador": ";"},
    }
    c.update(extra or {})
    return c


async def _semear_fonte(
    app_engine,
    *,
    nome: str,
    dataset: str,
    cadencia: str = "manual",
    ativo: bool = True,
    contrato: dict | None = None,
) -> uuid.UUID:
    async with app_engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO ingestao_fontes (nome, dataset, classe, contrato, cadencia, ativo) "
                    "VALUES (:n, :d, 'http_file', CAST(:c AS jsonb), :cad, :a) RETURNING id"
                ),
                {
                    "n": nome,
                    "d": dataset,
                    "c": json.dumps(contrato or _contrato(dataset)),
                    "cad": cadencia,
                    "a": ativo,
                },
            )
        ).scalar_one()


async def _semear_watermark(app_engine, fonte_id, *, chave: str, horas_atras: float) -> None:
    async with app_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO ingestao_watermarks (fonte_id, chave_lote, momento) "
                "VALUES (:f, :c, now() - make_interval(hours => CAST(:h AS int)))"
            ),
            {"f": fonte_id, "c": chave, "h": int(horas_atras)},
        )


async def _semear_execucao(app_engine, fonte_id, *, status: str = "concluida", horas: int = 1):
    async with app_engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO ingestao_execucoes (fonte_id, status, origem, iniciada_em, "
                    "concluida_em) VALUES (:f, :s, 'manual', "
                    "now() - make_interval(hours => CAST(:h AS int)), now()) RETURNING id"
                ),
                {"f": fonte_id, "s": status, "h": horas},
            )
        ).scalar_one()


async def _semear_lote(app_engine, fonte_id, *, chave: str, sha: str, bytes_: int, linhas: int):
    async with app_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO ingestao_lotes (fonte_id, chave_lote, sha256, status, bronze_uri, "
                "bytes, linhas, ingestion_date) VALUES (:f, :c, :s, 'bronze', :u, :b, :l, :dt)"
            ),
            {
                "f": fonte_id,
                "c": chave,
                "s": sha,
                "u": f"file:///lake/bronze/{chave}/dados.parquet",
                "b": bytes_,
                "l": linhas,
                "dt": date(2026, 1, 1),
            },
        )


async def _semear_quarentena(app_engine, fonte_id, *, chave: str, codigo: str = "parse_falhou"):
    async with app_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO ingestao_quarentena (fonte_id, chave_lote, etapa, codigo, mensagem) "
                "VALUES (:f, :c, 'parse', :co, 'failed')"
            ),
            {"f": fonte_id, "c": chave, "co": codigo},
        )


async def _admin(owner_engine, cliente, email="adm.lake@example.com"):
    return await usuario_logado(owner_engine, cliente, email=email, perfil="admin")


# --- 1. Lake block ---------------------------------------------------------------------------


async def test_bloco_lake_conta_o_que_existe(cliente, owner_engine, app_engine) -> None:
    """Happy path: sources, runs of the last 24 h, batches, quarantine and freshness."""
    fonte = await _semear_fonte(
        app_engine, nome="Rates feed", dataset="rates_daily", cadencia="diaria"
    )
    inativa = await _semear_fonte(
        app_engine, nome="Old feed", dataset="registry_yearly", ativo=False
    )
    await _semear_execucao(app_engine, fonte, status="concluida", horas=2)
    await _semear_execucao(app_engine, fonte, status="falha", horas=3)
    await _semear_lote(app_engine, fonte, chave="2026-08-01", sha="a1", bytes_=1000, linhas=50)
    await _semear_lote(app_engine, fonte, chave="2026-08-02", sha="a2", bytes_=2000, linhas=70)
    await _semear_quarentena(app_engine, fonte, chave="2026-08-03")

    _, h = await _admin(owner_engine, cliente)
    r = await cliente.get("/admin/metricas", headers=h)
    assert r.status_code == 200
    lake = r.json()["lake"]

    assert lake["fontes"]["total"] == 2
    assert lake["fontes"]["ativas"] == 1
    assert lake["fontes"]["por_cadencia"] == {"diaria": 1, "manual": 1}
    assert lake["execucoes_24h"] == {"concluida": 1, "falha": 1}
    assert lake["lotes"] == {"total": 2, "bronze": 2, "bytes": 3000, "linhas": 120}
    assert lake["quarentena"]["total"] == 1
    assert lake["quarentena"]["ultimas_24h"] == 1
    assert lake["quarentena"]["fontes_afetadas"] == 1
    # The quarantined slice never landed in bronze: it is a gap, not already-resolved noise.
    assert lake["quarentena"]["fatias_sem_lote_bom"] == 1
    assert {ln["fonte_id"] for ln in lake["frescor"]["fontes"]} == {str(fonte), str(inativa)}


async def test_painel_sem_lake_nenhum_nao_quebra(cliente, owner_engine, app_engine) -> None:
    """Boundary error: a database with NO registered source returns zeros, it neither blows up nor disappears."""
    _, h = await _admin(owner_engine, cliente, email="adm.vazio@example.com")
    lake = (await cliente.get("/admin/metricas", headers=h)).json()["lake"]
    assert lake["fontes"] == {"total": 0, "ativas": 0, "por_cadencia": {}}
    assert lake["lotes"]["bytes"] == 0
    assert lake["quarentena"]["fatias_sem_lote_bom"] == 0
    assert lake["frescor"]["fontes"] == []


async def test_metricas_lake_continua_so_de_admin(cliente, owner_engine, app_engine) -> None:
    """The new block does not loosen the edge: `/admin/metricas` stays admin-only."""
    _, h = await usuario_logado(
        owner_engine, cliente, email="esp.lake@example.com", perfil="especialista"
    )
    assert (await cliente.get("/admin/metricas", headers=h)).status_code == 403


# --- 2. Freshness SLO -----------------------------------------------------------------------


def test_sla_sai_do_mapa_de_cadencias_da_ingestao() -> None:
    """Interval + slack. `manual` has no SLA - and EVERY schedulable cadence has one."""
    from cflow_core.ingestao.service import INTERVALO

    assert admin_service.sla_frescor_horas("diaria") == 26.0
    assert admin_service.sla_frescor_horas("semanal") == 192.0  # 8 dias
    assert admin_service.sla_frescor_horas("mensal") == 960.0  # 40 dias
    assert admin_service.sla_frescor_horas("anual") == 9600.0  # 400 dias
    assert admin_service.sla_frescor_horas("manual") is None
    assert admin_service.sla_frescor_horas(None) is None
    # A new cadence in `INTERVALO` cannot be born without an SLA: the slack falls back to a fraction of the interval.
    for cadencia in INTERVALO:
        assert admin_service.sla_frescor_horas(cadencia) is not None


def test_override_do_contrato_vence_a_cadencia() -> None:
    """The contract is data: it is what gives a `manual` source an SLA - and it rejects garbage."""
    assert admin_service.sla_frescor_horas("manual", {"frescor": {"sla_horas": 72}}) == 72.0
    assert admin_service.sla_frescor_horas("diaria", {"frescor": {"sla_horas": 6}}) == 6.0
    # An invalid value does not become a silent SLA: it falls back to the cadence derivation.
    assert admin_service.sla_frescor_horas("diaria", {"frescor": {"sla_horas": 0}}) == 26.0
    assert admin_service.sla_frescor_horas("diaria", {"frescor": {"sla_horas": "soon"}}) == 26.0
    assert admin_service.sla_frescor_horas("diaria", {"frescor": True}) == 26.0
    assert admin_service.sla_frescor_horas("manual", {"frescor": {"sla_horas": -1}}) is None


async def test_frescor_separa_no_prazo_de_atrasada(cliente, owner_engine, app_engine) -> None:
    """Two daily sources, same cadence: one with a 2 h watermark, another with 5 days."""
    fresca = await _semear_fonte(
        app_engine, nome="Rates A", dataset="rates_series_daily", cadencia="diaria"
    )
    velha = await _semear_fonte(
        app_engine, nome="Rates B", dataset="bonds_daily", cadencia="diaria"
    )
    await _semear_watermark(app_engine, fresca, chave="2026-09-01", horas_atras=2)
    await _semear_watermark(app_engine, velha, chave="2026-08-27", horas_atras=120)

    _, h = await _admin(owner_engine, cliente, email="adm.frescor@example.com")
    lake = (await cliente.get("/admin/metricas", headers=h)).json()["lake"]
    por_id = {ln["fonte_id"]: ln for ln in lake["frescor"]["fontes"]}

    assert por_id[str(fresca)]["dentro_do_sla"] is True
    assert por_id[str(fresca)]["referencia"] == "watermark"
    assert por_id[str(fresca)]["sla_horas"] == 26.0
    assert por_id[str(velha)]["dentro_do_sla"] is False
    assert por_id[str(velha)]["atraso_horas"] >= 119
    assert lake["frescor"]["fora_do_sla"] == 1
    # Worst first: the late one heads the list, otherwise finding the only one that matters means scanning dozens.
    assert lake["frescor"]["fontes"][0]["fonte_id"] == str(velha)


async def test_execucao_que_nao_trouxe_dado_nao_mascara_o_atraso(
    cliente, owner_engine, app_engine
) -> None:
    """The gap that only the watermark sees: the source runs every day and has brought nothing for months.

    Measuring freshness by `ultima_execucao_em` would show green - the run was just now. The reference
    is the watermark precisely because 404 and `304` on everything produce a healthy run and stale data.
    """
    fonte = await _semear_fonte(
        app_engine, nome="Fund feed", dataset="fund_report_daily", cadencia="diaria"
    )
    await _semear_watermark(app_engine, fonte, chave="2026-06-01", horas_atras=2000)
    await _semear_execucao(app_engine, fonte, status="concluida", horas=1)

    _, h = await _admin(owner_engine, cliente, email="adm.wm@example.com")
    lake = (await cliente.get("/admin/metricas", headers=h)).json()["lake"]
    linha = lake["frescor"]["fontes"][0]
    assert linha["referencia"] == "watermark"
    assert linha["dentro_do_sla"] is False
    assert linha["ultima_execucao_em"] is not None  # the recent run appears, but does not decide


async def test_sem_sla_e_um_terceiro_estado(cliente, owner_engine, app_engine) -> None:
    """`manual` and inactive sources are not "late": they are sources with no applicable SLA.

    Without that third state, the panel is born with every source red (stopping the cadence moves
    them all to `manual`) - and an alert that is born noisy is read by nobody.
    """
    manual = await _semear_fonte(app_engine, nome="Registry feed", dataset="registry_monthly")
    inativa = await _semear_fonte(
        app_engine, nome="Quotes feed", dataset="quotes_daily", cadencia="diaria", ativo=False
    )
    await _semear_watermark(app_engine, manual, chave="2020-01", horas_atras=20000)
    await _semear_watermark(app_engine, inativa, chave="2020-01", horas_atras=20000)

    _, h = await _admin(owner_engine, cliente, email="adm.semsla@example.com")
    lake = (await cliente.get("/admin/metricas", headers=h)).json()["lake"]
    por_id = {ln["fonte_id"]: ln for ln in lake["frescor"]["fontes"]}

    assert por_id[str(manual)]["sla_horas"] is None
    assert por_id[str(manual)]["dentro_do_sla"] is None
    assert por_id[str(manual)]["atraso_horas"] > 19000  # the delay is measured even without an SLA
    assert por_id[str(inativa)]["sla_horas"] == 26.0  # the cadence has an SLA...
    assert por_id[str(inativa)]["dentro_do_sla"] is None  # ...but the source is paused
    assert lake["frescor"]["fora_do_sla"] == 0
    assert lake["frescor"]["sem_sla"] == 2


async def test_fonte_que_nunca_rodou_nao_esta_fresca(cliente, owner_engine, app_engine) -> None:
    """With an SLA and neither watermark nor run: outside the SLA, with a `null` delay (not zero).

    Zero hours of delay would read as "just ran", which is the opposite of what happened.
    """
    fonte = await _semear_fonte(app_engine, nome="New feed", dataset="new_feed", cadencia="semanal")
    _, h = await _admin(owner_engine, cliente, email="adm.nunca@example.com")
    lake = (await cliente.get("/admin/metricas", headers=h)).json()["lake"]
    linha = lake["frescor"]["fontes"][0]
    assert linha["fonte_id"] == str(fonte)
    assert linha["referencia"] is None
    assert linha["atraso_horas"] is None
    assert linha["dentro_do_sla"] is False


# --- 3. Health and readiness --------------------------------------------------------------------


async def test_saude_mantem_o_formato_que_o_cd_grepa(cliente) -> None:
    """The CD health step greps for the literal `"status":"ok"`: the `versao` field is an ADDITION."""
    r = await cliente.get("/saude")
    assert r.status_code == 200
    # Literal, as the CI `grep -q '"status":"ok"'` sees it: FastAPI JSONResponse serialises
    # without a space between key and value, and the CD depends on that string.
    assert '"status":"ok"' in r.text
    corpo = r.json()
    assert corpo["status"] == "ok"
    assert corpo["banco"] == "ok"
    assert corpo["momento"]
    assert corpo["versao"]  # without the environment variable, "desconhecida" - never absent


def test_versao_vem_do_ambiente_com_fallback(monkeypatch) -> None:
    from cflow_api.routes import health

    for chave in ("CFLOW_VERSAO", "GIT_SHA", "GITHUB_SHA"):
        monkeypatch.delenv(chave, raising=False)
    assert health.versao_no_ar() == "desconhecida"
    monkeypatch.setenv("GIT_SHA", "0123456789abcdef0")
    assert health.versao_no_ar() == "0123456789ab"  # short SHA
    monkeypatch.setenv("CFLOW_VERSAO", "v1.2.3")
    assert health.versao_no_ar() == "v1.2.3"  # the explicit one wins


async def test_pronto_responde_pelas_tres_dependencias(cliente) -> None:
    """Readiness: database + lake + workflow engine. With no registered source, the lake tested is the local one."""
    from cflow_api.routes import health

    health.limpar_cache_readiness()
    r = await cliente.get("/saude/pronto")
    corpo = r.json()
    if r.status_code == 200:
        assert corpo["status"] == "pronto"
        assert set(corpo["checagens"]) == {"banco", "lake", "temporal"}
        assert corpo["checagens"]["banco"]["ok"] is True
        assert corpo["checagens"]["lake"]["ok"] is True
    else:
        # A test environment without the workflow engine up is a legitimate outcome - what is proven
        # here is that the refusal NAMES who failed, instead of a mute 503.
        assert r.status_code == 503
        assert corpo["erro"]["codigo"] == "nao_pronto"
        assert {d["campo"] for d in corpo["erro"]["detalhes"]} <= {"banco", "lake", "temporal"}
        assert all(d["mensagem"] for d in corpo["erro"]["detalhes"])


async def test_pronto_reprova_com_lake_inalcancavel(cliente, app_engine, monkeypatch) -> None:
    """Error: a lake destination that does not open fails readiness - with the cause, not a stack trace."""
    from cflow_api.routes import health
    from cflow_core.ingestao import armazenamento

    health.limpar_cache_readiness()
    await _semear_fonte(app_engine, nome="Remote feed", dataset="remote_feed")

    class _Quebrado:
        async def testar(self) -> dict:
            return {"ok": False, "causa": "bucket 'cflow-bronze' does not exist"}

    async def _abrir(_conexao_id):
        return _Quebrado()

    monkeypatch.setattr(armazenamento, "abrir", _abrir)
    r = await cliente.get("/saude/pronto")
    assert r.status_code == 503
    corpo = r.json()["erro"]
    assert corpo["codigo"] == "nao_pronto"
    lake = next(d for d in corpo["detalhes"] if d["campo"] == "lake")
    assert "bucket" in lake["mensagem"]


async def test_pronto_nao_martela_o_lake_a_cada_probe(cliente, app_engine, monkeypatch) -> None:
    """The lake verdict is reused for a short window.

    A naive readiness makes one request per bucket on EVERY probe - at a 5 s interval, tens of
    thousands of calls per day per replica to always answer the same thing.
    """
    from cflow_api.routes import health
    from cflow_core.ingestao import armazenamento

    health.limpar_cache_readiness()
    await _semear_fonte(app_engine, nome="Local feed", dataset="local_feed")
    chamadas = {"n": 0}

    class _Contador:
        async def testar(self) -> dict:
            chamadas["n"] += 1
            return {"ok": True, "provedor": "local"}

    async def _abrir(_conexao_id):
        return _Contador()

    monkeypatch.setattr(armazenamento, "abrir", _abrir)
    await cliente.get("/saude/pronto")
    await cliente.get("/saude/pronto")
    await cliente.get("/saude/pronto")
    assert chamadas["n"] == 1
    health.limpar_cache_readiness()  # credential rotation forces a recheck
    await cliente.get("/saude/pronto")
    assert chamadas["n"] == 2

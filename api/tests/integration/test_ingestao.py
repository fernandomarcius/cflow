"""External ingestion end to end, with the origin simulated by an httpx transport.

What these tests prove is what separates production ingestion from a `curl` in a cron job:
hash idempotency, ETag-based incremental runs, quarantine that does not bring down the window,
replay from landing, a watermark that only advances when it can, and an append-only audit trail
recording everything.
"""

from __future__ import annotations

import io
import json
import re
import uuid
import zipfile
from datetime import date

import httpx
import pytest
from sqlalchemy import text

from cflow_core.ingestao import repositorio as repo
from cflow_core.ingestao import service

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration

# The non-ASCII value is deliberate test data: it exercises the latin-1 decoding path.
CSV = "ENTITY_ID;REF_DATE;AMOUNT;NAME\nE-000;2026-08-01;1,2345;CAFÉ CRÈME\n"


# --- Harness ----------------------------------------------------------------------------------


def _zip_registry(conteudo: str = CSV) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("registry_202608.csv", conteudo.encode("latin-1"))
    return buf.getvalue()


def contrato_registry(dataset: str = "registry_test", **over) -> dict:
    c = {
        "classe": "http_file",
        "dataset": dataset,
        "descricao": "Simulated registry",
        "cadencia": "manual",
        "janela": {"tipo": "mensal", "inicio": "2026-08"},
        "acesso": {
            "url_padrao": "https://example.com/files/registry_{ano}{mes}.zip",
            "condicional": True,
        },
        "formato": {
            "tipo": "csv",
            "encoding": "latin-1",
            "delimitador": ";",
            "compressao": "zip",
            "arquivo_no_zip": r"\.csv$",
        },
        "schema_esperado": ["ENTITY_ID", "REF_DATE", "AMOUNT"],
        "limites": {"concorrencia": 2, "timeout_segundos": 30},
    }
    c.update(over)
    return c


def contrato_rates(dataset: str = "rates_test", inicio: str = "2016-01-01") -> dict:
    return {
        "classe": "rest_api",
        "dataset": dataset,
        "cadencia": "manual",
        "janela": {"tipo": "periodo", "inicio": inicio, "passo_max_dias": 3650},
        "acesso": {
            "url_padrao": "https://example.com/api/series/{serie}/data",
            "variaveis": {"serie": "11"},
            "params": {"formato": "json"},
            "param_inicio": "dataInicial",
            "param_fim": "dataFinal",
            "formato_data": "%d/%m/%Y",
        },
        "formato": {"tipo": "json"},
        "schema_esperado": ["data", "valor"],
    }


class Origem:
    """Simulated origin: counts calls and answers 304 when `If-None-Match` matches.

    This is the REAL behavior of origins that send an ETag; without simulating it, the incremental
    test would only prove the code does not blow up, not that it saves the download."""

    def __init__(self, corpo: bytes, etag: str = '"v1"', tipo: str = "application/zip") -> None:
        self.corpo = corpo
        self.etag = etag
        self.tipo = tipo
        self.chamadas: list[httpx.Request] = []
        self.status_forcado: int | None = None

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.chamadas.append(req)
        if self.status_forcado:
            return httpx.Response(self.status_forcado, text="simulated error")
        if req.headers.get("If-None-Match") == self.etag:
            return httpx.Response(304, headers={"ETag": self.etag})
        return httpx.Response(
            200,
            content=self.corpo,
            headers={
                "ETag": self.etag,
                "Content-Type": self.tipo,
                "Last-Modified": "Sat, 15 Aug 2026 09:12:57 GMT",
            },
        )


@pytest.fixture
def origem(monkeypatch):
    """Injects the simulated transport into every `httpx.AsyncClient`; no test touches the network."""
    o = Origem(_zip_registry())
    original = httpx.AsyncClient

    def _fabrica(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(o.handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _fabrica)
    return o


@pytest.fixture
def lake(tmp_path, monkeypatch):
    """Local lake isolated per test (nothing is left behind in the repo)."""
    from cflow_core.config import get_settings

    monkeypatch.setenv("CFLOW_LAKE_DIR", str(tmp_path / "lake"))
    get_settings.cache_clear()
    yield tmp_path / "lake"
    get_settings.cache_clear()


async def _criar_fonte(cliente, headers, contrato: dict, nome: str = "Test source") -> str:
    r = await cliente.post(
        "/ingestao/fontes", json={"nome": nome, "contrato": contrato}, headers=headers
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _admin(owner_engine, cliente, email="admin.ingestao@example.com"):
    return await usuario_logado(owner_engine, cliente, email=email, perfil="admin")


async def _rodar(fonte_id: str, *, inicio=None, fim=None, replay=False, origem_exec="manual"):
    """Runs ingestion through the inline path (`service.processar`), which is the SAME engine the
    activity calls; the Temporal path has its own test (test_ingestao_workflow)."""
    ctx = await service.preparar(
        fonte_id=uuid.UUID(fonte_id),
        origem="replay" if replay else origem_exec,
        inicio=inicio,
        fim=fim,
        ator_id=None,
    )
    totais = await service.processar(
        fonte_id=fonte_id, execucao_id=ctx["execucao_id"], fatias_ctx=ctx["fatias"], replay=replay
    )
    return ctx, totais


# --- Registry and authorization --------------------------------------------------------------


async def test_criar_fonte_registra_na_trilha(cliente, owner_engine, app_engine) -> None:
    _, headers = await _admin(owner_engine, cliente)
    fid = await _criar_fonte(cliente, headers, contrato_registry())

    async with app_engine.connect() as conn:
        r = (
            (
                await conn.execute(
                    text(
                        "SELECT acao, recurso_tipo, recurso_id, depois FROM auditoria "
                        "WHERE recurso_tipo = 'ingestao_fonte'"
                    )
                )
            )
            .mappings()
            .all()
        )
    assert len(r) == 1
    assert r[0]["acao"] == "criar" and str(r[0]["recurso_id"]) == fid
    assert r[0]["depois"]["contrato"]["dataset"] == "registry_test"


async def test_so_admin_cadastra_fonte(cliente, owner_engine) -> None:
    _, esp = await usuario_logado(
        owner_engine, cliente, email="esp@example.com", perfil="especialista"
    )
    r = await cliente.post(
        "/ingestao/fontes", json={"nome": "X", "contrato": contrato_registry()}, headers=esp
    )
    assert r.status_code == 403
    assert r.json()["erro"]["codigo"] == "nao_autorizado"


async def test_operador_nem_le(cliente, owner_engine) -> None:
    _, op = await usuario_logado(owner_engine, cliente, email="op@example.com", perfil="operador")
    assert (await cliente.get("/ingestao/fontes", headers=op)).status_code == 403


async def test_auditor_le_mas_nao_executa(cliente, owner_engine) -> None:
    _, adm = await _admin(owner_engine, cliente)
    fid = await _criar_fonte(cliente, adm, contrato_registry())
    _, aud = await usuario_logado(owner_engine, cliente, email="aud@example.com", perfil="auditor")
    assert (await cliente.get("/ingestao/fontes", headers=aud)).status_code == 200
    r = await cliente.post(f"/ingestao/fontes/{fid}/executar", headers=aud)
    assert r.status_code == 403


async def test_contrato_invalido_devolve_422(cliente, owner_engine) -> None:
    _, headers = await _admin(owner_engine, cliente)
    ruim = contrato_registry()
    ruim["formato"] = {"tipo": "csv", "delimitador": ";"}  # no encoding
    r = await cliente.post(
        "/ingestao/fontes", json={"nome": "Bad", "contrato": ruim}, headers=headers
    )
    assert r.status_code == 422
    corpo = r.json()["erro"]
    assert corpo["codigo"] == "contrato_invalido"
    assert "encoding" in " ".join(d["mensagem"] for d in corpo["detalhes"])
    assert corpo["request_id"]


async def test_dataset_duplicado_e_409(cliente, owner_engine) -> None:
    _, headers = await _admin(owner_engine, cliente)
    await _criar_fonte(cliente, headers, contrato_registry(), nome="First")
    r = await cliente.post(
        "/ingestao/fontes",
        json={"nome": "Second", "contrato": contrato_registry()},
        headers=headers,
    )
    assert r.status_code == 409


async def test_dataset_nao_pode_mudar(cliente, owner_engine) -> None:
    """The dataset is the path in the lake: changing it would orphan the history already landed."""
    _, headers = await _admin(owner_engine, cliente)
    fid = await _criar_fonte(cliente, headers, contrato_registry())
    r = await cliente.patch(
        f"/ingestao/fontes/{fid}",
        json={"contrato": contrato_registry("outro_dataset")},
        headers=headers,
    )
    assert r.status_code == 409 and r.json()["erro"]["codigo"] == "dataset_imutavel"


async def test_editar_contrato_preserva_cadencia_e_cadencia_continua_editavel(
    cliente, owner_engine
) -> None:
    """Editing only the contract must not touch cadence or its trigger (`proxima_em`); cadence
    itself stays editable, and a non-manual cadence needs a trigger."""
    _, headers = await _admin(owner_engine, cliente)
    fid = await _criar_fonte(cliente, headers, contrato_registry())
    r = await cliente.patch(f"/ingestao/fontes/{fid}", json={"cadencia": "mensal"}, headers=headers)
    assert r.status_code == 204, r.text
    r = await cliente.patch(f"/ingestao/fontes/{fid}", json={"cadencia": "manual"}, headers=headers)
    assert r.status_code == 204, r.text

    novo = contrato_registry(descricao="Edited description")
    r = await cliente.patch(f"/ingestao/fontes/{fid}", json={"contrato": novo}, headers=headers)
    assert r.status_code == 204, r.text
    fonte = await _fonte_na_lista(cliente, headers, "registry_test")
    assert fonte["cadencia"] == "manual"
    assert fonte["proxima_em"] is None

    r = await cliente.patch(f"/ingestao/fontes/{fid}", json={"cadencia": "diaria"}, headers=headers)
    assert r.status_code == 204, r.text
    fonte = await _fonte_na_lista(cliente, headers, "registry_test")
    assert fonte["cadencia"] == "diaria"
    assert fonte["proxima_em"] is not None, "a non-manual cadence needs a trigger"


async def test_destino_precisa_ser_conexao_objeto(cliente, owner_engine) -> None:
    _, headers = await _admin(owner_engine, cliente)
    r = await cliente.post(
        "/conexoes",
        json={"nome": "Any PG", "tipo": "postgres", "config": {"host": "x"}},
        headers=headers,
    )
    conexao_id = r.json()["id"]
    r = await cliente.post(
        "/ingestao/fontes",
        json={"nome": "F", "contrato": contrato_registry(), "destino_conexao_id": conexao_id},
        headers=headers,
    )
    assert r.status_code == 400 and r.json()["erro"]["codigo"] == "destino_invalido"


async def test_conexao_objeto_local_e_testavel(cliente, owner_engine, tmp_path) -> None:
    _, headers = await _admin(owner_engine, cliente)
    r = await cliente.post(
        "/conexoes",
        json={
            "nome": "Lake dev",
            "tipo": "objeto",
            "config": {"provedor": "local", "raiz": str(tmp_path / "lake")},
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    teste = await cliente.post(f"/conexoes/{r.json()['id']}/testar", headers=headers)
    assert teste.status_code == 200 and teste.json()["ok"] is True


# --- Ingestion: landing -> bronze ---------------------------------------------------------


async def test_landing_e_bronze_com_manifesto_e_checksum(
    cliente, owner_engine, origem, lake
) -> None:
    import hashlib

    _, headers = await _admin(owner_engine, cliente)
    fid = await _criar_fonte(cliente, headers, contrato_registry())
    ctx, totais = await _rodar(fid, inicio=date(2026, 8, 1), fim=date(2026, 8, 31))

    assert totais == {
        "lotes": 1,
        "novos": 1,
        "reusados": 0,
        "quarentena": 0,
        "nao_modificados": 0,
        "nao_publicados": 0,
    }

    r = await cliente.get(f"/ingestao/execucoes/{ctx['execucao_id']}", headers=headers)
    detalhe = r.json()
    assert detalhe["status"] == "concluida"
    lote = detalhe["lotes"][0]
    assert lote["chave_lote"] == "2026-08" and lote["status"] == "bronze" and lote["linhas"] == 1

    esperado = hashlib.sha256(origem.corpo).hexdigest()
    assert lote["sha256"] == esperado

    # Immutable landing, partitioned by ingestion_date, with the hash in the path.
    landing = list((lake / "landing" / "registry_test").rglob("*.zip"))
    assert len(landing) == 1
    assert f"ingestion_date={lote['ingestion_date']}" in str(landing[0])
    assert esperado[:12] in str(landing[0])
    assert landing[0].read_bytes() == origem.corpo  # RAW bytes, exactly as they arrived

    manifesto = json.loads((landing[0].parent / "_manifesto.json").read_text())
    assert manifesto["sha256"] == esperado
    assert manifesto["encoding"] == "latin-1"
    assert manifesto["etag"] == '"v1"'
    assert manifesto["janela"] == {"inicio": "2026-08-01", "fim": "2026-08-31"}
    assert manifesto["momento"].endswith("-03:00")  # explicit local-timezone offset
    assert manifesto["contrato_hash"]

    # Bronze: Parquet + manifest with rows/columns.
    parquet = list((lake / "bronze" / "registry_test").rglob("*.parquet"))
    assert len(parquet) == 1
    mb = json.loads((parquet[0].parent / "_manifesto.json").read_text())
    assert mb["linhas"] == 1 and "ENTITY_ID" in mb["colunas"]


async def test_bronze_preserva_acento_da_origem_latin1(cliente, owner_engine, origem, lake) -> None:
    """Exists because of mojibake: a latin-1 accent decoded as UTF-8 comes out garbled."""
    import duckdb

    _, headers = await _admin(owner_engine, cliente)
    fid = await _criar_fonte(cliente, headers, contrato_registry())
    await _rodar(fid, inicio=date(2026, 8, 1), fim=date(2026, 8, 31))

    parquet = list((lake / "bronze").rglob("*.parquet"))[0]
    con = duckdb.connect()
    try:
        denom = con.execute(f"SELECT NAME FROM read_parquet('{parquet}')").fetchone()[0]
        assert denom == "CAFÉ CRÈME"
    finally:
        con.close()


async def test_reexecucao_com_mesmo_hash_e_no_op(cliente, owner_engine, origem, lake) -> None:
    """Idempotency: the second pass writes nothing new, and does not even download (304)."""
    _, headers = await _admin(owner_engine, cliente)
    fid = await _criar_fonte(cliente, headers, contrato_registry())
    await _rodar(fid, inicio=date(2026, 8, 1), fim=date(2026, 8, 31))
    antes = len(list((lake / "landing").rglob("*.zip")))

    _, totais = await _rodar(fid, inicio=date(2026, 8, 1), fim=date(2026, 8, 31))
    assert totais["novos"] == 0
    assert totais["nao_modificados"] == 1
    assert len(list((lake / "landing").rglob("*.zip"))) == antes

    condicionais = [c for c in origem.chamadas if c.headers.get("If-None-Match")]
    assert condicionais, (
        "the 2nd run must send If-None-Match (otherwise it re-downloads for nothing)"
    )


async def test_sem_condicional_mesmo_hash_ainda_e_reuso(
    cliente, owner_engine, origem, lake
) -> None:
    """Origin that sends no ETag: the payload hash still prevents the duplicate batch."""
    _, headers = await _admin(owner_engine, cliente)
    contrato = contrato_registry()
    contrato["acesso"] = {**contrato["acesso"], "condicional": False}
    fid = await _criar_fonte(cliente, headers, contrato)

    await _rodar(fid, inicio=date(2026, 8, 1), fim=date(2026, 8, 31))
    _, totais = await _rodar(fid, inicio=date(2026, 8, 1), fim=date(2026, 8, 31))
    assert totais["novos"] == 0 and totais["reusados"] == 1
    assert len(list((lake / "landing").rglob("*.zip"))) == 1


async def test_conteudo_novo_gera_lote_novo_sem_apagar_o_anterior(
    cliente, owner_engine, origem, lake
) -> None:
    """The origin republishes the current month: both versions coexist (landing is append-only)."""
    _, headers = await _admin(owner_engine, cliente)
    fid = await _criar_fonte(cliente, headers, contrato_registry())
    await _rodar(fid, inicio=date(2026, 8, 1), fim=date(2026, 8, 31))

    origem.corpo = _zip_registry(CSV + "E-111;2026-08-02;9,9999;OTHER ENTITY\n")
    origem.etag = '"v2"'
    _, totais = await _rodar(fid, inicio=date(2026, 8, 1), fim=date(2026, 8, 31))

    assert totais["novos"] == 1
    assert len(list((lake / "landing").rglob("*.zip"))) == 2  # v1 is still there
    lotes = await repo.lotes_da_fonte(uuid.UUID(fid))
    assert lotes[0]["chave_lote"] == "2026-08"  # the most recent of the slice


# --- Watermark ------------------------------------------------------------------------------


async def test_watermark_avanca_e_dirige_o_incremental(cliente, owner_engine, origem, lake) -> None:
    _, headers = await _admin(owner_engine, cliente)
    fid = await _criar_fonte(cliente, headers, contrato_registry())
    await _rodar(fid, inicio=date(2026, 8, 1), fim=date(2026, 8, 31))

    wm = await repo.ler_watermark(fid)
    assert wm["chave_lote"] == "2026-08"

    # Without an explicit window, the next run starts at the watermark (and reprocesses its slice,
    # because the origin still republishes the current period).
    ctx = await service.preparar(
        fonte_id=uuid.UUID(fid), origem="manual", inicio=None, fim=None, ator_id=None
    )
    assert ctx["janela"]["inicio"] == "2026-08-01"


async def test_janela_sem_nenhuma_fatia_conclui_zerada(cliente, owner_engine, origem, lake) -> None:
    """Window entirely below the contract floor: nothing to do is `concluida` with zeros, not an
    error (it is what happens every day on a source that is already up to date)."""
    _, headers = await _admin(owner_engine, cliente)
    fid = await _criar_fonte(cliente, headers, contrato_registry())  # floor: 2026-08
    r = await cliente.post(
        f"/ingestao/fontes/{fid}/executar",
        json={"inicio": "2020-01-01", "fim": "2020-03-31"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    corpo = r.json()
    assert corpo["status"] == "concluida"
    assert corpo["totais"]["lotes"] == 0
    assert corpo["fatias"] == []
    assert not origem.chamadas


async def test_watermark_nao_avanca_com_fatia_em_quarentena(
    cliente, owner_engine, origem, lake
) -> None:
    """Marking "reached August" with July quarantined is the gap that is never retried."""
    _, headers = await _admin(owner_engine, cliente)
    fid = await _criar_fonte(cliente, headers, contrato_registry())
    origem.corpo = b"<html>error page</html>"
    await _rodar(fid, inicio=date(2026, 8, 1), fim=date(2026, 8, 31))
    assert await repo.ler_watermark(fid) is None


# --- DLQ / quarantine -----------------------------------------------------------------------


async def test_payload_corrompido_vai_para_quarentena(cliente, owner_engine, origem, lake) -> None:
    _, headers = await _admin(owner_engine, cliente)
    fid = await _criar_fonte(cliente, headers, contrato_registry())
    origem.corpo = b"<html>500 Internal Server Error</html>"

    ctx, totais = await _rodar(fid, inicio=date(2026, 8, 1), fim=date(2026, 8, 31))
    assert totais["quarentena"] == 1 and totais["novos"] == 0

    itens = (await cliente.get("/ingestao/quarentena", headers=headers)).json()["itens"]
    assert len(itens) == 1
    assert itens[0]["codigo"] == "zip_invalido" and itens[0]["etapa"] == "decodificacao"
    assert itens[0]["chave_lote"] == "2026-08"

    # The raw payload is kept: it is what you open to find out what the origin sent.
    quarentena = list((lake / "quarantine").rglob("*"))
    assert any(p.is_file() and p.read_bytes() == origem.corpo for p in quarentena)
    # And bronze was NOT written.
    assert not list((lake / "bronze").rglob("*.parquet"))


async def test_uma_fatia_ruim_nao_derruba_a_janela(
    cliente, owner_engine, monkeypatch, lake
) -> None:
    """The point of the DLQ: a broken August must not make July and September vanish."""
    o = Origem(_zip_registry())

    def _handler(req: httpx.Request) -> httpx.Response:
        o.chamadas.append(req)
        if "202608" in str(req.url):
            return httpx.Response(200, content=b"not a zip", headers={"ETag": '"x"'})
        return httpx.Response(200, content=o.corpo, headers={"ETag": '"v1"'})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **k: original(*a, **{**k, "transport": httpx.MockTransport(_handler)}),
    )

    _, headers = await _admin(owner_engine, cliente)
    contrato = contrato_registry()
    contrato["janela"] = {"tipo": "mensal", "inicio": "2026-07"}
    fid = await _criar_fonte(cliente, headers, contrato)

    _, totais = await _rodar(fid, inicio=date(2026, 7, 1), fim=date(2026, 9, 30))
    assert totais["lotes"] == 3
    assert totais["novos"] == 2 and totais["quarentena"] == 1


async def test_falha_de_download_vai_para_quarentena(cliente, owner_engine, origem, lake) -> None:
    """403 (origin block) is a failure: unlike 404, which means "not published"."""
    _, headers = await _admin(owner_engine, cliente)
    contrato = contrato_registry()
    contrato["limites"] = {"tentativas": 1, "timeout_segundos": 5}
    fid = await _criar_fonte(cliente, headers, contrato)
    origem.status_forcado = 403

    _, totais = await _rodar(fid, inicio=date(2026, 8, 1), fim=date(2026, 8, 31))
    assert totais["quarentena"] == 1
    itens = (await cliente.get("/ingestao/quarentena", headers=headers)).json()["itens"]
    assert itens[0]["etapa"] == "download"


async def test_schema_quebrado_vai_para_quarentena(cliente, owner_engine, origem, lake) -> None:
    """The origin changed its layout: named quarantine, not bronze with a missing column."""
    _, headers = await _admin(owner_engine, cliente)
    contrato = contrato_registry(schema_esperado=["ENTITY_ID", "NEW_REQUIRED_COLUMN"])
    fid = await _criar_fonte(cliente, headers, contrato)

    _, totais = await _rodar(fid, inicio=date(2026, 8, 1), fim=date(2026, 8, 31))
    assert totais["quarentena"] == 1
    itens = (await cliente.get("/ingestao/quarentena", headers=headers)).json()["itens"]
    assert itens[0]["codigo"] == "schema_inesperado" and itens[0]["etapa"] == "validacao"
    # Landing KEPT the payload anyway: replay will rebuild from it later.
    assert list((lake / "landing").rglob("*.zip"))


# --- Replay ---------------------------------------------------------------------------------


async def test_replay_reconstroi_bronze_sem_tocar_a_rede(
    cliente, owner_engine, origem, lake
) -> None:
    """Why landing is immutable: the bronze rule changed, so reprocess without downloading anything."""
    _, headers = await _admin(owner_engine, cliente)
    contrato = contrato_registry(schema_esperado=["ENTITY_ID", "MISSING_COLUMN"])
    fid = await _criar_fonte(cliente, headers, contrato)
    await _rodar(fid, inicio=date(2026, 8, 1), fim=date(2026, 8, 31))
    assert not list((lake / "bronze").rglob("*.parquet"))

    # Fix the contract (the expected schema was wrong) and reprocess from landing.
    r = await cliente.patch(
        f"/ingestao/fontes/{fid}", json={"contrato": contrato_registry()}, headers=headers
    )
    assert r.status_code == 204
    chamadas_antes = len(origem.chamadas)

    _, totais = await _rodar(fid, inicio=date(2026, 8, 1), fim=date(2026, 8, 31), replay=True)
    assert totais["novos"] == 1
    assert len(origem.chamadas) == chamadas_antes, "replay must NOT touch the network"
    assert len(list((lake / "bronze").rglob("*.parquet"))) == 1


async def test_replay_e_admin(cliente, owner_engine) -> None:
    _, adm = await _admin(owner_engine, cliente)
    fid = await _criar_fonte(cliente, adm, contrato_registry())
    _, esp = await usuario_logado(
        owner_engine, cliente, email="e2@example.com", perfil="especialista"
    )
    r = await cliente.post(
        f"/ingestao/fontes/{fid}/replay",
        json={"inicio": "2026-08-01", "fim": "2026-08-31"},
        headers=esp,
    )
    assert r.status_code == 403


# --- Estimate (backfill/replay dry-run) -----------------------------------------------------


async def test_estimativa_devolve_a_mesma_contagem_da_execucao_real(cliente, owner_engine) -> None:
    """The UI shows this count BEFORE enabling the backfill/replay click; it has to be the number
    that will actually run, not a rough estimate. Proven by comparing with `contrato.fatias`
    called directly, the same function the real run uses underneath."""
    from cflow_core.ingestao.contrato import fatias as recortar_fatias

    _, adm = await _admin(owner_engine, cliente)
    contrato = contrato_registry()
    fid = await _criar_fonte(cliente, adm, contrato)

    r = await cliente.get(
        f"/ingestao/fontes/{fid}/estimativa",
        params={"inicio": "2026-08-01", "fim": "2026-08-31"},
        headers=adm,
    )
    assert r.status_code == 200, r.text
    corpo = r.json()
    assert corpo["dataset"] == contrato["dataset"]
    assert corpo["classe"] == "http_file"
    assert corpo["janela"] == {"inicio": "2026-08-01", "fim": "2026-08-31"}

    esperado = recortar_fatias(contrato, date(2026, 8, 1), date(2026, 8, 31))
    assert corpo["fatias"] == len(esperado) == 1


async def test_estimativa_nao_toca_a_rede(cliente, owner_engine, origem) -> None:
    _, adm = await _admin(owner_engine, cliente)
    fid = await _criar_fonte(cliente, adm, contrato_registry())
    chamadas_antes = len(origem.chamadas)

    r = await cliente.get(
        f"/ingestao/fontes/{fid}/estimativa",
        params={"inicio": "2026-08-01", "fim": "2026-08-31"},
        headers=adm,
    )
    assert r.status_code == 200, r.text
    assert len(origem.chamadas) == chamadas_antes


@pytest.mark.parametrize("perfil", ["especialista", "operador", "auditor"])
async def test_estimativa_e_admin(cliente, owner_engine, perfil) -> None:
    _, adm = await _admin(owner_engine, cliente)
    fid = await _criar_fonte(cliente, adm, contrato_registry())
    _, headers = await usuario_logado(
        owner_engine, cliente, email=f"est-{perfil}@example.com", perfil=perfil
    )
    r = await cliente.get(
        f"/ingestao/fontes/{fid}/estimativa",
        params={"inicio": "2026-08-01", "fim": "2026-08-31"},
        headers=headers,
    )
    assert r.status_code == 403


async def test_estimativa_de_fonte_inexistente_e_404(cliente, owner_engine) -> None:
    _, adm = await _admin(owner_engine, cliente)
    r = await cliente.get(
        f"/ingestao/fontes/{uuid.uuid4()}/estimativa",
        params={"inicio": "2026-08-01", "fim": "2026-08-31"},
        headers=adm,
    )
    assert r.status_code == 404


# --- rest_api --------------------------------------------------------------------------


async def test_rest_api_fatia_a_janela_e_grava_bronze(
    cliente, owner_engine, monkeypatch, lake
) -> None:
    corpo = json.dumps([{"data": "01/08/2026", "valor": "0.052531"}]).encode()
    chamadas: list[httpx.Request] = []

    def _handler(req: httpx.Request) -> httpx.Response:
        chamadas.append(req)
        return httpx.Response(200, content=corpo, headers={"Content-Type": "application/json"})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **k: original(*a, **{**k, "transport": httpx.MockTransport(_handler)}),
    )

    _, headers = await _admin(owner_engine, cliente)
    fid = await _criar_fonte(cliente, headers, contrato_rates())
    ctx, totais = await _rodar(fid, inicio=date(2016, 1, 1), fim=date(2026, 8, 15))

    # A 10.6-year window gives two slices (the contract caps a request at 10 years).
    assert len(ctx["fatias"]) == 2
    assert totais["novos"] == 2
    assert chamadas[0].url.params["dataInicial"] == "01/01/2016"
    assert chamadas[0].url.params["formato"] == "json"
    assert "series/11" in str(chamadas[0].url)
    assert len(list((lake / "bronze" / "rates_test").rglob("*.parquet"))) == 2


async def test_rest_api_html_de_erro_com_200_nao_vira_lote(
    cliente, owner_engine, monkeypatch, lake
) -> None:
    """The costliest failure mode: 200 + an error page would become an empty bronze that looks fine."""
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **k: original(
            *a,
            **{
                **k,
                "transport": httpx.MockTransport(
                    lambda r: httpx.Response(200, content=b"<html>error</html>")
                ),
            },
        ),
    )
    _, headers = await _admin(owner_engine, cliente)
    contrato = contrato_rates()
    contrato["limites"] = {"tentativas": 1}
    fid = await _criar_fonte(cliente, headers, contrato)

    _, totais = await _rodar(fid, inicio=date(2026, 1, 1), fim=date(2026, 8, 15))
    assert totais["novos"] == 0 and totais["quarentena"] == 1
    itens = (await cliente.get("/ingestao/quarentena", headers=headers)).json()["itens"]
    assert itens[0]["codigo"] == "resposta_nao_json"


# --- odata ---------------------------------------------------------------------------


async def test_odata_pagina_ate_a_pagina_curta(cliente, owner_engine, monkeypatch, lake) -> None:
    paginas: list[int] = []

    def _handler(req: httpx.Request) -> httpx.Response:
        skip = int(req.url.params.get("$skip", 0))
        paginas.append(skip)
        itens = [{"buyPrice": "5.1", "sellPrice": "5.2", "quoteTime": "2026-07-01"}]
        corpo = itens * (2 if skip == 0 else 1)  # the 2nd page is short -> stop
        return httpx.Response(200, json={"value": corpo})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **k: original(*a, **{**k, "transport": httpx.MockTransport(_handler)}),
    )

    _, headers = await _admin(owner_engine, cliente)
    contrato = {
        "classe": "odata",
        "dataset": "fx_test",
        "cadencia": "manual",
        "janela": {"tipo": "mensal", "inicio": "2026-07"},
        "acesso": {
            "url_padrao": "https://example.com/odata/v1/Quotes",
            "params": {"$format": "json"},
            "params_janela": {"@dataInicial": "'{inicio:%m-%d-%Y}'"},
            "top": 2,
            "max_paginas": 10,
            "campo_itens": "value",
        },
        "formato": {"tipo": "json", "json_path": "value"},
        "schema_esperado": ["buyPrice"],
    }
    fid = await _criar_fonte(cliente, headers, contrato)
    _, totais = await _rodar(fid, inicio=date(2026, 7, 1), fim=date(2026, 7, 31))

    assert paginas == [0, 2]  # stopped at the short page
    assert totais["novos"] == 1
    lotes = await repo.lotes_da_fonte(uuid.UUID(fid))
    assert lotes[0]["manifesto"]["linhas"] == 3  # 2 + 1
    assert lotes[0]["manifesto"]["paginas"] == 2


# --- Audit trail and secrets -------------------------------------------------------------------------


async def test_execucao_registra_na_trilha(cliente, owner_engine, app_engine, origem, lake) -> None:
    _, headers = await _admin(owner_engine, cliente)
    fid = await _criar_fonte(cliente, headers, contrato_registry())
    ctx, _ = await _rodar(fid, inicio=date(2026, 8, 1), fim=date(2026, 8, 31))

    async with app_engine.connect() as conn:
        r = (
            (
                await conn.execute(
                    text(
                        "SELECT ator_tipo, depois FROM auditoria "
                        "WHERE recurso_tipo = 'ingestao_fonte' AND acao = 'executar'"
                    )
                )
            )
            .mappings()
            .all()
        )
    assert len(r) == 1
    # Triggered by the scheduler/engine: 'sistema' actor (no ator_id), consistent with the DB CHECK.
    assert r[0]["ator_tipo"] == "sistema"
    assert r[0]["depois"]["janela"] == {"inicio": "2026-08-01", "fim": "2026-08-31"}
    assert r[0]["depois"]["execucao_id"] == ctx["execucao_id"]


async def test_credencial_do_lake_nunca_volta_pela_api(cliente, owner_engine, tmp_path) -> None:
    """The service account goes to the vault; neither the source list nor its detail returns it."""
    _, headers = await _admin(owner_engine, cliente)
    segredo = json.dumps({"type": "service_account", "private_key": "PEM-SECRETISSIMO"})
    r = await cliente.post(
        "/conexoes",
        json={
            "nome": "Lake GCS",
            "tipo": "objeto",
            "segredo": segredo,
            "config": {
                "provedor": "gcs",
                "buckets": {"landing": "l", "bronze": "b", "quarantine": "q"},
            },
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    conexao_id = r.json()["id"]

    fid = await _criar_fonte(cliente, headers, contrato_registry())
    await cliente.patch(
        f"/ingestao/fontes/{fid}", json={"destino_conexao_id": conexao_id}, headers=headers
    )

    for rota in ("/conexoes", "/ingestao/fontes", f"/ingestao/fontes/{fid}"):
        corpo = (await cliente.get(rota, headers=headers)).text
        assert "PEM-SECRETISSIMO" not in corpo and "private_key" not in corpo


async def test_trilha_da_conexao_do_lake_nao_tem_segredo(cliente, owner_engine, app_engine) -> None:
    _, headers = await _admin(owner_engine, cliente)
    await cliente.post(
        "/conexoes",
        json={
            "nome": "Lake GCS",
            "tipo": "objeto",
            "segredo": json.dumps({"private_key": "PEM-SECRETISSIMO"}),
            "config": {
                "provedor": "gcs",
                "buckets": {"landing": "l", "bronze": "b", "quarantine": "q"},
            },
        },
        headers=headers,
    )
    async with app_engine.connect() as conn:
        linhas = (await conn.execute(text("SELECT antes, depois FROM auditoria"))).all()
    assert "PEM-SECRETISSIMO" not in json.dumps(linhas, default=str)


# --- Queries ---------------------------------------------------------------------------------


async def test_manifesto_pela_api(cliente, owner_engine, origem, lake) -> None:
    _, headers = await _admin(owner_engine, cliente)
    fid = await _criar_fonte(cliente, headers, contrato_registry())
    await _rodar(fid, inicio=date(2026, 8, 1), fim=date(2026, 8, 31))

    r = await cliente.get(f"/ingestao/fontes/{fid}/manifesto/2026-08", headers=headers)
    assert r.status_code == 200
    assert r.json()["dataset"] == "registry_test" and r.json()["linhas"] == 1

    ausente = await cliente.get(f"/ingestao/fontes/{fid}/manifesto/1999-01", headers=headers)
    assert ausente.status_code == 404


async def test_excluir_fonte_nao_apaga_o_lake(cliente, owner_engine, origem, lake) -> None:
    """The ingested data does not belong to the registry. Deleting the source just forgets the index."""
    _, headers = await _admin(owner_engine, cliente)
    fid = await _criar_fonte(cliente, headers, contrato_registry())
    await _rodar(fid, inicio=date(2026, 8, 1), fim=date(2026, 8, 31))
    arquivos = list((lake / "landing").rglob("*.zip"))

    assert (await cliente.delete(f"/ingestao/fontes/{fid}", headers=headers)).status_code == 204
    assert (await cliente.get(f"/ingestao/fontes/{fid}", headers=headers)).status_code == 404
    assert all(p.is_file() for p in arquivos)


async def test_odata_sem_skip_faz_uma_requisicao_e_marca_truncado(
    cliente, owner_engine, monkeypatch, lake
) -> None:
    """`paginar: false`: a service that returns 500 for ANY `$skip`. One request; a full page
    becomes `truncado`, never silence."""
    vistas: list[httpx.Request] = []

    def _handler(req: httpx.Request) -> httpx.Response:
        vistas.append(req)
        if "$skip" in str(req.url):
            return httpx.Response(500, text="Erro desconhecido")
        return httpx.Response(200, json={"value": [{"Date": "2026-07-01", "Total": "1"}] * 2})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **k: original(*a, **{**k, "transport": httpx.MockTransport(_handler)}),
    )

    _, headers = await _admin(owner_engine, cliente)
    contrato = {
        "classe": "odata",
        "dataset": "payments_test",
        "cadencia": "manual",
        "janela": {"tipo": "mensal", "inicio": "2026-07"},
        "acesso": {
            "url_padrao": "https://example.com/odata/v1/Payments",
            "params": {"$format": "json"},
            "paginar": False,
            "top": 2,
            "campo_itens": "value",
        },
        "formato": {"tipo": "json", "json_path": "value"},
    }
    fid = await _criar_fonte(cliente, headers, contrato)
    _, totais = await _rodar(fid, inicio=date(2026, 7, 1), fim=date(2026, 7, 31))

    assert totais["novos"] == 1
    assert all("$skip" not in str(r.url) for r in vistas), "must not send $skip"
    lotes = await repo.lotes_da_fonte(uuid.UUID(fid))
    assert lotes[0]["manifesto"]["truncado"] is True
    assert lotes[0]["manifesto"]["linhas"] == 2


async def test_trocar_o_destino_regrava_no_lake_novo(
    cliente, owner_engine, origem, lake, tmp_path
) -> None:
    """Lake migration (disk -> bucket, or bucket -> bucket): "already ingested" holds PER DESTINATION.

    Without this, the migration ends "successfully" with the new destination empty, the worst
    outcome because it looks done."""
    from cflow_core.config import get_settings

    _, headers = await _admin(owner_engine, cliente)
    fid = await _criar_fonte(cliente, headers, contrato_registry())
    await _rodar(fid, inicio=date(2026, 8, 1), fim=date(2026, 8, 31))
    assert len(list((lake / "bronze").rglob("*.parquet"))) == 1

    # Same source, same bytes, ANOTHER lake.
    novo = tmp_path / "lake2"
    monkey = pytest.MonkeyPatch()
    monkey.setenv("CFLOW_LAKE_DIR", str(novo))
    get_settings.cache_clear()
    try:
        _, totais = await _rodar(fid, inicio=date(2026, 8, 1), fim=date(2026, 8, 31))
        assert totais["novos"] == 1, "the new destination must receive the batch"
        assert len(list((novo / "landing").rglob("*.zip"))) == 1
        assert len(list((novo / "bronze").rglob("*.parquet"))) == 1
        # The batch pointer now points at the new lake (otherwise replay breaks).
        lotes = await repo.lotes_da_fonte(uuid.UUID(fid))
        assert str(novo) in lotes[0]["landing_uri"]
    finally:
        monkey.undo()
        get_settings.cache_clear()

    # Back to the original destination: rewritten there, since it is the destination again.
    _, totais = await _rodar(fid, inicio=date(2026, 8, 1), fim=date(2026, 8, 31))
    assert totais["novos"] == 1  # rewrites into the original lake, which is the destination again


async def test_fatia_nao_publicada_nao_vira_quarentena(cliente, owner_engine, origem, lake) -> None:
    """404 is the origin saying "that day does not exist": weekend, holiday, future period.
    Treating it as a failure drowns the DLQ, and then nobody looks at the DLQ, which is where the
    real problem lives."""
    _, headers = await _admin(owner_engine, cliente)
    contrato = contrato_registry()
    contrato["janela"] = {"tipo": "mensal", "inicio": "2026-07"}
    fid = await _criar_fonte(cliente, headers, contrato)
    origem.status_forcado = 404

    _, totais = await _rodar(fid, inicio=date(2026, 7, 1), fim=date(2026, 8, 31))
    assert totais["quarentena"] == 0, "404 is not quarantine"
    assert totais["nao_publicados"] == 2
    assert totais["lotes"] == 0, "a nonexistent slice is not a batch"

    itens = (await cliente.get("/ingestao/quarentena", headers=headers)).json()["itens"]
    assert itens == []


async def test_erro_de_servidor_continua_indo_para_quarentena(
    cliente, owner_engine, origem, lake
) -> None:
    """The distinction must hold both ways: 5xx is a real failure and must show up."""
    _, headers = await _admin(owner_engine, cliente)
    contrato = contrato_registry()
    contrato["limites"] = {"tentativas": 1, "timeout_segundos": 5}
    fid = await _criar_fonte(cliente, headers, contrato)
    origem.status_forcado = 500

    _, totais = await _rodar(fid, inicio=date(2026, 8, 1), fim=date(2026, 8, 31))
    assert totais["quarentena"] == 1
    assert totais["nao_publicados"] == 0


# --- Listing consumed by the Studio's Bronze tab ---------------------------------------------
#
# The Bronze tab shows ingestion sources next to the pipelines classified as bronze. It has NO route
# of its own: each source's state (cadence, maintenance flag, time and outcome of the last run)
# comes from `GET /ingestao/fontes`, via subselects in `repositorio.listar_fontes`.
#
# These tests exist because those fields had none: they were returned and nobody read them. Now a
# screen depends on them, and an SQL refactor that dropped them would make the tab say "never run"
# for sources that run every month, silently, because the response would still be 200.


async def _fonte_na_lista(cliente, headers, dataset: str) -> dict:
    r = await cliente.get("/ingestao/fontes", headers=headers)
    assert r.status_code == 200, r.text
    achadas = [f for f in r.json()["fontes"] if f["dataset"] == dataset]
    assert len(achadas) == 1, f"expected 1 source with dataset {dataset}, found {len(achadas)}"
    return achadas[0]


async def test_listagem_traz_o_estado_que_a_aba_bronze_mostra(
    cliente, owner_engine, origem, lake
) -> None:
    """The fields of the Bronze tab row, and what each one answers on screen."""
    _, headers = await _admin(owner_engine, cliente)
    r = await cliente.post(
        "/ingestao/fontes",
        json={
            "nome": "Daily registry (test)",
            "contrato": contrato_registry(),
            "cadencia": "mensal",
            # `sistema` in ingestion means "maintained by the platform", NOT visibility: the
            # listing does not filter on it. If it did, the tab would hide the catalog sources from
            # the specialist, who is exactly who the `fonte lake` node serves.
            "sistema": True,
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text

    # 1) Before running: "never run" is a LEGITIMATE STATE, and the absence of the two fields is
    #    what the screen uses to say so (instead of inventing a date or painting an outcome pill).
    antes = await _fonte_na_lista(cliente, headers, "registry_test")
    assert antes["ultima_execucao_em"] is None
    assert antes["ultimo_status"] is None
    assert antes["cadencia"] == "mensal"
    assert antes["classe"] == "http_file"
    assert antes["sistema"] is True
    assert antes["ativo"] is True
    assert antes["watermark"] is None

    await _rodar(antes["id"], inicio=date(2026, 8, 1), fim=date(2026, 8, 31))

    # 2) After running: time + outcome + how far the incremental run got.
    depois = await _fonte_na_lista(cliente, headers, "registry_test")
    assert depois["ultimo_status"] == "concluida"
    assert depois["ultima_execucao_em"] is not None
    assert depois["watermark"] == "2026-08"
    # The timestamp must carry a TIMEZONE (the column is `timestamptz`): the offset lets the front
    # end convert it to the display timezone. Without it, `new Date(iso)` in the browser would read
    # the time as the viewer's LOCAL time, and the screen would show the wrong time looking right.
    assert re.search(r"(\+|-)\d{2}:\d{2}$|Z$", depois["ultima_execucao_em"]), depois[
        "ultima_execucao_em"
    ]


async def test_listagem_mostra_a_execucao_MAIS_RECENTE_e_a_falha(
    cliente, owner_engine, origem, lake
) -> None:
    """Two runs: the row speaks of the latest one, and a failure shows up as a failure.

    This is the field that makes someone open (or not) the list. Showing the outcome of the FIRST
    run would hide exactly the case where someone needs to act."""
    _, headers = await _admin(owner_engine, cliente)
    fid = await _criar_fonte(cliente, headers, contrato_registry())

    ctx1, _ = await _rodar(fid, inicio=date(2026, 8, 1), fim=date(2026, 8, 31))
    primeira = await _fonte_na_lista(cliente, headers, "registry_test")
    assert primeira["ultimo_status"] == "concluida"

    # Second run, ended in FAILURE through the same path as production (`finalizar_execucao`
    # with `erro`), not by a hand-written UPDATE in the test.
    ctx2 = await service.preparar(
        fonte_id=uuid.UUID(fid), origem="manual", inicio=None, fim=None, ator_id=None
    )
    assert ctx2["execucao_id"] != ctx1["execucao_id"]
    await repo.finalizar_execucao(ctx2["execucao_id"], contagens={}, erro="lake unavailable")

    linha = await _fonte_na_lista(cliente, headers, "registry_test")
    assert linha["ultimo_status"] == "falha"
    assert linha["ultima_execucao_em"] > primeira["ultima_execucao_em"]
    # The error message does NOT go in the listing: it may carry an internal path/lake URI, and
    # the list row is read by any read-capable role. The detail lives in
    # GET /ingestao/execucoes/{id}, which is where you go to investigate.
    assert "lake unavailable" not in json.dumps(linha, default=str)


async def test_especialista_dispara_a_ingestao_pela_rota(
    cliente, owner_engine, origem, lake, monkeypatch
) -> None:
    """The Bronze tab trigger, exactly as the front end does it: POST with no body.

    No body on purpose: `executarIngestao` in the front-end client sends
    `Content-Type: application/json` and an empty body. If the route's `ExecutarIn | None` rejected
    that, the button would return 422 in the browser and would pass any test that sent `json={}`."""
    from cflow_core.config import get_settings

    # No orchestrator: runs inline and returns the totals (the Temporal path has its own test in
    # test_ingestao_workflow).
    monkeypatch.setattr(get_settings(), "usar_temporal", False)

    _, adm = await _admin(owner_engine, cliente)
    contrato = contrato_registry()
    contrato["janela"] = {"tipo": "mensal", "inicio": "2026-08"}
    fid = await _criar_fonte(cliente, adm, contrato)

    _, esp = await usuario_logado(
        owner_engine, cliente, email="esp.ingestao@example.com", perfil="especialista"
    )
    r = await cliente.post(
        f"/ingestao/fontes/{fid}/executar",
        headers={**esp, "Content-Type": "application/json"},
    )
    assert r.status_code == 200, r.text
    corpo = r.json()
    assert corpo["status"] == "concluida"
    assert corpo["dataset"] == "registry_test"
    # The screen uses the slice count and the window in the trigger confirmation/return.
    assert corpo["janela"]["inicio"] and corpo["janela"]["fim"]
    assert len(corpo["fatias"]) >= 1

    # And the effect shows up in the SAME listing the tab reads, with no extra call per source.
    linha = await _fonte_na_lista(cliente, esp, "registry_test")
    assert linha["ultimo_status"] == "concluida"
    assert linha["ultima_execucao_em"] is not None


async def test_operador_nao_dispara_ingestao(cliente, owner_engine) -> None:
    """An operator neither sees nor triggers: the Portal is consumption-only (the `GET` was already
    denied; so is the trigger).

    The check is at the EDGE, before any effect; there is no created run to deny afterwards.
    """
    _, adm = await _admin(owner_engine, cliente)
    fid = await _criar_fonte(cliente, adm, contrato_registry())
    _, op = await usuario_logado(
        owner_engine, cliente, email="op.ingestao@example.com", perfil="operador"
    )
    r = await cliente.post(f"/ingestao/fontes/{fid}/executar", headers=op)
    assert r.status_code == 403
    assert r.json()["erro"]["codigo"] == "nao_autorizado"

    execs = (await cliente.get("/ingestao/execucoes", headers=adm)).json()["execucoes"]
    assert execs == [], "denying at the edge means NO effect, not even a recorded run"


async def test_disparo_em_fonte_inexistente_e_404(cliente, owner_engine) -> None:
    """Error in the standard envelope: this `mensagem` is what the block's red banner shows."""
    _, esp = await usuario_logado(
        owner_engine, cliente, email="esp.404@example.com", perfil="especialista"
    )
    r = await cliente.post(f"/ingestao/fontes/{uuid.uuid4()}/executar", headers=esp)
    assert r.status_code == 404
    erro = r.json()["erro"]
    assert erro["codigo"] == "nao_encontrado"
    assert erro["mensagem"] == "External source not found."
    assert erro["request_id"]


# --- One in-flight run per source ---------------------------------------------------


async def test_fonte_com_execucao_em_voo_recusa_e_orfa_nao_bloqueia(
    cliente, owner_engine, app_engine, lake, origem, monkeypatch
) -> None:
    """Scheduler + manual, backfill + incremental: the second run of the SAME source read the same
    watermark and downloaded the same slices (2x network against the public origin, 2x the
    contract's semaphore, and it could advance the watermark over the first run's quarantine).
    Refused with 409; and since ingestion has no reaper of its own, a run whose workflow died is
    closed right here and does not block the next one."""
    from cflow_core.execucao import reconciliacao as rec

    _, headers = await _admin(owner_engine, cliente, email="conc.ingestao@example.com")
    fid = await _criar_fonte(cliente, headers, contrato_registry())
    ctx = await service.preparar(
        fonte_id=uuid.UUID(fid), origem="manual", inicio=None, fim=None, ator_id=None
    )

    async def _conectar(_host):
        return object()

    monkeypatch.setattr(rec, "conectar", _conectar)

    async def _viva(_c, _e, prefixos=()):
        assert prefixos == ("ingestao-",)  # the ingestion workflow has its own prefix
        return rec.Veredito(viva=True, desconhecida=False)

    monkeypatch.setattr(rec, "consultar_uma", _viva)

    r = await cliente.post(f"/ingestao/fontes/{fid}/executar", headers=headers)
    assert r.status_code == 409, r.text
    assert r.json()["erro"]["codigo"] == "ingestao_em_andamento"
    assert r.json()["erro"]["detalhes"][0]["mensagem"] == str(ctx["execucao_id"])

    async def _morta(_c, _e, prefixos=()):
        return rec.Veredito(viva=False, desconhecida=True)

    monkeypatch.setattr(rec, "consultar_uma", _morta)
    r = await cliente.post(f"/ingestao/fontes/{fid}/executar", headers=headers)
    assert r.status_code in (200, 202), r.text
    async with app_engine.connect() as conn:
        st = (
            await conn.execute(
                text("SELECT status FROM ingestao_execucoes WHERE id = :e"),
                {"e": uuid.UUID(str(ctx["execucao_id"]))},
            )
        ).scalar_one()
    assert st == "falha"  # the orphan was closed, with the same verdict as the reaper

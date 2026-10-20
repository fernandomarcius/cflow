"""Ingestion as a PIPELINE: the `ingestao` node, per-slice log and retry.

What these tests prove:
  1. **run log**: one row per slice, including those that produce no batch (origin 404);
  2. **per-source history**: the same slice across several runs;
  3. **per-slice retry**: redoes only what ended badly, and refuses when there is nothing to redo;
  4. **dependency between sources**: has no test of its own here because it has no code of its own:
     it becomes a graph edge (`fonte` with `origem:"pipeline"`), covered by `pipelines/dag.py`.

And what sustains the three: the `ingestao` node really runs ingestion and returns the log as DATA.
"""

from __future__ import annotations

import io
import zipfile

import httpx
import pytest

from cflow_core.errors import ErroAplicacao
from cflow_core.pipelines.definicao import validar

from ._arnes_auth import usuario_logado
from .test_ingestao import CSV, contrato_registry

pytestmark = pytest.mark.integration


# --- Harness ----------------------------------------------------------------------------------


#: Fixed date in the zip header. `writestr` with a bare name records the CURRENT time (2 s format
#: resolution), so two calls of this harness in different seconds produce different bytes, and
#: ingestion idempotency is by payload `sha256`. That made
#: `test_historico_da_fatia_atravessa_execucoes` intermittently report `novo` where it expects
#: `reusado`: the test was right, the fixture was not deterministic.
_DATA_ZIP = (2026, 8, 1, 0, 0, 0)


def _zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            zipfile.ZipInfo("registry_202608.csv", date_time=_DATA_ZIP),
            CSV.encode("latin-1"),
        )
    return buf.getvalue()


def test_zip_do_arnes_e_byte_identico_entre_chamadas():
    """The harness must be deterministic for hash idempotency to be testable."""
    assert _zip() == _zip()


def _transporte(monkeypatch, responder) -> None:
    """Swap `httpx.AsyncClient` for one with a mocked transport: ingestion tests never touch the
    network."""
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **k: original(*a, **{**k, "transport": httpx.MockTransport(responder)}),
    )


def _arnes_inline(monkeypatch, tmp_path, responder) -> None:
    """Lake in `tmp` and INLINE execution.

    Inline on purpose in the log tests: with the orchestrator, `POST /executar` returns
    'executando' and the log only exists later, so the test would measure the wait, not the record.
    The orchestrated path (one activity per slice, with the SAME log function) is covered by
    `test_ingestao_workflow.py` and by the end-to-end pipeline test at the end of this file."""
    from cflow_core.config import get_settings

    monkeypatch.setenv("CFLOW_LAKE_DIR", str(tmp_path / "lake"))
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "usar_temporal", False)
    _transporte(monkeypatch, responder)


def _no(**cfg) -> dict:
    return {"nos": [{"id": "ing", "tipo": "ingestao", "config": cfg}], "arestas": []}


async def _fonte(cliente, headers, dataset: str, **over) -> str:
    r = await cliente.post(
        "/ingestao/fontes",
        json={"nome": f"Source {dataset}", "contrato": contrato_registry(dataset, **over)},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


# --- Structural validation of the node --------------------------------------------------------


def test_ingestao_sem_fonte_id_e_recusada() -> None:
    with pytest.raises(ErroAplicacao) as e:
        validar(_no())
    assert "fonte_id" in str(e.value.detalhes)


def test_contrato_da_fonte_nao_entra_no_no() -> None:
    """A URL on the node would create two truths about the same origin, and the graph's would win
    silently. Refusing beats ignoring: accepted config with no effect looks configured."""
    with pytest.raises(ErroAplicacao) as e:
        validar(_no(fonte_id="00000000-0000-0000-0000-000000000001", url="https://x/y.zip"))
    assert "belongs to the source contract" in str(e.value.detalhes)


def test_janela_do_no_so_aceita_data_iso() -> None:
    """`:param` is left out on purpose: the window decides HOW MANY requests go to the public
    origin, and "blank = everything" would become a ten-year backfill triggered by accident."""
    with pytest.raises(ErroAplicacao):
        validar(_no(fonte_id="00000000-0000-0000-0000-000000000001", inicio=":competencia"))


def test_janela_invertida_e_recusada() -> None:
    with pytest.raises(ErroAplicacao) as e:
        validar(
            _no(
                fonte_id="00000000-0000-0000-0000-000000000001",
                inicio="2026-08-10",
                fim="2026-08-01",
            )
        )
    assert "after the end date" in str(e.value.detalhes)


def test_ingestao_nao_aceita_entrada() -> None:
    """An edge arriving at the node looks like chaining and is not: the predecessor's data would be
    silently ignored. Chaining is done with a `fonte` of origin 'pipeline' in the next pipeline."""
    d = {
        "nos": [
            {"id": "a", "tipo": "sql", "sql": "SELECT 1 AS x"},
            {
                "id": "ing",
                "tipo": "ingestao",
                "config": {"fonte_id": "00000000-0000-0000-0000-000000000001"},
            },
        ],
        "arestas": [{"de": "a", "para": "ing"}],
    }
    with pytest.raises(ErroAplicacao) as e:
        validar(d)
    assert "does not accept input" in str(e.value.detalhes)


def test_no_valido_passa() -> None:
    validar(_no(fonte_id="00000000-0000-0000-0000-000000000001", inicio="2026-08-01"))


# --- Publishing: the link to the source is checked ---------------------------------------------


async def test_publicar_com_fonte_inexistente_e_recusado(cliente, owner_engine) -> None:
    """Without this check the error would only show up at run time, after scheduling, when nobody
    is looking."""
    _, headers = await usuario_logado(
        owner_engine, cliente, email="pub.ing@example.com", perfil="admin"
    )
    pid = (
        await cliente.post("/pipelines", json={"nome": "Orphan bronze"}, headers=headers)
    ).json()["id"]
    await cliente.put(
        f"/pipelines/{pid}/rascunho",
        json={
            "definicao": _no(fonte_id="00000000-0000-0000-0000-0000000000ff"),
            "base_numero": 0,
        },
        headers=headers,
    )
    r = await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=headers
    )
    assert r.status_code == 422, r.text
    assert "ingestion source not found" in r.text


# --- Per-slice log ------------------------------------------------------------------------------


async def test_log_registra_uma_linha_por_fatia_inclusive_sem_lote(
    cliente, owner_engine, monkeypatch, tmp_path
) -> None:
    """A slice the origin does NOT publish (404) yields no batch, and it was exactly that one that
    went missing.

    A 3-month window in which the origin only has 1 showed up as "1 batch", with nothing to tell
    "2 months not yet published" apart from "2 months lost"."""
    from cflow_core.config import get_settings

    def responder(req: httpx.Request) -> httpx.Response:
        # Only August exists at the origin; June and July answer 404 (= "not published", not a failure).
        if "202608" in str(req.url):
            return httpx.Response(200, content=_zip(), headers={"ETag": '"v1"'})
        return httpx.Response(404)

    _arnes_inline(monkeypatch, tmp_path, responder)
    _, headers = await usuario_logado(
        owner_engine, cliente, email="log.ing@example.com", perfil="admin"
    )
    fid = await _fonte(
        cliente, headers, "registry_log", janela={"tipo": "mensal", "inicio": "2026-06"}
    )

    r = await cliente.post(
        f"/ingestao/fontes/{fid}/executar",
        json={"inicio": "2026-06-01", "fim": "2026-08-31"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    execucao_id = r.json()["execucao_id"]

    log = (await cliente.get(f"/ingestao/execucoes/{execucao_id}/fatias", headers=headers)).json()[
        "fatias"
    ]
    por_chave = {f["chave_lote"]: f for f in log}
    assert set(por_chave) == {"2026-06", "2026-07", "2026-08"}, (
        "every slice of the window has a row"
    )
    assert por_chave["2026-08"]["desfecho"] == "novo"
    assert por_chave["2026-06"]["desfecho"] == "nao_publicado"
    assert por_chave["2026-07"]["desfecho"] == "nao_publicado"
    assert all(f["tentativa"] == 1 for f in log)
    assert all(f["duracao_ms"] is not None for f in log)

    # And the run SUMMARY now holds all six outcomes, not four.
    ex = (await cliente.get(f"/ingestao/execucoes/{execucao_id}", headers=headers)).json()
    assert ex["lotes_nao_publicados"] == 2, "the non-event is counted, outside quarantine"
    assert ex["lotes_novos"] == 1
    assert ex["lotes_quarentena"] == 0
    get_settings.cache_clear()


async def test_historico_da_fatia_atravessa_execucoes(
    cliente, owner_engine, monkeypatch, tmp_path
) -> None:
    """ "Since when has the August slice been like this?" A single run's log cannot answer."""
    from cflow_core.config import get_settings

    _arnes_inline(
        monkeypatch,
        tmp_path,
        lambda r: httpx.Response(200, content=_zip(), headers={"ETag": '"v1"'}),
    )
    _, headers = await usuario_logado(
        owner_engine, cliente, email="hist.ing@example.com", perfil="admin"
    )
    fid = await _fonte(cliente, headers, "registry_hist")
    for _ in range(2):
        r = await cliente.post(
            f"/ingestao/fontes/{fid}/executar",
            json={"inicio": "2026-08-01", "fim": "2026-08-31"},
            headers=headers,
        )
        assert r.status_code == 200, r.text

    h = (await cliente.get(f"/ingestao/fontes/{fid}/fatias/2026-08", headers=headers)).json()[
        "fatias"
    ]
    assert len(h) == 2, "two runs, two passes over the same slice"
    # The 2nd sees the same bytes: hash idempotency -> `reusado`, not a duplicate batch.
    #
    # Assertion on the SET first, then on the order. It used to be positional only, and when this
    # test failed once in CI the message `assert 'novo' in ('reusado','nao_modificado')` could not
    # tell "the order came swapped" (cosmetic) from "idempotency did not work" (serious).
    desfechos = {x["desfecho"] for x in h}
    assert desfechos & {"novo"} and desfechos & {"reusado", "nao_modificado"}, (
        f"hash idempotency did not hold: the 2nd pass over the same bytes should reuse, "
        f"and got {[x['desfecho'] for x in h]}"
    )
    assert h[0]["desfecho"] in ("reusado", "nao_modificado"), (
        f"order swapped: history runs from newest to oldest, and came as "
        f"{[x['desfecho'] for x in h]}"
    )
    assert h[1]["desfecho"] == "novo"
    get_settings.cache_clear()


# --- Per-slice retry ----------------------------------------------------------------------------


async def test_retentar_sem_falha_e_recusado(cliente, owner_engine, monkeypatch, tmp_path) -> None:
    """Explicit refusal instead of creating an empty run: "completed, 0 slices" would read as if
    the retry had worked."""
    from cflow_core.config import get_settings

    _arnes_inline(
        monkeypatch,
        tmp_path,
        lambda r: httpx.Response(200, content=_zip(), headers={"ETag": '"v1"'}),
    )
    _, headers = await usuario_logado(
        owner_engine, cliente, email="ret.ok@example.com", perfil="admin"
    )
    fid = await _fonte(cliente, headers, "registry_ret_ok")
    ex = (
        await cliente.post(
            f"/ingestao/fontes/{fid}/executar",
            json={"inicio": "2026-08-01", "fim": "2026-08-31"},
            headers=headers,
        )
    ).json()

    r = await cliente.post(f"/ingestao/execucoes/{ex['execucao_id']}/retentar", headers=headers)
    assert r.status_code == 409, r.text
    assert r.json()["erro"]["codigo"] == "nada_a_retentar"
    get_settings.cache_clear()


async def test_retentar_refaz_so_a_fatia_que_falhou(
    cliente, owner_engine, monkeypatch, tmp_path
) -> None:
    """Two slices, one quarantined. The retry creates a NEW run (the previous one is evidence) and
    runs 1 slice, not 2: that is what separates a retry from a backfill."""
    from cflow_core.config import get_settings

    quebrado = {"v": True}

    def responder(req: httpx.Request) -> httpx.Response:
        if "202607" in str(req.url) and quebrado["v"]:
            # Corrupt zip = DATA failure -> quarantine (the next slice carries on).
            return httpx.Response(200, content=b"not-a-zip", headers={"ETag": '"x"'})
        return httpx.Response(200, content=_zip(), headers={"ETag": '"v1"'})

    _arnes_inline(monkeypatch, tmp_path, responder)
    _, headers = await usuario_logado(
        owner_engine, cliente, email="ret.falha@example.com", perfil="admin"
    )
    fid = await _fonte(
        cliente, headers, "registry_ret_falha", janela={"tipo": "mensal", "inicio": "2026-07"}
    )
    ex = (
        await cliente.post(
            f"/ingestao/fontes/{fid}/executar",
            json={"inicio": "2026-07-01", "fim": "2026-08-31"},
            headers=headers,
        )
    ).json()
    log = (
        await cliente.get(f"/ingestao/execucoes/{ex['execucao_id']}/fatias", headers=headers)
    ).json()["fatias"]
    por_chave = {f["chave_lote"]: f for f in log}
    assert por_chave["2026-07"]["desfecho"] == "quarentena"
    assert por_chave["2026-07"][
        "motivo" if "motivo" in por_chave["2026-07"] else "quarentena_mensagem"
    ]
    assert por_chave["2026-08"]["desfecho"] == "novo"

    quebrado["v"] = False  # the origin republished the good file
    r = await cliente.post(f"/ingestao/execucoes/{ex['execucao_id']}/retentar", headers=headers)
    assert r.status_code == 200, r.text
    nova = r.json()
    assert nova["execucao_id"] != ex["execucao_id"], "new run; the previous one is evidence"
    assert len(nova["fatias"]) == 1, "only the slice that failed: a retry is not a backfill"
    assert nova["fatias"][0]["chave"] == "2026-07"

    log2 = (
        await cliente.get(f"/ingestao/execucoes/{nova['execucao_id']}/fatias", headers=headers)
    ).json()["fatias"]
    assert [f["chave_lote"] for f in log2] == ["2026-07"]
    assert log2[0]["desfecho"] == "novo", "the slice was recovered"

    # The PREVIOUS run still records the failure: history is not rewritten.
    antiga = (
        await cliente.get(f"/ingestao/execucoes/{ex['execucao_id']}/fatias", headers=headers)
    ).json()["fatias"]
    assert {f["chave_lote"]: f["desfecho"] for f in antiga}["2026-07"] == "quarentena"
    get_settings.cache_clear()


# --- RBAC at the edge ---------------------------------------------------------------------------


async def test_operador_nao_ve_log_nem_retenta(cliente, owner_engine) -> None:
    _, headers = await usuario_logado(
        owner_engine, cliente, email="op.ing@example.com", perfil="operador"
    )
    vazio = "00000000-0000-0000-0000-000000000001"
    assert (
        await cliente.get(f"/ingestao/execucoes/{vazio}/fatias", headers=headers)
    ).status_code == 403
    assert (
        await cliente.post(f"/ingestao/execucoes/{vazio}/retentar", headers=headers)
    ).status_code == 403


async def test_auditor_le_o_log_mas_nao_retenta(cliente, owner_engine) -> None:
    """The auditor reads everything and triggers nothing: the same line that separates `Leitura`
    from `EspecialistaOuAdmin` in the rest of ingestion."""
    _, headers = await usuario_logado(
        owner_engine, cliente, email="aud.ing@example.com", perfil="auditor"
    )
    vazio = "00000000-0000-0000-0000-000000000001"
    # 404 (not 403) proves it passed authorization and did not find the resource.
    assert (
        await cliente.get(f"/ingestao/execucoes/{vazio}/fatias", headers=headers)
    ).status_code == 404
    assert (
        await cliente.post(f"/ingestao/execucoes/{vazio}/retentar", headers=headers)
    ).status_code == 403


# --- End to end: ingestion running as a pipeline node -------------------------------------------


async def test_pipeline_com_no_de_ingestao_produz_o_log_como_dado(
    cliente, owner_engine, monkeypatch, tmp_path
) -> None:
    """The whole design in one test: a one-node pipeline really ingests, and the node SNAPSHOT is
    the per-slice log, which is what the run screen already knows how to display, with no new
    screen."""
    from cflow_core.config import get_settings

    from .conftest import aguardar_execucao

    monkeypatch.setenv("CFLOW_LAKE_DIR", str(tmp_path / "lake"))
    get_settings.cache_clear()
    _transporte(
        monkeypatch, lambda r: httpx.Response(200, content=_zip(), headers={"ETag": '"v1"'})
    )
    _, headers = await usuario_logado(
        owner_engine, cliente, email="pipe.ing@example.com", perfil="admin"
    )
    fid = await _fonte(cliente, headers, "registry_pipe")

    definicao = _no(fonte_id=fid, inicio="2026-08-01", fim="2026-08-31")
    pid = (
        await cliente.post("/pipelines", json={"nome": "bronze · registry_pipe"}, headers=headers)
    ).json()["id"]
    await cliente.put(
        f"/pipelines/{pid}/rascunho",
        json={"definicao": definicao, "base_numero": 0},
        headers=headers,
    )
    pub = await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=headers
    )
    assert pub.status_code == 201, pub.text

    ex = await cliente.post(f"/pipelines/{pid}/executar", json={}, headers=headers)
    assert ex.status_code == 200, ex.text
    detalhe = await aguardar_execucao(cliente, ex.json()["id"], headers)
    assert detalhe["status"] == "concluida", detalhe.get("erro")
    no = {n["no_id"]: n for n in detalhe["nos"]}["ing"]
    assert no["linhas"] == 1, "one row per slice of the window: the log IS the node data"

    # And the data really reached bronze: the ingestion index has the batch, not just the log.
    lotes = (await cliente.get(f"/ingestao/fontes/{fid}/lotes", headers=headers)).json()["lotes"]
    assert [x["chave_lote"] for x in lotes] == ["2026-08"]
    assert lotes[0]["status"] == "bronze"
    get_settings.cache_clear()

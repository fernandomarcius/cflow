"""A `origem: "publicado"` source INSIDE a chain reads the publication of ITS OWN run.

The implicit order of `dag.py` makes the reader wait for the producer of the same chain - but it
used to keep reading `_atual.json`, which is the CURRENT pointer of the scope. The pointer is
partitioned only by unit: another chain of the same unit, with another as-of (different parameters,
hence no 409), moves that pointer between this chain publication and the read, and the reader
computes over the other run data - and completes green. What is pinned:

- with the producer IN the chain, the source reads the IMMUTABLE index/manifest of its run (the same
  per-`execucao_id` resolution as `ingestao/publicados.py`), with the partition values of the source
  itself - never the pointer;
- if that run did not publish (root `montante_publica: false`), it is a NAMED error
  (`publicado_da_cadeia_ausente`) - falling back to the pointer would read another run;
- with no producer in the chain (or outside a chain), the behaviour is today's: the pointer.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from ._arnes_auth import usuario_logado
from .conftest import aguardar_execucao, contrato_ambiente_gold

pytestmark = pytest.mark.integration

OP = "61"
NO_LEITOR = "src_reader"


@pytest.fixture
def lake(tmp_path, monkeypatch):
    """Test lake under `tmp`. The settings are cached: without clearing, `CFLOW_LAKE_DIR` has no effect."""
    from cflow_core.config import get_settings

    monkeypatch.setenv("CFLOW_LAKE_DIR", str(tmp_path / "lake"))
    get_settings.cache_clear()
    yield tmp_path / "lake"
    get_settings.cache_clear()


def _modo(modo: str, monkeypatch) -> None:
    if modo == "inline":
        from cflow_core.config import get_settings

        monkeypatch.setattr(get_settings(), "usar_temporal", False)


async def _publicar(cliente, h, nome: str, definicao: dict) -> str:
    definicao = contrato_ambiente_gold(definicao, declarar=True)
    pid = (await cliente.post("/pipelines", json={"nome": nome}, headers=h)).json()["id"]
    r = await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    assert r.status_code in (200, 201), r.text
    r = await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    assert r.status_code in (200, 201), r.text
    return pid


_PARAM_OP = {"nome": "operacao_id", "tipo": "texto", "obrigatorio": True}
_PARAM_LOTE = {"nome": "lote", "tipo": "texto", "obrigatorio": True}


def _produtor(dataset: str) -> dict:
    """The load template: publishes to gold PARTITIONED by unit. The `lote` plays the role of the
    as-of - batch A gives 1 row, batch B gives 3 -, and it is the row count that says which
    publication was read."""
    return {
        "parametros": [_PARAM_OP, _PARAM_LOTE],
        "nos": [
            {
                "id": "dado",
                "tipo": "sql",
                "sql": "SELECT n FROM (VALUES (1),(2),(3)) t(n) "
                "WHERE CAST(:lote AS VARCHAR) = 'B' OR n = 1",
            },
            {
                "id": "saida",
                "tipo": "saida",
                "config": {
                    "destino": "lake",
                    "camada": "gold",
                    "dataset": dataset,
                    "particao": [{"origem": "parametro", "nome": "operacao_id"}],
                },
            },
        ],
        "arestas": [{"de": "dado", "para": "saida"}],
    }


def _leitor(*datasets: str) -> dict:
    """The reference: reads each dataset through `publicado`, in the scope of ITS unit."""
    nos = [
        {
            "id": NO_LEITOR if i == 0 else f"{NO_LEITOR}{i}",
            "tipo": "fonte",
            "config": {
                "origem": "publicado",
                "dataset": ds,
                "particao": {"operacao_id": ":operacao_id"},
            },
        }
        for i, ds in enumerate(datasets)
    ]
    ids = [n["id"] for n in nos]
    nos.append(
        {
            "id": "cont",
            "tipo": "sql",
            "sql": " UNION ALL ".join(f"SELECT count(*) AS c FROM {i}" for i in ids),
        }
    )
    return {
        "parametros": [_PARAM_OP],
        "nos": nos,
        "arestas": [{"de": i, "para": "cont"} for i in ids],
    }


def _raiz(w: str, r: str, **marca) -> dict:
    return {
        **marca,
        "parametros": [_PARAM_OP, _PARAM_LOTE],
        "nos": [
            {
                "id": "f0",
                "tipo": "fonte",
                "config": {"origem": "pipeline", "pipeline_id": w, "no_id": "saida"},
            },
            {
                "id": "f1",
                "tipo": "fonte",
                "config": {"origem": "pipeline", "pipeline_id": r, "no_id": "cont"},
            },
            {
                "id": "junta",
                "tipo": "sql",
                "sql": "SELECT count(*) AS c FROM f0 UNION ALL SELECT count(*) FROM f1",
            },
        ],
        "arestas": [{"de": "f0", "para": "junta"}, {"de": "f1", "para": "junta"}],
    }


async def _detalhe(cliente, h, exec_id: str, modo: str) -> dict:
    if modo == "temporal":
        return await aguardar_execucao(cliente, exec_id, h)
    return (await cliente.get(f"/execucoes/{exec_id}", headers=h)).json()


def _linhas(det: dict) -> dict[str, int]:
    return {n["no_id"]: n["linhas"] for n in det["nos"]}


def _ponteiro(lake, dataset: str) -> dict:
    return json.loads(
        (lake / "gold" / "dev" / dataset / f"operacao_id={OP}" / "_atual.json").read_text()
    )


@pytest.mark.parametrize("modo", ["inline", "temporal"])
async def test_cadeia_concorrente_nao_troca_o_as_of_da_leitura(
    owner_engine, cliente, lake, monkeypatch, modo
):
    """Two chains of the SAME unit with different as-ofs. The other one (batch B) publishes between
    the publication of this chain load (batch A) and the read by the reference - the interleaving is
    forced at the start of the reference node, which is the same point inline and in the worker. The
    reference must read batch A."""
    _modo(modo, monkeypatch)
    uid, h = await usuario_logado(
        owner_engine, cliente, email=f"pdc1{modo}@example.com", perfil="especialista"
    )
    ds = f"load_pdc1_{modo}"
    w = await _publicar(cliente, h, "Load-pdc1", _produtor(ds))
    r = await _publicar(cliente, h, "Ref-pdc1", _leitor(ds))
    raiz = await _publicar(cliente, h, "root-pdc1", _raiz(w, r))

    from cflow_core.execucao import engine

    original = engine._marcar_no_iniciado
    outra: dict = {}

    async def _intercalado(execucao_id, no_id, **kw):
        # The OTHER run (same unit, batch B) finishes here: it moves the scope `_atual.json` after
        # this chain load published and before the reference reads.
        if no_id == NO_LEITOR and not outra:
            outra["res"] = await engine.executar(
                pipeline_id=uuid.UUID(w),
                ator_id=uuid.UUID(str(uid)),
                parametros={"ambiente": "dev", "operacao_id": OP, "lote": "B"},
            )
        return await original(execucao_id, no_id, **kw)

    monkeypatch.setattr(engine, "_marcar_no_iniciado", _intercalado)

    resp = await cliente.post(
        f"/pipelines/{raiz}/executar-encadeado",
        json={"parametros": {"ambiente": "dev", "operacao_id": OP, "lote": "A"}},
        headers=h,
    )
    assert resp.status_code == 200, resp.text
    corpo = resp.json()
    det_raiz = await _detalhe(cliente, h, corpo["id"], modo)
    exec_w = next(c["execucao_id"] for c in corpo["cadeia"] if c["pipeline_id"] == w)
    exec_r = next(c["execucao_id"] for c in corpo["cadeia"] if c["pipeline_id"] == r)
    det_w = await _detalhe(cliente, h, exec_w, modo)
    det_r = await _detalhe(cliente, h, exec_r, modo)

    # The interleaving really happened: the scope pointer belongs to the OTHER run.
    assert outra["res"]["status"] == "concluida"
    assert _ponteiro(lake, ds)["execucao_id"] == outra["res"]["id"] != exec_w
    assert det_raiz["status"] == "concluida", det_raiz
    assert _linhas(det_w)["saida"] == 1
    # The finding: the reference used to read 3 rows (batch B, through the pointer). It must read the 1 row of THIS chain.
    assert _linhas(det_r)[NO_LEITOR] == 1


async def test_sem_produtor_na_cadeia_le_o_ponteiro_como_hoje(owner_engine, cliente, lake):
    """Degeneration: the producer of the second dataset is NOT in the closure. Reading the pointer is
    what `publicado` always did (and why it exists: reading without rerunning whoever published)."""
    from cflow_core.config import get_settings

    get_settings().usar_temporal = False
    try:
        uid, h = await usuario_logado(
            owner_engine, cliente, email="pdc2@example.com", perfil="especialista"
        )
        w = await _publicar(cliente, h, "Load-pdc2", _produtor("load_pdc2"))
        w_fora = await _publicar(cliente, h, "LoadOut-pdc2", _produtor("load_pdc2_other"))
        r = await _publicar(cliente, h, "Ref-pdc2", _leitor("load_pdc2", "load_pdc2_other"))
        raiz = await _publicar(cliente, h, "root-pdc2", _raiz(w, r))

        from cflow_core.execucao import engine

        anterior = await engine.executar(
            pipeline_id=uuid.UUID(w_fora),
            ator_id=uuid.UUID(str(uid)),
            parametros={"ambiente": "dev", "operacao_id": OP, "lote": "A"},
        )
        assert anterior["status"] == "concluida"

        resp = await cliente.post(
            f"/pipelines/{raiz}/executar-encadeado",
            json={"parametros": {"ambiente": "dev", "operacao_id": OP, "lote": "B"}},
            headers=h,
        )
        assert resp.status_code == 200, resp.text
        corpo = resp.json()
        exec_r = next(c["execucao_id"] for c in corpo["cadeia"] if c["pipeline_id"] == r)
        nos_r = _linhas((await cliente.get(f"/execucoes/{exec_r}", headers=h)).json())
        assert nos_r[NO_LEITOR] == 3  # producer in the chain: the publication of this run (batch B)
        assert nos_r[f"{NO_LEITOR}1"] == 1  # no producer in the chain: the pointer (batch A)
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize(
    ("modo", "marca"),
    [("inline", {}), ("temporal", {}), ("inline", {"montante_publica": False})],
)
async def test_outro_escopo_do_dataset_da_cadeia_continua_vindo_do_ponteiro(
    owner_engine, cliente, lake, monkeypatch, modo, marca
):
    """The cover template: a reference IN THE CHAIN publishes the summary of competence M, and the
    cover reads the same dataset at competence M-1 - the FROZEN history, which by definition belongs
    to another run. This chain does not produce that scope: it is still the pointer, even when the
    upstream does not publish. Sending that read to this run publication would make every closing
    fail over a scope it would never produce."""
    _modo(modo, monkeypatch)
    uid, h = await usuario_logado(
        owner_engine,
        cliente,
        email=f"pdc4{modo}{len(marca)}@example.com",
        perfil="especialista",
    )
    ds = f"summary_pdc4_{modo}_{len(marca)}"
    param_comp = {"nome": "comp", "tipo": "texto", "obrigatorio": True}
    produtor = _produtor(ds)
    produtor["parametros"].append(param_comp)
    produtor["nos"][1]["config"]["particao"].append({"origem": "parametro", "nome": "comp"})
    leitor = _leitor(ds)
    leitor["parametros"].append({"nome": "comp_m1", "tipo": "texto", "obrigatorio": True})
    leitor["nos"][0]["config"]["particao"]["comp"] = ":comp_m1"
    raiz_def = _raiz("", "", **marca)
    raiz_def["parametros"] += [
        param_comp,
        {"nome": "comp_m1", "tipo": "texto", "obrigatorio": True},
    ]
    w = await _publicar(cliente, h, "Load-pdc4", produtor)
    r = await _publicar(cliente, h, "Ref-pdc4", leitor)
    raiz_def["nos"][0]["config"]["pipeline_id"] = w
    raiz_def["nos"][1]["config"]["pipeline_id"] = r
    raiz = await _publicar(cliente, h, "root-pdc4", raiz_def)

    from cflow_core.execucao import engine

    # The previous month, FROZEN by a past run (batch B: 3 rows).
    passado = await engine.executar(
        pipeline_id=uuid.UUID(w),
        ator_id=uuid.UUID(str(uid)),
        parametros={
            "ambiente": "dev",
            "operacao_id": OP,
            "lote": "B",
            "comp": "2026-07-01",
        },
    )
    assert passado["status"] == "concluida"

    resp = await cliente.post(
        f"/pipelines/{raiz}/executar-encadeado",
        json={
            "parametros": {
                "ambiente": "dev",
                "operacao_id": OP,
                "lote": "A",
                "comp": "2026-08-01",
                "comp_m1": "2026-07-01",
            }
        },
        headers=h,
    )
    assert resp.status_code == 200, resp.text
    corpo = resp.json()
    det_raiz = await _detalhe(cliente, h, corpo["id"], modo)
    exec_r = next(c["execucao_id"] for c in corpo["cadeia"] if c["pipeline_id"] == r)
    det_r = await _detalhe(cliente, h, exec_r, modo)

    assert det_raiz["status"] == "concluida", det_raiz
    assert _linhas(det_r)[NO_LEITOR] == 3  # the frozen July data, through the pointer of that scope


def _produtor_por_coluna(dataset: str, *, permitir_vazio: bool = False) -> dict:
    """The REAL template of a summary dataset: `operacao_id` by parameter and the competence by COLUMN
    (the value is in the data). Batch A gives 1 row, B gives 3, Z gives zero."""
    produtor = _produtor(dataset)
    produtor["parametros"].append({"nome": "comp", "tipo": "texto", "obrigatorio": True})
    produtor["nos"][0]["sql"] = (
        "SELECT n, CAST(:comp AS VARCHAR) AS comp FROM (VALUES (1),(2),(3)) t(n) "
        "WHERE CAST(:lote AS VARCHAR) <> 'Z' AND (CAST(:lote AS VARCHAR) = 'B' OR n = 1)"
    )
    saida = produtor["nos"][1]["config"]
    saida["particao"].append({"origem": "coluna", "nome": "comp"})
    if permitir_vazio:
        saida["permitir_vazio"] = True
    return produtor


def _leitor_por_coluna(dataset: str) -> dict:
    """The cover: reads the PREVIOUS month (`comp_m1`, the history) and the run own (`comp`)."""
    leitor = _leitor(dataset, dataset)
    leitor["parametros"] += [
        {"nome": "comp", "tipo": "texto", "obrigatorio": True},
        {"nome": "comp_m1", "tipo": "texto", "obrigatorio": True},
    ]
    leitor["nos"][0]["config"]["particao"]["comp"] = ":comp_m1"
    leitor["nos"][1]["config"]["particao"]["comp"] = ":comp"
    return leitor


def _raiz_por_coluna(w: str, r: str) -> dict:
    raiz = _raiz(w, r)
    raiz["parametros"] += [
        {"nome": "comp", "tipo": "texto", "obrigatorio": True},
        {"nome": "comp_m1", "tipo": "texto", "obrigatorio": True},
    ]
    return raiz


@pytest.mark.parametrize("modo", ["inline", "temporal"])
async def test_historico_por_coluna_vem_do_ponteiro_e_o_escopo_da_corrida_da_execucao(
    owner_engine, cliente, lake, monkeypatch, modo
):
    """Regression: a reference in the chain publishes a summary dataset with the competence by
    COLUMN, and the cover asks for m-1 (2026-07). Only `operacao_id` is a parameter partition - it
    matched -, the read went to THIS run publication (only 2026-08) and the closing failed. The run
    published other scopes and not that one: it is the history, through the pointer. The scope it
    published still comes from it."""
    _modo(modo, monkeypatch)
    uid, h = await usuario_logado(
        owner_engine, cliente, email=f"pdc5{modo}@example.com", perfil="especialista"
    )
    ds = f"summary_pdc5_{modo}"
    w = await _publicar(cliente, h, "Load-pdc5", _produtor_por_coluna(ds))
    r = await _publicar(cliente, h, "Ref-pdc5", _leitor_por_coluna(ds))
    raiz = await _publicar(cliente, h, "root-pdc5", _raiz_por_coluna(w, r))

    from cflow_core.execucao import engine

    passado = await engine.executar(
        pipeline_id=uuid.UUID(w),
        ator_id=uuid.UUID(str(uid)),
        parametros={
            "ambiente": "dev",
            "operacao_id": OP,
            "lote": "B",
            "comp": "2026-07-01",
        },
    )
    assert passado["status"] == "concluida"

    resp = await cliente.post(
        f"/pipelines/{raiz}/executar-encadeado",
        json={
            "parametros": {
                "ambiente": "dev",
                "operacao_id": OP,
                "lote": "A",
                "comp": "2026-08-01",
                "comp_m1": "2026-07-01",
            }
        },
        headers=h,
    )
    assert resp.status_code == 200, resp.text
    corpo = resp.json()
    det_raiz = await _detalhe(cliente, h, corpo["id"], modo)
    exec_r = next(c["execucao_id"] for c in corpo["cadeia"] if c["pipeline_id"] == r)
    det_r = await _detalhe(cliente, h, exec_r, modo)

    assert det_raiz["status"] == "concluida", det_raiz
    linhas = _linhas(det_r)
    assert linhas[NO_LEITOR] == 3  # July, frozen, through the pointer
    assert linhas[f"{NO_LEITOR}1"] == 1  # August, from the publication of this run (batch A)


@pytest.mark.parametrize("modo", ["inline", "temporal"])
async def test_produtor_da_cadeia_sem_escopo_publicado_por_coluna_e_erro_nomeado(
    owner_engine, cliente, lake, monkeypatch, modo
):
    """With NO scope published at all (zero rows with `permitir_vazio`: the index comes out without
    partitions), there is no way to say the requested value belongs to another run - it may be
    exactly this one, empty. It stays a named error: falling back to the pointer would read the
    previous run of the same scope."""
    _modo(modo, monkeypatch)
    uid, h = await usuario_logado(
        owner_engine, cliente, email=f"pdc6{modo}@example.com", perfil="especialista"
    )
    ds = f"summary_pdc6_{modo}"
    w = await _publicar(cliente, h, "Load-pdc6", _produtor_por_coluna(ds, permitir_vazio=True))
    r = await _publicar(cliente, h, "Ref-pdc6", _leitor_por_coluna(ds))
    raiz = await _publicar(cliente, h, "root-pdc6", _raiz_por_coluna(w, r))

    from cflow_core.execucao import engine

    for comp in ("2026-07-01", "2026-08-01"):  # both scopes already exist in the pointer
        anterior = await engine.executar(
            pipeline_id=uuid.UUID(w),
            ator_id=uuid.UUID(str(uid)),
            parametros={"ambiente": "dev", "operacao_id": OP, "lote": "B", "comp": comp},
        )
        assert anterior["status"] == "concluida"

    resp = await cliente.post(
        f"/pipelines/{raiz}/executar-encadeado",
        json={
            "parametros": {
                "ambiente": "dev",
                "operacao_id": OP,
                "lote": "Z",
                "comp": "2026-08-01",
                "comp_m1": "2026-07-01",
            }
        },
        headers=h,
    )
    if modo == "temporal":
        assert resp.status_code == 200, resp.text
        det = await aguardar_execucao(cliente, resp.json()["id"], h)
        assert det["status"] == "falha", det
    else:
        assert resp.status_code == 400, resp.text
    nos_r = {n["no_id"]: n for n in await _nos_do_pipeline(owner_engine, r)}
    falhos = [n for n in nos_r.values() if n["status"] == "falha"]
    assert falhos and all(ds in (n["erro"] or "") for n in falhos), nos_r
    assert "cont" not in nos_r


async def _nos_do_pipeline(owner_engine, pipeline_id: str) -> list[dict]:
    async with owner_engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT n.execucao_id::text AS execucao_id, n.no_id, n.status, n.erro "
                "FROM execucao_nos n JOIN execucoes e ON e.id = n.execucao_id "
                "WHERE e.pipeline_id = CAST(:p AS uuid)"
            ),
            {"p": pipeline_id},
        )
        return [dict(x) for x in rows.mappings().all()]


@pytest.mark.parametrize("modo", ["inline", "temporal"])
async def test_produtor_da_cadeia_que_nao_publicou_e_erro_nomeado(
    owner_engine, cliente, lake, monkeypatch, modo
):
    """Root `montante_publica: false`: the load in the chain computes and does NOT publish. The
    reference in the chain cannot read the pointer (the publication of another run, which exists) -
    it fails naming what is missing."""
    _modo(modo, monkeypatch)
    uid, h = await usuario_logado(
        owner_engine, cliente, email=f"pdc3{modo}@example.com", perfil="especialista"
    )
    ds = f"load_pdc3_{modo}"
    w = await _publicar(cliente, h, "Load-pdc3", _produtor(ds))
    r = await _publicar(cliente, h, "Ref-pdc3", _leitor(ds))
    raiz = await _publicar(cliente, h, "root-pdc3", _raiz(w, r, montante_publica=False))

    from cflow_core.execucao import engine

    # A publication of ANOTHER run is already in the pointer - it is where the old read would land.
    anterior = await engine.executar(
        pipeline_id=uuid.UUID(w),
        ator_id=uuid.UUID(str(uid)),
        parametros={"ambiente": "dev", "operacao_id": OP, "lote": "B"},
    )
    assert anterior["status"] == "concluida"

    resp = await cliente.post(
        f"/pipelines/{raiz}/executar-encadeado",
        json={"parametros": {"ambiente": "dev", "operacao_id": OP, "lote": "A"}},
        headers=h,
    )
    if modo == "temporal":
        assert resp.status_code == 200, resp.text
        det = await aguardar_execucao(cliente, resp.json()["id"], h)
        assert det["status"] == "falha", det
    else:
        assert resp.status_code == 400, resp.text

    [exec_w] = {n["execucao_id"] for n in await _nos_do_pipeline(owner_engine, w)} - {
        anterior["id"]
    }
    nos_r = {n["no_id"]: n for n in await _nos_do_pipeline(owner_engine, r)}
    assert nos_r[NO_LEITOR]["status"] == "falha"
    erro = nos_r[NO_LEITOR]["erro"]
    assert ds in erro and exec_w in erro, erro
    assert "cont" not in nos_r  # nothing computed over the read that did not happen
    # The pointer is still the previous run: nobody published, and nobody read it.
    assert _ponteiro(lake, ds)["execucao_id"] == anterior["id"]

    # The code is stable and named (the node trail keeps the message; the code is the helper one).
    from cflow_core.errors import ErroAplicacao
    from cflow_core.ingestao import leitura

    with pytest.raises(ErroAplicacao) as exc:
        await leitura.resolver_publicado_da_execucao(
            {"dataset": ds, "camada": "gold", "particao": {"operacao_id": ":operacao_id"}},
            execucao_id=exec_w,
            parametros={"ambiente": "dev", "operacao_id": OP},
        )
    assert exc.value.codigo == "publicado_da_cadeia_ausente"


async def test_reuso_na_cadeia_nao_abre_buraco_na_linhagem(
    owner_engine, cliente, lake, monkeypatch
):
    """Two nodes reading the SAME publication: the second goes out through chain REUSE (hardlink,
    without re-reading the parquet). The `leitura_lake` trail must exist for BOTH, with the same
    content.

    It is the condition that allows reuse to exist. Reading faster and leaving a node without lineage
    would trade performance for auditability - and the trail is append-only: what is missing there
    cannot be added later."""
    _modo("inline", monkeypatch)
    _, h = await usuario_logado(
        owner_engine, cliente, email="pdc-lineage@example.com", perfil="especialista"
    )
    ds = "load_pdc_lineage"
    w = await _publicar(cliente, h, "Load-lineage", _produtor(ds))
    r = await _publicar(cliente, h, "Ref-lineage", _leitor(ds, ds))  # the SAME dataset twice
    raiz = await _publicar(cliente, h, "root-lineage", _raiz(w, r))

    resp = await cliente.post(
        f"/pipelines/{raiz}/executar-encadeado",
        json={"parametros": {"ambiente": "dev", "operacao_id": OP, "lote": "A"}},
        headers=h,
    )
    assert resp.status_code == 200, resp.text
    corpo = resp.json()
    exec_r = next(c["execucao_id"] for c in corpo["cadeia"] if c["pipeline_id"] == r)
    det_r = await _detalhe(cliente, h, exec_r, "inline")
    assert det_r["status"] == "concluida", det_r

    leitores = {n["no_id"]: n for n in det_r["nos"] if n["no_id"].startswith(NO_LEITOR)}
    assert len(leitores) == 2
    assert all(n["linhas"] == 1 for n in leitores.values()), leitores
    assert sum(1 for n in leitores.values() if n.get("reaproveitado")) == 1, (
        f"one of the two must go out through chain reuse: {leitores}"
    )

    async with owner_engine.connect() as conn:
        eventos = (
            (
                await conn.execute(
                    text(
                        "SELECT depois FROM auditoria WHERE recurso_tipo = 'leitura_lake' "
                        "AND recurso_id = CAST(:e AS uuid)"
                    ),
                    {"e": str(exec_r)},
                )
            )
            .scalars()
            .all()
        )
    assert len(eventos) == 2, f"BOTH nodes record the read, including the reused one: {eventos}"
    primeiro = eventos[0] if isinstance(eventos[0], dict) else json.loads(eventos[0])
    segundo = eventos[1] if isinstance(eventos[1], dict) else json.loads(eventos[1])
    assert primeiro == segundo, "same publication read, same lineage"
    assert primeiro["dataset"] == ds and primeiro["execucao_id_origem"]

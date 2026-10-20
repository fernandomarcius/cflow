"""OBSOLETE stamp on the scope pointer through the API - `/publicados/{camada}/{dataset}/escopo`.

The pointer of the PREVIOUS layout of a dataset that changed partitioning stays readable and frozen
on the migration day. The stamp makes a `publicado` read refuse it. It is a generic admin route,
audited in the SAME transaction.

The arrangement: a pipeline publishes `ds_carimbo` partitioned by `operacao_id` (old layout) and
runs; then a new version starts partitioning by `operacao_id` + `lote` (new layout) and is
published. The pointer `ds_carimbo/operacao_id=11/_atual.json` is what is left of the old layout.

Lake under `tmp` (CFLOW_LAKE_DIR), the same arrangement as `test_publicados.py`.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from cflow_core.errors import ErroAplicacao

from ._arnes_auth import usuario_logado
from .conftest import aguardar_execucao, contrato_ambiente_gold

pytestmark = pytest.mark.integration

DATASET = "ds_carimbo"
OLD_LAYOUT = [{"origem": "parametro", "nome": "operacao_id"}]
NEW_LAYOUT = [
    {"origem": "parametro", "nome": "operacao_id"},
    {"origem": "parametro", "nome": "lote"},
]
ROTA = f"/publicados/gold/{DATASET}/escopo"
AMBIENTE = {"ambiente": "dev"}


def _lake(tmp_path, monkeypatch):
    from cflow_core.config import get_settings

    monkeypatch.setenv("CFLOW_LAKE_DIR", str(tmp_path / "lake"))
    get_settings.cache_clear()
    return tmp_path / "lake"


def _definicao(particao: list[dict]) -> dict:
    return {
        "parametros": [
            {"nome": "operacao_id", "tipo": "texto", "obrigatorio": True},
            {"nome": "lote", "tipo": "texto", "obrigatorio": True},
        ],
        "nos": [
            {
                "id": "src",
                "tipo": "sql",
                "sql": "SELECT CAST(:operacao_id AS VARCHAR) AS op, CAST(:lote AS VARCHAR) AS lote",
            },
            {
                "id": "out",
                "tipo": "saida",
                "config": {
                    "destino": "lake",
                    "camada": "gold",
                    "dataset": DATASET,
                    "particao": particao,
                },
            },
        ],
        "arestas": [{"de": "src", "para": "out"}],
    }


async def _publicar(cliente, h, pid: str, definicao: dict, base: int) -> int:
    definicao = contrato_ambiente_gold(definicao)
    r = await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": base}, headers=h
    )
    assert r.status_code == 200, r.text
    numero = r.json()["numero"]
    r = await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": numero, "nota": "v"}, headers=h
    )
    assert r.status_code == 201, r.text
    # Publishing creates the NEW published version; the next draft starts from it (optimistic lock).
    return r.json()["numero"]


async def _layout_antigo_migrado(cliente, h) -> str:
    """Publish and run in the OLD layout (creates the scope pointer) and publish the NEW layout."""
    pid = (await cliente.post("/pipelines", json={"nome": "stamp-prod"}, headers=h)).json()["id"]
    numero = await _publicar(cliente, h, pid, _definicao(OLD_LAYOUT), 0)
    ex = (
        await cliente.post(
            f"/pipelines/{pid}/executar",
            json={"parametros": {"ambiente": "dev", "operacao_id": "11", "lote": "A"}},
            headers=h,
        )
    ).json()
    det = await aguardar_execucao(cliente, ex["id"], h)
    assert det["status"] == "concluida", det
    await _publicar(cliente, h, pid, _definicao(NEW_LAYOUT), numero)
    return pid


def _ponteiro(lake) -> dict:
    return json.loads(
        (lake / "gold" / "dev" / DATASET / "operacao_id=11" / "_atual.json").read_text()
    )


async def _trilha(owner_engine) -> list[dict]:
    async with owner_engine.connect() as conn:
        linhas = (
            (
                await conn.execute(
                    text(
                        "SELECT acao, ator_id, ator_tipo, antes, depois, request_id FROM auditoria "
                        "WHERE recurso_tipo = 'lake_escopo' ORDER BY id"
                    )
                )
            )
            .mappings()
            .all()
        )
    return [dict(x) for x in linhas]


async def _admin(owner_engine, cliente):
    return await usuario_logado(
        owner_engine, cliente, email="adm-carimbo@example.com", perfil="admin"
    )


async def test_carimbar_escopo_do_layout_anterior_audita_e_a_leitura_passa_a_recusar(
    cliente, owner_engine, tmp_path, monkeypatch
):
    lake = _lake(tmp_path, monkeypatch)
    uid, h = await _admin(owner_engine, cliente)
    await _layout_antigo_migrado(cliente, h)
    antes = _ponteiro(lake)

    # The query shows what the write will decide: current, stampable, current layout = the new one.
    r = await cliente.get(ROTA, params={"ambiente": "dev", "particao": "operacao_id=11"}, headers=h)
    assert r.status_code == 200, r.text
    plano = r.json()
    assert plano["estado"] == "vigente" and plano["pode_carimbar"] is True, plano
    assert plano["layout_vigente"] == ["operacao_id", "lote"]
    assert plano["aponta"]["execucao_id"] == antes["execucao_id"]
    assert plano["produtores"][0]["nome"] == "stamp-prod"

    r = await cliente.post(
        f"{ROTA}/carimbar?ambiente=dev",
        json={"particao": ["operacao_id=11"], "motivo": "the partition gained the batch"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["mudou"] is True and r.json()["estado"] == "obsoleto"

    depois = _ponteiro(lake)
    assert depois["obsoleto"]["chaves"] == ["operacao_id", "lote"]
    assert depois["obsoleto"]["motivo"] == "the partition gained the batch"
    # The previous body stays next to the mark: it is what says where the pointer used to point.
    assert {k: v for k, v in depois.items() if k != "obsoleto"} == antes

    # A `publicado` read of the old scope is now refused, naming the current layout.
    from cflow_core.ingestao import armazenamento, leitura

    with pytest.raises(ErroAplicacao) as e:
        await leitura.ler_ponteiro(
            await armazenamento.abrir(None),
            DATASET,
            "gold",
            {"operacao_id": "11"},
            ambiente="dev",
        )
    assert e.value.codigo == "lake_escopo_obsoleto"
    assert "operacao_id/lote" in e.value.mensagem

    trilha = await _trilha(owner_engine)
    assert len(trilha) == 1
    t = trilha[0]
    assert t["acao"] == "editar" and t["ator_id"] == uid and t["ator_tipo"] == "usuario"
    assert t["request_id"]
    assert t["antes"]["obsoleto"] is None
    assert t["depois"]["obsoleto"]["motivo"] == "the partition gained the batch"
    assert t["depois"]["operacao"] == "carimbar"
    assert t["depois"]["caminho"] == f"dev/{DATASET}/operacao_id=11/_atual.json"
    assert t["depois"]["aponta_execucao"] == antes["execucao_id"]


async def test_descarimbar_devolve_o_corpo_de_antes_e_audita(
    cliente, owner_engine, tmp_path, monkeypatch
):
    lake = _lake(tmp_path, monkeypatch)
    _uid, h = await _admin(owner_engine, cliente)
    await _layout_antigo_migrado(cliente, h)
    antes = _ponteiro(lake)
    corpo = {"particao": ["operacao_id=11"], "motivo": "migration"}
    assert (
        await cliente.post(f"{ROTA}/carimbar?ambiente=dev", json=corpo, headers=h)
    ).status_code == 200

    r = await cliente.post(
        f"{ROTA}/descarimbar?ambiente=dev",
        json={**corpo, "motivo": "stamped by mistake"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["mudou"] is True and r.json()["estado"] == "vigente"
    assert _ponteiro(lake) == antes

    trilha = await _trilha(owner_engine)
    assert [t["depois"]["operacao"] for t in trilha] == ["carimbar", "descarimbar"]
    assert trilha[1]["antes"]["obsoleto"]["motivo"] == "migration"
    assert trilha[1]["depois"]["obsoleto"] is None
    assert trilha[1]["depois"]["motivo"] == "stamped by mistake"

    # Unstamping a current scope is a no-op: it writes nothing and leaves no audit entry.
    r = await cliente.post(f"{ROTA}/descarimbar?ambiente=dev", json=corpo, headers=h)
    assert r.status_code == 200 and r.json()["mudou"] is False
    assert len(await _trilha(owner_engine)) == 2


async def test_simular_mostra_o_depois_sem_gravar_nem_auditar(
    cliente, owner_engine, tmp_path, monkeypatch
):
    lake = _lake(tmp_path, monkeypatch)
    _uid, h = await _admin(owner_engine, cliente)
    await _layout_antigo_migrado(cliente, h)
    antes = _ponteiro(lake)
    r = await cliente.post(
        f"{ROTA}/carimbar?ambiente=dev",
        json={"particao": ["operacao_id=11"], "motivo": "dry run", "simular": True},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["simulado"] is True and r.json()["mudou"] is True
    assert r.json()["estado"] == "obsoleto" and r.json()["carimbo"]["motivo"] == "dry run"
    assert _ponteiro(lake) == antes
    assert await _trilha(owner_engine) == []


async def test_carimbar_duas_vezes_grava_uma(cliente, owner_engine, tmp_path, monkeypatch):
    lake = _lake(tmp_path, monkeypatch)
    _uid, h = await _admin(owner_engine, cliente)
    await _layout_antigo_migrado(cliente, h)
    corpo = {"particao": ["operacao_id=11"], "motivo": "migration"}
    assert (await cliente.post(f"{ROTA}/carimbar?ambiente=dev", json=corpo, headers=h)).json()[
        "mudou"
    ] is True
    carimbado = _ponteiro(lake)
    r = await cliente.post(
        f"{ROTA}/carimbar?ambiente=dev", json={**corpo, "motivo": "again"}, headers=h
    )
    assert r.status_code == 200 and r.json()["mudou"] is False
    assert _ponteiro(lake) == carimbado  # the `desde` and the reason of the first stamp stay
    assert len(await _trilha(owner_engine)) == 1


async def test_escopo_do_layout_vigente_e_recusado(cliente, owner_engine, tmp_path, monkeypatch):
    _lake(tmp_path, monkeypatch)
    _uid, h = await _admin(owner_engine, cliente)
    await _layout_antigo_migrado(cliente, h)
    r = await cliente.post(
        f"{ROTA}/carimbar?ambiente=dev",
        json={"particao": ["operacao_id=11", "lote=A"], "motivo": "x"},
        headers=h,
    )
    assert r.status_code == 409, r.text
    assert r.json()["erro"]["codigo"] == "escopo_do_layout_vigente"
    assert "stamp-prod" in r.json()["erro"]["mensagem"]
    assert await _trilha(owner_engine) == []


async def test_recusas_de_entrada(cliente, owner_engine, tmp_path, monkeypatch):
    _lake(tmp_path, monkeypatch)
    _uid, h = await _admin(owner_engine, cliente)
    await _layout_antigo_migrado(cliente, h)
    casos = [
        ({"particao": ["operacao_id=99"], "motivo": "x"}, 404, "escopo_ausente"),
        ({"particao": [], "motivo": "x"}, 400, "particao_invalida"),
        ({"particao": ["operacao_id"], "motivo": "x"}, 400, "particao_invalida"),
        ({"particao": ["operacao_id=../x"], "motivo": "x"}, 400, "particao_valor_invalido"),
        ({"particao": ["operacao_id=11"], "motivo": "  "}, 400, "motivo_obrigatorio"),
    ]
    for corpo, status, codigo in casos:
        r = await cliente.post(f"{ROTA}/carimbar?ambiente=dev", json=corpo, headers=h)
        assert (r.status_code, r.json()["erro"]["codigo"]) == (status, codigo), (corpo, r.text)
    r = await cliente.get(
        "/publicados/gold/nobody_publishes/escopo",
        params={"ambiente": "dev", "particao": "a=1"},
        headers=h,
    )
    assert r.status_code == 404 and r.json()["erro"]["codigo"] == "publicado_sem_produtor"
    r = await cliente.get("/publicados/bronze/x/escopo", params={"particao": "a=1"}, headers=h)
    assert r.status_code == 404  # layer outside the served ones
    assert await _trilha(owner_engine) == []


async def test_so_admin_consulta_e_carimba(cliente, owner_engine, tmp_path, monkeypatch):
    lake = _lake(tmp_path, monkeypatch)
    _uid, h = await _admin(owner_engine, cliente)
    await _layout_antigo_migrado(cliente, h)
    antes = _ponteiro(lake)
    _eid, he = await usuario_logado(
        owner_engine, cliente, email="esp-carimbo@example.com", perfil="especialista"
    )
    corpo = {"particao": ["operacao_id=11"], "motivo": "x"}
    assert (
        await cliente.get(
            ROTA, params={"ambiente": "dev", "particao": "operacao_id=11"}, headers=he
        )
    ).status_code == 403
    for acao in ("carimbar", "descarimbar"):
        assert (
            await cliente.post(f"{ROTA}/{acao}?ambiente=dev", json=corpo, headers=he)
        ).status_code == 403
    assert (await cliente.post(f"{ROTA}/carimbar?ambiente=dev", json=corpo)).status_code == 401
    assert _ponteiro(lake) == antes
    assert await _trilha(owner_engine) == []


# --- Discovering the scopes of the previous layout -----------------------------------------------

ROTA_DESCOBRIR = f"/publicados/gold/{DATASET}/escopos-anteriores?ambiente=dev"
COLUNA = [{"origem": "coluna", "nome": "op"}]


async def _executar(cliente, h, pid: str, parametros: dict) -> None:
    parametros = {"ambiente": "dev", **parametros}
    ex = (
        await cliente.post(f"/pipelines/{pid}/executar", json={"parametros": parametros}, headers=h)
    ).json()
    det = await aguardar_execucao(cliente, ex["id"], h)
    assert det["status"] == "concluida", det


async def test_descobrir_acha_os_escopos_anteriores_depois_da_primeira_corrida_do_layout_novo(
    cliente, owner_engine, tmp_path, monkeypatch
):
    """The case that was left open: the 1st run of the new layout replaces the dataset index, which
    stops listing the scopes of the previous one. Discovery finds them through the runs of the old
    version."""
    lake = _lake(tmp_path, monkeypatch)
    _uid, h = await _admin(owner_engine, cliente)
    pid = (await cliente.post("/pipelines", json={"nome": "stamp-prod"}, headers=h)).json()["id"]
    numero = await _publicar(cliente, h, pid, _definicao(OLD_LAYOUT), 0)
    for op in ("11", "2"):
        await _executar(cliente, h, pid, {"operacao_id": op, "lote": "A"})
    await _publicar(cliente, h, pid, _definicao(NEW_LAYOUT), numero)
    await _executar(cliente, h, pid, {"operacao_id": "11", "lote": "A"})
    indice = json.loads((lake / "gold" / "dev" / DATASET / "_atual.json").read_text())
    assert indice["chaves"] == ["operacao_id", "lote"]  # the index no longer lists the old layout

    r = await cliente.get(ROTA_DESCOBRIR, headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    # Number as number (`2` before `11`); the scope of the NEW layout is not included.
    assert [e["particao"] for e in d["escopos"]] == [["operacao_id=2"], ["operacao_id=11"]]
    assert all(e["estado"] == "vigente" and e["pode_carimbar"] for e in d["escopos"])
    assert d["layout_vigente"] == ["operacao_id", "lote"]
    [anterior] = d["layouts_anteriores"]
    assert anterior["chaves"] == ["operacao_id"] and anterior["execucoes"] == 2
    assert anterior["versoes"][0]["nome"] == "stamp-prod"
    assert (anterior["sem_indice"], d["ausentes"], d["truncado"]) == (0, 0, False)
    assert await _trilha(owner_engine) == []  # discovering writes nothing

    # The stamped one shows up as such in the next discovery - and stays in the list.
    corpo = {"particao": ["operacao_id=11"], "motivo": "the partition gained the batch"}
    assert (
        await cliente.post(f"{ROTA}/carimbar?ambiente=dev", json=corpo, headers=h)
    ).status_code == 200
    d = (await cliente.get(ROTA_DESCOBRIR, headers=h)).json()
    assert [(e["particao"], e["estado"], e["pode_carimbar"]) for e in d["escopos"]] == [
        (["operacao_id=2"], "vigente", True),
        (["operacao_id=11"], "obsoleto", False),
    ]


def _definicao_por_coluna() -> dict:
    definicao = _definicao(COLUNA)
    definicao["nos"][0]["sql"] = "SELECT * FROM (VALUES ('11'), ('2')) AS t(op)"
    return definicao


async def test_descobrir_le_o_escopo_de_coluna_no_indice_imutavel_de_cada_corrida(
    cliente, owner_engine, tmp_path, monkeypatch
):
    """A COLUMN scope does not come from the parameters: it comes from the run immutable
    `_indice.json`, at a path that is computed. Without it, the absence is COUNTED - an empty list
    that looks complete is the defect this discovery exists to avoid."""
    lake = _lake(tmp_path, monkeypatch)
    _uid, h = await _admin(owner_engine, cliente)
    pid = (await cliente.post("/pipelines", json={"nome": "stamp-col"}, headers=h)).json()["id"]
    numero = await _publicar(cliente, h, pid, _definicao_por_coluna(), 0)
    await _executar(cliente, h, pid, {"operacao_id": "x", "lote": "A"})
    await _publicar(cliente, h, pid, _definicao(NEW_LAYOUT), numero)
    await _executar(cliente, h, pid, {"operacao_id": "11", "lote": "A"})

    d = (await cliente.get(ROTA_DESCOBRIR, headers=h)).json()
    assert [e["particao"] for e in d["escopos"]] == [["op=2"], ["op=11"]]
    assert all(e["pode_carimbar"] for e in d["escopos"])
    assert d["layouts_anteriores"][0]["chaves"] == ["op"]

    # The column-run index lives at the dataset root; the new run index, inside the scope.
    indices = list((lake / "gold" / "dev" / DATASET).glob("execucao_id=*/_indice.json"))
    assert len(indices) == 1, indices
    indices[0].unlink()
    d = (await cliente.get(ROTA_DESCOBRIR, headers=h)).json()
    assert d["escopos"] == []
    assert d["layouts_anteriores"][0]["sem_indice"] == 1


async def test_descobrir_e_so_de_admin(cliente, owner_engine, tmp_path, monkeypatch):
    _lake(tmp_path, monkeypatch)
    _uid, h = await _admin(owner_engine, cliente)
    await _layout_antigo_migrado(cliente, h)
    _eid, he = await usuario_logado(
        owner_engine, cliente, email="esp-descobre@example.com", perfil="especialista"
    )
    assert (await cliente.get(ROTA_DESCOBRIR, headers=he)).status_code == 403
    assert (await cliente.get(ROTA_DESCOBRIR)).status_code == 401
    r = await cliente.get(
        "/publicados/gold/nobody_publishes/escopos-anteriores?ambiente=dev", headers=h
    )
    assert r.status_code == 404 and r.json()["erro"]["codigo"] == "publicado_sem_produtor"


async def test_falha_ao_gravar_no_lake_nao_deixa_trilha(
    cliente, owner_engine, tmp_path, monkeypatch
):
    """The audit entry is written BEFORE the object, in the lock transaction: if the lake refuses the
    write, the rollback takes the entry with it. The inverse - a changed pointer with no record - is
    what an out-of-band script would do."""
    lake = _lake(tmp_path, monkeypatch)
    _uid, h = await _admin(owner_engine, cliente)
    await _layout_antigo_migrado(cliente, h)
    antes = _ponteiro(lake)

    from cflow_core.ingestao import armazenamento

    async def _disco_cheio(self, *a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(armazenamento.ArmazenamentoLocal, "gravar", _disco_cheio)
    try:
        r = await cliente.post(
            f"{ROTA}/carimbar?ambiente=dev",
            json={"particao": ["operacao_id=11"], "motivo": "x"},
            headers=h,
        )
        assert r.status_code == 500, r.text
    except RuntimeError as e:  # the test ASGI transport re-raises the app exception
        assert "disk full" in str(e)
    assert _ponteiro(lake) == antes
    assert await _trilha(owner_engine) == []

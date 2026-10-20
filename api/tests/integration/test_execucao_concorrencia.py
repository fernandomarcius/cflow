"""Duplicate runs: same pipeline + SAME parameters still in flight -> 409.

Motivation: overlapping scheduled runs, double clicks on "Run" and re-triggered jobs, where each
duplicate repeated e-mails/POSTs/`append`s and competed for the lake pointer file.

The RELEASE tests matter as much as the refusal: an `executando` row whose workflow died (worker
restarted before `finalizar`) is common in dev, and a 409 that trusted only the database would
block "it failed, I fixed it, I run it again" for half an hour.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text

from cflow_core.errors import ErroAplicacao
from cflow_core.execucao import engine
from cflow_core.execucao import reconciliacao as rec

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration

DEF = {
    "parametros": [{"nome": "operacao_id", "tipo": "texto", "obrigatorio": True}],
    "nos": [{"id": "n1", "tipo": "sql", "sql": "SELECT :operacao_id AS op"}],
    "arestas": [],
}


async def _publicar(cliente, h, definicao: dict = DEF) -> str:
    pid = (
        await cliente.post("/pipelines", json={"nome": f"conc-{uuid.uuid4().hex[:6]}"}, headers=h)
    ).json()["id"]
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    return pid


async def _em_voo(pid: str, uid, parametros: dict) -> str:
    """Create the `executando` run WITHOUT a workflow (what the guard will find)."""
    ctx = await engine.criar_execucao(
        pipeline_id=uuid.UUID(pid), ator_id=uid, parametros=parametros
    )
    return ctx["execucao_id"]


def _fingir_temporal(monkeypatch, *, veredito=None, erro_ao_conectar=None):
    """The guard asks Temporal whether the in-flight run is still alive. No network in the test."""

    async def _conectar(_host):
        if erro_ao_conectar:
            raise erro_ao_conectar
        return object()

    monkeypatch.setattr(rec, "conectar", _conectar)

    async def consultar(_cliente, _eid, **_kw):
        return veredito

    monkeypatch.setattr(rec, "consultar_uma", consultar)


def _viva(monkeypatch):
    _fingir_temporal(monkeypatch, veredito=rec.Veredito(viva=True, desconhecida=False))


async def _status(engine_db, eid: str) -> str:
    async with engine_db.connect() as conn:
        return (
            await conn.execute(
                text("SELECT status::text FROM execucoes WHERE id = :e"), {"e": uuid.UUID(eid)}
            )
        ).scalar_one()


async def test_mesmos_parametros_em_voo_recusa_e_aponta_a_execucao(
    owner_engine, cliente, monkeypatch
):
    uid, h = await usuario_logado(
        owner_engine, cliente, email="conc1@example.com", perfil="especialista"
    )
    pid = await _publicar(cliente, h)
    em_voo = await _em_voo(pid, uid, {"operacao_id": "6"})
    _viva(monkeypatch)

    r = await cliente.post(
        f"/pipelines/{pid}/executar", json={"parametros": {"operacao_id": "6"}}, headers=h
    )
    assert r.status_code == 409, r.text
    erro = r.json()["erro"]
    assert erro["codigo"] == "execucao_em_andamento"
    assert "already in progress" in erro["mensagem"]
    # The UI needs WHERE the in-flight run is (the "Follow" link).
    assert erro["detalhes"][0] == {"campo": "execucao_id", "mensagem": em_voo}

    # DIFFERENT parameters stay free: per-run isolation already exists.
    r2 = await cliente.post(
        f"/pipelines/{pid}/executar", json={"parametros": {"operacao_id": "51"}}, headers=h
    )
    assert r2.status_code == 200, r2.text


async def test_anterior_concluida_libera_na_hora(owner_engine, cliente, monkeypatch):
    """No cooldown: the key only exists while `status='executando'`."""
    uid, h = await usuario_logado(
        owner_engine, cliente, email="conc2@example.com", perfil="especialista"
    )
    pid = await _publicar(cliente, h)
    em_voo = await _em_voo(pid, uid, {"operacao_id": "6"})
    _viva(monkeypatch)
    await engine.finalizar(execucao_id=uuid.UUID(em_voo), erro=None)

    r = await cliente.post(
        f"/pipelines/{pid}/executar", json={"parametros": {"operacao_id": "6"}}, headers=h
    )
    assert r.status_code == 200, r.text


async def test_orfa_nao_bloqueia_e_e_fechada_no_ato(owner_engine, cliente, monkeypatch):
    """Dead workflow (worker restarted before `finalizar`): the guard closes the orphan and moves
    on, without waiting for the reaper's minimum age."""
    uid, h = await usuario_logado(
        owner_engine, cliente, email="conc3@example.com", perfil="especialista"
    )
    pid = await _publicar(cliente, h)
    orfa = await _em_voo(pid, uid, {"operacao_id": "6"})
    _fingir_temporal(monkeypatch, veredito=rec.Veredito(viva=False, desconhecida=True))

    r = await cliente.post(
        f"/pipelines/{pid}/executar", json={"parametros": {"operacao_id": "6"}}, headers=h
    )
    assert r.status_code == 200, r.text
    assert await _status(owner_engine, orfa) == "falha"


async def test_temporal_mudo_mantem_o_bloqueio(owner_engine, cliente, monkeypatch):
    """Orchestrator silence is not a verdict (same guard as the reaper): the in-flight row COUNTS."""
    uid, h = await usuario_logado(
        owner_engine, cliente, email="conc4@example.com", perfil="especialista"
    )
    pid = await _publicar(cliente, h)
    em_voo = await _em_voo(pid, uid, {"operacao_id": "6"})
    _fingir_temporal(monkeypatch, erro_ao_conectar=ConnectionRefusedError("no route"))

    r = await cliente.post(
        f"/pipelines/{pid}/executar", json={"parametros": {"operacao_id": "6"}}, headers=h
    )
    assert r.status_code == 409
    assert await _status(owner_engine, em_voo) == "executando"  # untouched


async def test_corrida_entre_dois_disparos_deixa_uma_linha_so(owner_engine, cliente, monkeypatch):
    """Two simultaneous `criar_execucao` calls: the pre-check may let both through; the partial
    unique index (`ux_execucoes_em_voo`) decides. Exactly one 409 and ONE `executando` row."""
    uid, h = await usuario_logado(
        owner_engine, cliente, email="conc5@example.com", perfil="especialista"
    )
    pid = await _publicar(cliente, h)
    _viva(monkeypatch)

    async def _um():
        return await engine.criar_execucao(
            pipeline_id=uuid.UUID(pid), ator_id=uid, parametros={"operacao_id": "6"}
        )

    res = await asyncio.gather(_um(), _um(), return_exceptions=True)
    erros = [r for r in res if isinstance(r, ErroAplicacao)]
    assert len(erros) == 1 and erros[0].codigo == "execucao_em_andamento"
    async with owner_engine.connect() as conn:
        n = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM execucoes WHERE pipeline_id = :p AND status = 'executando'"
                ),
                {"p": uuid.UUID(pid)},
            )
        ).scalar_one()
    assert n == 1


async def test_chave_ignora_ordem_das_chaves_e_o_valor_do_segredo(
    owner_engine, cliente, monkeypatch
):
    """The key is `jsonb::text` (canonical) over the REDACTED parameters: dict order does not
    matter, and two dispatches differing only in the credential count as equal (the stored value
    is `***`)."""
    uid, h = await usuario_logado(
        owner_engine, cliente, email="conc6@example.com", perfil="especialista"
    )
    definicao = {
        **DEF,
        "parametros": [
            {"nome": "operacao_id", "tipo": "texto", "obrigatorio": True},
            {"nome": "senha_api", "tipo": "texto", "segredo": True},
        ],
    }
    pid = await _publicar(cliente, h, definicao)
    await _em_voo(pid, uid, {"operacao_id": "6", "senha_api": "abc"})
    _viva(monkeypatch)

    r = await cliente.post(
        f"/pipelines/{pid}/executar",
        json={"parametros": {"senha_api": "OUTRA", "operacao_id": "6"}},
        headers=h,
    )
    assert r.status_code == 409, r.text


async def test_recusa_fica_na_trilha(owner_engine, cliente, monkeypatch):
    uid, h = await usuario_logado(
        owner_engine, cliente, email="conc7@example.com", perfil="especialista"
    )
    pid = await _publicar(cliente, h)
    em_voo = await _em_voo(pid, uid, {"operacao_id": "6"})
    _viva(monkeypatch)
    await cliente.post(
        f"/pipelines/{pid}/executar", json={"parametros": {"operacao_id": "6"}}, headers=h
    )

    async with owner_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT depois->>'recusado', depois->'parametros'->>'operacao_id', ator_id "
                    "FROM auditoria WHERE recurso_tipo='execucao' AND recurso_id = :e "
                    "AND depois->>'recusado' IS NOT NULL"
                ),
                {"e": uuid.UUID(em_voo)},
            )
        ).first()
    assert row is not None and row[0] == "execucao_em_andamento"
    assert row[1] == "6" and row[2] == uid  # who tried, with what, against which run


async def test_execucao_criada_leva_a_chave_e_vazio_e_omitido_dao_a_mesma(
    owner_engine, cliente, monkeypatch
):
    """`""` and omitted are the SAME value (existing normalisation): the key must not diverge
    between a client that sends "" and one that omits the field."""
    uid, h = await usuario_logado(
        owner_engine, cliente, email="conc8@example.com", perfil="especialista"
    )
    definicao = {
        "parametros": [
            {"nome": "operacao_id", "tipo": "texto", "obrigatorio": True},
            {"nome": "segmento", "tipo": "texto"},
        ],
        "nos": [{"id": "n1", "tipo": "sql", "sql": "SELECT :operacao_id AS op"}],
        "arestas": [],
    }
    pid = await _publicar(cliente, h, definicao)
    em_voo = await _em_voo(pid, uid, {"operacao_id": "6"})
    _viva(monkeypatch)
    async with owner_engine.connect() as conn:
        chave = (
            await conn.execute(
                text("SELECT chave_concorrencia FROM execucoes WHERE id = :e"),
                {"e": uuid.UUID(em_voo)},
            )
        ).scalar_one()
    assert chave and len(chave) == 32  # md5 filled on the root

    r = await cliente.post(
        f"/pipelines/{pid}/executar",
        json={"parametros": {"operacao_id": "6", "segmento": ""}},
        headers=h,
    )
    assert r.status_code == 409, r.text

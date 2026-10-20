"""A pipeline can be deactivated: `ativo` blocks EXECUTION (not editing/publishing), and the block
lives at the single choke point of `engine.criar_execucao`, so it holds for `/executar` and for
every other trigger that goes through there (cron scheduler, executar-encadeado, re-execution),
without duplicating the check in each route. Same audit/no-op pattern as `test_camada_pipeline.py`."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration


async def _criar(cliente, h, nome: str) -> str:
    r = await cliente.post("/pipelines", json={"nome": nome}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _achar(cliente, h, pid: str) -> dict | None:
    r = await cliente.get("/pipelines", headers=h)
    assert r.status_code == 200, r.text
    return next((x for x in r.json() if x["id"] == pid), None)


async def _editar_ativo(owner_engine, pid: str) -> list[dict]:
    """`editar` trail records that touched `ativo` (the key is in the diff)."""
    async with owner_engine.connect() as conn:
        linhas = (
            (
                await conn.execute(
                    text(
                        "SELECT antes, depois FROM auditoria WHERE acao='editar' "
                        "AND recurso_tipo='pipeline' AND recurso_id=:r ORDER BY id"
                    ),
                    {"r": pid},
                )
            )
            .mappings()
            .all()
        )
    return [
        dict(x) for x in linhas if "ativo" in (x["depois"] or {}) or "ativo" in (x["antes"] or {})
    ]


async def _publicar_simples(cliente, h, nome: str) -> str:
    """1-node SQL pipeline, published, with no dependency on an external file/connection."""
    pid = await _criar(cliente, h, nome)
    definicao = {"nos": [{"id": "n1", "tipo": "sql", "sql": "SELECT 1 AS a"}], "arestas": []}
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    return pid


async def test_pipeline_nasce_ativo(owner_engine, app_engine, cliente):
    """`ativo` is born `true`: deactivating is a deliberate action, never the default of a new pipeline."""
    _, adm = await usuario_logado(
        owner_engine, cliente, email="ativ-novo@example.com", perfil="admin"
    )
    pid = await _criar(cliente, adm, "Freshly created")
    achado = await _achar(cliente, adm, pid)
    assert achado is not None
    assert achado["ativo"] is True

    det = (await cliente.get(f"/pipelines/{pid}", headers=adm)).json()
    assert det["ativo"] is True


async def test_desativar_audita_e_noop(owner_engine, app_engine, cliente):
    """PATCH stores `ativo`, returns 204, shows up in the listing and leaves ONE trail record with
    a before/after diff. Repeating the same value is a silent no-op."""
    _, adm = await usuario_logado(
        owner_engine, cliente, email="ativ-adm@example.com", perfil="admin"
    )
    pid = await _criar(cliente, adm, "To be deactivated")

    r = await cliente.patch(f"/pipelines/{pid}/ativo", json={"ativo": False}, headers=adm)
    assert r.status_code == 204, r.text
    assert r.content == b""

    achado = await _achar(cliente, adm, pid)
    assert achado["ativo"] is False

    linhas = await _editar_ativo(owner_engine, pid)
    assert len(linhas) == 1
    assert linhas[0]["antes"] == {"ativo": True}
    assert linhas[0]["depois"] == {"ativo": False}

    # no-op: same value again -> 204 with no second record
    r2 = await cliente.patch(f"/pipelines/{pid}/ativo", json={"ativo": False}, headers=adm)
    assert r2.status_code == 204
    assert len(await _editar_ativo(owner_engine, pid)) == 1


async def test_reativar_audita(owner_engine, app_engine, cliente):
    """Reactivating is an audited edit like any other: when and by whom the pipeline was
    switched on/off can be reconstructed from the trail."""
    _, adm = await usuario_logado(
        owner_engine, cliente, email="ativ-troca@example.com", perfil="admin"
    )
    pid = await _criar(cliente, adm, "Toggled on and off")

    for valor in (False, True, False):
        r = await cliente.patch(f"/pipelines/{pid}/ativo", json={"ativo": valor}, headers=adm)
        assert r.status_code == 204, r.text

    assert (await _achar(cliente, adm, pid))["ativo"] is False
    linhas = await _editar_ativo(owner_engine, pid)
    assert [(x["antes"]["ativo"], x["depois"]["ativo"]) for x in linhas] == [
        (True, False),
        (False, True),
        (True, False),
    ]


async def test_ativo_pipeline_inexistente_404(owner_engine, app_engine, cliente):
    _, adm = await usuario_logado(
        owner_engine, cliente, email="ativ-404@example.com", perfil="admin"
    )
    r = await cliente.patch(f"/pipelines/{uuid.uuid4()}/ativo", json={"ativo": False}, headers=adm)
    assert r.status_code == 404
    assert r.json()["erro"]["codigo"] == "nao_encontrado"


async def test_especialista_nao_desativa(owner_engine, app_engine, cliente):
    """Deactivating is admin-only (same gate as `/camada` and `/sistema`): a specialist gets 403 and the
    pipeline stays active."""
    _, adm = await usuario_logado(
        owner_engine, cliente, email="ativ-a2@example.com", perfil="admin"
    )
    pid = await _criar(cliente, adm, "Not yours")
    _, esp = await usuario_logado(
        owner_engine, cliente, email="ativ-e2@example.com", perfil="especialista"
    )

    r = await cliente.patch(f"/pipelines/{pid}/ativo", json={"ativo": False}, headers=esp)
    assert r.status_code == 403
    assert (await _achar(cliente, adm, pid))["ativo"] is True


async def test_executar_pipeline_desativado_bloqueia(owner_engine, app_engine, cliente):
    """The CORE of the feature: a deactivated pipeline refuses `/executar` with 409 `pipeline_desativado`,
    creating no execution at all (not even a phantom `executando`). Reactivating releases it again; the
    block is about RUNNING, never about editing/publishing (the create and publish calls above already
    happened normally while the pipeline was still active)."""
    _, h = await usuario_logado(
        owner_engine, cliente, email="ativ-exec@example.com", perfil="admin"
    )
    pid = await _publicar_simples(cliente, h, "blocks-execution")

    assert (
        await cliente.patch(f"/pipelines/{pid}/ativo", json={"ativo": False}, headers=h)
    ).status_code == 204

    antes = (await cliente.get(f"/pipelines/{pid}/execucoes", headers=h)).json()["total"]
    r = await cliente.post(f"/pipelines/{pid}/executar", headers=h)
    assert r.status_code == 409, r.text
    assert r.json()["erro"]["codigo"] == "pipeline_desativado"
    depois = (await cliente.get(f"/pipelines/{pid}/execucoes", headers=h)).json()["total"]
    assert depois == antes  # nothing was created, not even a stuck "executando" execution

    # reactivating releases execution again
    assert (
        await cliente.patch(f"/pipelines/{pid}/ativo", json={"ativo": True}, headers=h)
    ).status_code == 204
    r2 = await cliente.post(f"/pipelines/{pid}/executar", headers=h)
    assert r2.status_code == 200, r2.text

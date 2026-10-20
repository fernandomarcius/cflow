"""Medallion layer on the pipeline (`bronze`|`silver`|`gold`|none) and its orthogonality with
`sistema`: the layer says in which TAB the pipeline appears, `sistema` says WHO can see it. Swapping
one for the other would hide the silver pipeline from whoever is entitled to see it, or expose the
admin-only one in the tab."""

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


async def _editar_camada(owner_engine, pid: str) -> list[dict]:
    """`editar` trail records that touched `camada` (the key is in the diff)."""
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
        dict(x) for x in linhas if "camada" in (x["depois"] or {}) or "camada" in (x["antes"] or {})
    ]


async def test_pipeline_nasce_sem_camada(owner_engine, app_engine, cliente):
    """No layer is a legitimate state (General tab): not a disguised default or an empty string: `null`."""
    _, adm = await usuario_logado(
        owner_engine, cliente, email="cam-novo@example.com", perfil="admin"
    )
    pid = await _criar(cliente, adm, "Plain")
    achado = await _achar(cliente, adm, pid)
    assert achado is not None
    assert achado["camada"] is None
    assert achado["sistema"] is False


async def test_marcar_camada_audita_e_noop(owner_engine, app_engine, cliente):
    """PATCH stores the layer, returns 204, shows up in the listing and leaves ONE trail record with
    a before/after diff. Repeating the same value is a silent no-op: an append-only trail must not
    fill up with identical rows on every click."""
    _, adm = await usuario_logado(
        owner_engine, cliente, email="cam-adm@example.com", perfil="admin"
    )
    pid = await _criar(cliente, adm, "Silver of orders")

    r = await cliente.patch(f"/pipelines/{pid}/camada", json={"camada": "silver"}, headers=adm)
    assert r.status_code == 204, r.text
    assert r.content == b""

    achado = await _achar(cliente, adm, pid)
    assert achado["camada"] == "silver"

    linhas = await _editar_camada(owner_engine, pid)
    assert len(linhas) == 1
    assert linhas[0]["antes"] == {"camada": None}
    assert linhas[0]["depois"] == {"camada": "silver"}

    # no-op: same value again -> 204 with no second record
    r2 = await cliente.patch(f"/pipelines/{pid}/camada", json={"camada": "silver"}, headers=adm)
    assert r2.status_code == 204
    assert len(await _editar_camada(owner_engine, pid)) == 1


async def test_trocar_e_remover_camada_audita(owner_engine, app_engine, cliente):
    """Switching layer and going back to `null` are audited edits: `null` removes the layer, it is not
    'leave alone' (an omitted body would fall to the model default and silently erase it)."""
    _, adm = await usuario_logado(
        owner_engine, cliente, email="cam-troca@example.com", perfil="admin"
    )
    pid = await _criar(cliente, adm, "Bronze that became gold")

    for valor in ("bronze", "gold", None):
        r = await cliente.patch(f"/pipelines/{pid}/camada", json={"camada": valor}, headers=adm)
        assert r.status_code == 204, r.text

    assert (await _achar(cliente, adm, pid))["camada"] is None
    linhas = await _editar_camada(owner_engine, pid)
    assert [(x["antes"]["camada"], x["depois"]["camada"]) for x in linhas] == [
        (None, "bronze"),
        ("bronze", "gold"),
        ("gold", None),
    ]


async def test_camada_invalida_no_envelope_padrao(owner_engine, app_engine, cliente):
    """A value outside the set is a 422 in the standard envelope (`camada_invalida`), not FastAPI's
    default 422, and nothing is stored or audited."""
    _, adm = await usuario_logado(
        owner_engine, cliente, email="cam-inv@example.com", perfil="admin"
    )
    pid = await _criar(cliente, adm, "Silvr?")

    r = await cliente.patch(f"/pipelines/{pid}/camada", json={"camada": "silvr"}, headers=adm)
    assert r.status_code == 422, r.text
    erro = r.json()["erro"]
    assert erro["codigo"] == "camada_invalida"
    assert "bronze" in erro["mensagem"] and "gold" in erro["mensagem"]

    assert (await _achar(cliente, adm, pid))["camada"] is None
    assert await _editar_camada(owner_engine, pid) == []


async def test_pipeline_inexistente_404(owner_engine, app_engine, cliente):
    """A pipeline that does not exist is a 404, and must not be confused with a pipeline without a
    layer, which also reads `camada = NULL` in the database."""
    _, adm = await usuario_logado(
        owner_engine, cliente, email="cam-404@example.com", perfil="admin"
    )
    r = await cliente.patch(
        f"/pipelines/{uuid.uuid4()}/camada", json={"camada": "silver"}, headers=adm
    )
    assert r.status_code == 404
    assert r.json()["erro"]["codigo"] == "nao_encontrado"


async def test_especialista_nao_define_camada(owner_engine, app_engine, cliente):
    """Setting the layer is admin-only (same gate as PATCH /sistema): a specialist gets 403 and the
    pipeline stays without a layer."""
    _, adm = await usuario_logado(owner_engine, cliente, email="cam-a2@example.com", perfil="admin")
    pid = await _criar(cliente, adm, "Not yours")
    _, esp = await usuario_logado(
        owner_engine, cliente, email="cam-e2@example.com", perfil="especialista"
    )

    r = await cliente.patch(f"/pipelines/{pid}/camada", json={"camada": "silver"}, headers=esp)
    assert r.status_code == 403
    assert (await _achar(cliente, adm, pid))["camada"] is None


async def test_camada_e_sistema_sao_ortogonais(owner_engine, app_engine, cliente):
    """The layer is NOT visibility: a silver pipeline with `sistema=false` stays visible to the
    specialist (with the layer in the response, so the front end can filter the Silver tab); and turning
    `sistema` on hides it without erasing the layer: the admin still sees both fields."""
    _, adm = await usuario_logado(
        owner_engine, cliente, email="cam-ort-a@example.com", perfil="admin"
    )
    pid = await _criar(cliente, adm, "Visible silver")
    assert (
        await cliente.patch(f"/pipelines/{pid}/camada", json={"camada": "silver"}, headers=adm)
    ).status_code == 204

    _, esp = await usuario_logado(
        owner_engine, cliente, email="cam-ort-e@example.com", perfil="especialista"
    )
    visto = await _achar(cliente, esp, pid)
    assert visto is not None, "the layer must not hide a pipeline from whoever may see it"
    assert visto["camada"] == "silver" and visto["sistema"] is False

    # `sistema` hides; the layer stays stored and visible to the admin
    assert (
        await cliente.patch(f"/pipelines/{pid}/sistema", json={"sistema": True}, headers=adm)
    ).status_code == 200
    assert await _achar(cliente, esp, pid) is None
    do_adm = await _achar(cliente, adm, pid)
    assert do_adm["sistema"] is True and do_adm["camada"] == "silver"

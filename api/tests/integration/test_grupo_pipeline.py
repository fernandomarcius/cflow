"""Pipeline group (`load`|`reference`|`export`|none) and its INDEPENDENCE from `camada` and `sistema`.

The three columns answer different questions: `camada` says which medallion layer the pipeline
produces, `grupo` says which set it belongs to and `sistema` says who can see it. A pipeline that
publishes to the `gold` layer AND belongs to a load group is the case that forces `grupo` to be its
own column: in `camada`, which holds a single value, one classification would erase the other.
"""

from __future__ import annotations

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


async def test_pipeline_nasce_sem_grupo(owner_engine, cliente):
    """No group is a legitimate state (General tab): `null`, not an empty string."""
    _, adm = await usuario_logado(
        owner_engine, cliente, email="grp-novo@example.com", perfil="admin"
    )
    pid = await _criar(cliente, adm, "Plain without group")
    achado = await _achar(cliente, adm, pid)
    assert achado is not None
    assert achado["grupo"] is None


async def test_grupo_e_camada_coexistem(owner_engine, cliente):
    """Storing the group does NOT erase the layer, and vice versa."""
    _, adm = await usuario_logado(
        owner_engine, cliente, email="grp-coex@example.com", perfil="admin"
    )
    pid = await _criar(cliente, adm, "orders_load")
    assert (
        await cliente.patch(f"/pipelines/{pid}/camada", json={"camada": "gold"}, headers=adm)
    ).status_code == 204
    assert (
        await cliente.patch(f"/pipelines/{pid}/grupo", json={"grupo": "load"}, headers=adm)
    ).status_code == 204
    achado = await _achar(cliente, adm, pid)
    assert achado["camada"] == "gold"
    assert achado["grupo"] == "load"


async def test_grupo_invalido_recusado_no_envelope_padrao(owner_engine, cliente):
    """Closed vocabulary: a value outside the list is a 422 in the standard envelope."""
    _, adm = await usuario_logado(
        owner_engine, cliente, email="grp-inv@example.com", perfil="admin"
    )
    pid = await _criar(cliente, adm, "Invalid group")
    r = await cliente.patch(f"/pipelines/{pid}/grupo", json={"grupo": "loads"}, headers=adm)
    assert r.status_code == 422
    assert r.json()["erro"]["codigo"] == "grupo_invalido"
    assert "Invalid group" in r.json()["erro"]["mensagem"]


async def test_remover_grupo(owner_engine, cliente):
    _, adm = await usuario_logado(owner_engine, cliente, email="grp-rm@example.com", perfil="admin")
    pid = await _criar(cliente, adm, "Leaves the group")
    await cliente.patch(f"/pipelines/{pid}/grupo", json={"grupo": "load"}, headers=adm)
    assert (
        await cliente.patch(f"/pipelines/{pid}/grupo", json={"grupo": None}, headers=adm)
    ).status_code == 204
    assert (await _achar(cliente, adm, pid))["grupo"] is None


async def test_so_admin_define_grupo(owner_engine, cliente):
    """The listing classification is admin-only, like the layer: generic 403 for everyone else."""
    _, adm = await usuario_logado(
        owner_engine, cliente, email="grp-adm@example.com", perfil="admin"
    )
    pid = await _criar(cliente, adm, "Admin only")
    _, esp = await usuario_logado(
        owner_engine, cliente, email="grp-esp@example.com", perfil="especialista"
    )
    assert (
        await cliente.patch(f"/pipelines/{pid}/grupo", json={"grupo": "load"}, headers=esp)
    ).status_code == 403
    assert (await _achar(cliente, adm, pid))["grupo"] is None


async def test_grupo_grava_na_trilha_com_diff(owner_engine, cliente):
    """Every relevant write becomes ONE trail record, with before/after."""
    _, adm = await usuario_logado(
        owner_engine, cliente, email="grp-aud@example.com", perfil="admin"
    )
    pid = await _criar(cliente, adm, "Audited")
    await cliente.patch(f"/pipelines/{pid}/grupo", json={"grupo": "load"}, headers=adm)
    # Silent no-op: storing the SAME value does not pollute the trail with an unchanged row.
    await cliente.patch(f"/pipelines/{pid}/grupo", json={"grupo": "load"}, headers=adm)
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
    do_grupo = [x for x in linhas if "grupo" in (x["depois"] or {})]
    assert len(do_grupo) == 1
    assert do_grupo[0]["antes"] == {"grupo": None}
    assert do_grupo[0]["depois"] == {"grupo": "load"}


@pytest.mark.parametrize("grupo", ["load", "reference", "export"])
async def test_todos_os_valores_do_vocabulario_sao_aceitos(owner_engine, cliente, grupo):
    """Each value of the closed vocabulary is accepted, and none of them touches the layer."""
    _, adm = await usuario_logado(
        owner_engine, cliente, email=f"grp-voc-{grupo}@example.com", perfil="admin"
    )
    pid = await _criar(cliente, adm, f"Pipeline {grupo}")
    r = await cliente.patch(f"/pipelines/{pid}/grupo", json={"grupo": grupo}, headers=adm)
    assert r.status_code == 204, r.text
    achado = await _achar(cliente, adm, pid)
    assert achado["grupo"] == grupo
    assert achado["camada"] is None


@pytest.mark.parametrize("grafia", ["Load", "LOAD", "re-ference", "exports", "batch"])
async def test_grafia_fora_do_vocabulario_de_grupo_e_recusada(owner_engine, cliente, grafia):
    """Free text would turn 'load', 'Load' and 'LOAD' into separate values in the same listing,
    each spelling opening its own tab."""
    _, adm = await usuario_logado(
        owner_engine, cliente, email=f"grp-grafia-{grafia}@example.com", perfil="admin"
    )
    pid = await _criar(cliente, adm, "Crooked spelling")
    r = await cliente.patch(f"/pipelines/{pid}/grupo", json={"grupo": grafia}, headers=adm)
    assert r.status_code == 422, r.text
    assert r.json()["erro"]["codigo"] == "grupo_invalido"
    assert (await _achar(cliente, adm, pid))["grupo"] is None

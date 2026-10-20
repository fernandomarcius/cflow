"""SERVICE account reading `GET /publicados/{camada}/{dataset}/{dados,indice}`.

Integrations read gold with a service token, without a personal password. The service account
reaches EVERY pipeline without a grant, `sistema` included - so it reads the run of any of them. A
nonexistent run is still the generic 403 (a 404 would tell the integrator that the id exists); a
revoked token is 401.
"""

from __future__ import annotations

import uuid

from cflow_core.authz import permissoes
from cflow_core.db import transacao

from ._arnes_auth import semear_usuario, usuario_logado
from .test_publicados import _criar_publicado, _dados, _def_projects, _executar, _lake

SENHA = "password-the-service-account-never-uses-1"


async def _cenario(owner_engine, cliente, tmp_path, monkeypatch, sufixo: str):
    _lake(tmp_path, monkeypatch)
    admin, h = await usuario_logado(
        owner_engine, cliente, email=f"adm.pubsvc.{sufixo}@example.com", perfil="admin"
    )
    pid = await _criar_publicado(cliente, h, _def_projects(f"svc_{sufixo}"), f"Projects {sufixo}")
    ex = await _executar(cliente, h, pid, {"operacao_id": "61", "lote": "A"})
    uid = await semear_usuario(
        owner_engine, email=f"svc.pub.{sufixo}@example.com", senha=SENHA, perfil="servico"
    )
    corpo = (
        await cliente.post(
            f"/usuarios/{uid}/tokens", json={"nome": "Integration token", "dias": 30}, headers=h
        )
    ).json()
    hs = {"Authorization": f"Bearer {corpo['token']}"}
    return admin, h, pid, ex, uid, corpo["id"], hs


async def _conceder(uid: uuid.UUID, pid: str, admin: uuid.UUID, nivel: str = "ver") -> None:
    async with transacao() as conn:
        await permissoes.conceder(
            conn,
            sujeito_tipo="usuario",
            sujeito_id=uid,
            recurso_tipo="pipeline",
            recurso_id=uuid.UUID(pid),
            nivel=nivel,
            concedido_por=admin,
        )


async def test_servico_com_ver_le_dados_e_indice(owner_engine, cliente, tmp_path, monkeypatch):
    admin, h, pid, ex, uid, _, hs = await _cenario(
        owner_engine, cliente, tmp_path, monkeypatch, "ver"
    )
    await _conceder(uid, pid, admin)

    r = await cliente.get(_dados("svc_ver"), params={"execucao_id": ex}, headers=hs)
    assert r.status_code == 200, r.text
    # The SAME page the specialist reads: the service account does not get a slice of its own.
    r_h = await cliente.get(_dados("svc_ver"), params={"execucao_id": ex}, headers=h)
    assert r.json() == r_h.json()

    r = await cliente.get("/publicados/gold/svc_ver/indice", params={"execucao_id": ex}, headers=hs)
    assert r.status_code == 200, r.text


async def test_executar_inclui_ver(owner_engine, cliente, tmp_path, monkeypatch):
    admin, _, pid, ex, uid, _, hs = await _cenario(
        owner_engine, cliente, tmp_path, monkeypatch, "exe"
    )
    await _conceder(uid, pid, admin, nivel="executar")
    r = await cliente.get(_dados("svc_exe"), params={"execucao_id": ex}, headers=hs)
    assert r.status_code == 200, r.text


async def test_servico_le_sem_concessao_e_execucao_inexistente_da_403(
    owner_engine, cliente, tmp_path, monkeypatch
):
    _, _, _, ex, _, _, hs = await _cenario(owner_engine, cliente, tmp_path, monkeypatch, "sem")

    # Without any grant: the service account reaches every pipeline.
    sem = await cliente.get(_dados("svc_sem"), params={"execucao_id": ex}, headers=hs)
    assert sem.status_code == 200, sem.text
    indice = await cliente.get(
        "/publicados/gold/svc_sem/indice", params={"execucao_id": ex}, headers=hs
    )
    assert indice.status_code == 200, indice.text
    inexistente = await cliente.get(
        _dados("svc_sem"), params={"execucao_id": str(uuid.uuid4())}, headers=hs
    )
    assert inexistente.status_code == 403, inexistente.text
    assert inexistente.json()["erro"]["codigo"] == "acesso_negado"


async def test_pipeline_sistema_tambem_abre_para_servico(
    owner_engine, cliente, tmp_path, monkeypatch
):
    _, h, pid, ex, _, _, hs = await _cenario(owner_engine, cliente, tmp_path, monkeypatch, "sis")
    r = await cliente.patch(f"/pipelines/{pid}/sistema", json={"sistema": True}, headers=h)
    assert r.status_code == 200, r.text

    r = await cliente.get(_dados("svc_sis"), params={"execucao_id": ex}, headers=hs)
    assert r.status_code == 200, r.text


async def test_token_revogado_e_401(owner_engine, cliente, tmp_path, monkeypatch):
    admin, h, pid, ex, uid, token_id, hs = await _cenario(
        owner_engine, cliente, tmp_path, monkeypatch, "rev"
    )
    await _conceder(uid, pid, admin)
    assert (
        await cliente.get(_dados("svc_rev"), params={"execucao_id": ex}, headers=hs)
    ).status_code == 200

    assert (
        await cliente.delete(f"/usuarios/{uid}/tokens/{token_id}", headers=h)
    ).status_code == 204
    r = await cliente.get(_dados("svc_rev"), params={"execucao_id": ex}, headers=hs)
    assert r.status_code == 401, r.text


async def test_carimbo_continua_so_admin(owner_engine, cliente, tmp_path, monkeypatch):
    admin, _, pid, _, uid, _, hs = await _cenario(
        owner_engine, cliente, tmp_path, monkeypatch, "car"
    )
    await _conceder(uid, pid, admin, nivel="executar")
    r = await cliente.get(
        "/publicados/gold/svc_car/escopo", params={"particao": ["operacao_id=61"]}, headers=hs
    )
    assert r.status_code == 403, r.text

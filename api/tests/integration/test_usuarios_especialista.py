"""The specialist creates and manages OPERATORS, and only them.

The original segregation of duties said "only admin manages identity/access". The reach was
widened by product decision: the specialist now creates the operators who will consume the portal,
without depending on an admin for each registration. What did NOT change is what prevents
escalation:

- creating/managing applies only to `operador` targets (`GERIDOS_PELO_ESPECIALISTA`);
- changing PROFILE remains admin-only: it is the door that would turn "I can create operators"
  into "I can create an admin in two steps";
- the specialist's listing shows only what they manage, so no row offers an action that would
  answer 403.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from cflow_core.auth import notificacao
from cflow_core.auth.notificacao import ProvedorMemoria

from ._arnes_auth import semear_usuario, usuario_logado

pytestmark = pytest.mark.integration


@pytest.fixture
def canal():
    mem = ProvedorMemoria()
    notificacao.definir_provedor(mem)
    yield mem
    notificacao.definir_provedor(None)


async def _especialista(owner_engine, cliente, email="esp@example.com"):
    return await usuario_logado(owner_engine, cliente, email=email, perfil="especialista")


async def _trilha(owner_engine, recurso_tipo: str) -> list[dict]:
    async with owner_engine.begin() as conn:
        linhas = (
            await conn.execute(
                text(
                    "SELECT acao, ator_id, ator_tipo, recurso_id, depois FROM auditoria "
                    "WHERE recurso_tipo = :t ORDER BY ocorrido_em"
                ),
                {"t": recurso_tipo},
            )
        ).mappings()
        return [dict(x) for x in linhas]


async def test_especialista_cria_operador_e_o_link_sai(owner_engine, app_engine, cliente, canal):
    esp_id, h = await _especialista(owner_engine, cliente)
    r = await cliente.post(
        "/usuarios",
        json={"email": "op1@example.com", "nome": "Op Um", "perfil": "operador"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    # the first-password token goes through the channel, never in the response, like the admin path
    assert canal.enviados[0][0] == "op1@example.com"
    assert canal.enviados[0][1] not in r.text

    trilha = await _trilha(owner_engine, "usuario")
    criar = [t for t in trilha if t["acao"] == "criar"]
    assert len(criar) == 1
    assert criar[0]["ator_id"] == esp_id  # the creator is the specialist, not "the system"
    assert criar[0]["depois"]["perfil"] == "operador"


# `servico` is the case that matters most after admin: an integration account executes pipelines
# by token, with no human in front.
@pytest.mark.parametrize("perfil", ["admin", "especialista", "auditor", "servico"])
async def test_especialista_nao_cria_outro_perfil(owner_engine, app_engine, cliente, canal, perfil):
    """403 and NOTHING written: the dangerous case is an admin created by someone who does not
    manage identity."""
    _, h = await _especialista(owner_engine, cliente, email=f"esp-{perfil}@example.com")
    r = await cliente.post(
        "/usuarios",
        json={"email": f"x-{perfil}@example.com", "nome": "X", "perfil": perfil},
        headers=h,
    )
    assert r.status_code == 403
    assert r.json()["erro"]["codigo"] == "nao_autorizado"
    assert canal.enviados == []
    async with owner_engine.begin() as conn:
        existe = (
            await conn.execute(
                text("SELECT 1 FROM usuarios WHERE email = :e"),
                {"e": f"x-{perfil}@example.com"},
            )
        ).first()
    assert existe is None


async def test_perfil_invalido_e_400_antes_do_403(owner_engine, app_engine, cliente, canal):
    """A mistyped "admni" is invalid input; a 403 there would suggest the profile exists."""
    _, h = await _especialista(owner_engine, cliente, email="esp-inv@example.com")
    r = await cliente.post(
        "/usuarios",
        json={"email": "y@example.com", "nome": "Y", "perfil": "admni"},
        headers=h,
    )
    assert r.status_code == 400
    assert r.json()["erro"]["codigo"] == "perfil_invalido"


async def test_especialista_nao_troca_perfil_de_ninguem(owner_engine, app_engine, cliente, canal):
    """The two-step escalation (create operator -> promote to admin) dies here."""
    _, h = await _especialista(owner_engine, cliente, email="esp-esc@example.com")
    uid = (
        await cliente.post(
            "/usuarios",
            json={"email": "op-esc@example.com", "nome": "Op", "perfil": "operador"},
            headers=h,
        )
    ).json()["id"]

    r = await cliente.patch(f"/usuarios/{uid}/perfil", json={"perfil": "admin"}, headers=h)
    assert r.status_code == 403
    async with owner_engine.begin() as conn:
        perfil = (
            await conn.execute(text("SELECT perfil FROM usuarios WHERE id = :i"), {"i": uid})
        ).scalar_one()
    assert perfil == "operador"


async def test_especialista_administra_o_operador_que_criou(
    owner_engine, app_engine, cliente, canal
):
    """Resend link, correct e-mail and suspend: the fix for "the e-mail did not arrive" must not
    go back to depending on an admin, otherwise creating operators delivers only half the way."""
    _, h = await _especialista(owner_engine, cliente, email="esp-adm@example.com")
    uid = (
        await cliente.post(
            "/usuarios",
            json={"email": "errado@exmaple.com", "nome": "Op", "perfil": "operador"},
            headers=h,
        )
    ).json()["id"]

    r = await cliente.patch(
        f"/usuarios/{uid}/email", json={"email": "certo@example.com"}, headers=h
    )
    assert r.status_code == 200, r.text
    assert r.json()["email"] == "certo@example.com"

    assert (await cliente.post(f"/usuarios/{uid}/reenviar-acesso", headers=h)).status_code == 200
    assert (
        await cliente.patch(f"/usuarios/{uid}/ativo", json={"ativo": False}, headers=h)
    ).status_code == 204
    assert (
        await cliente.patch(f"/usuarios/{uid}/ativo", json={"ativo": True}, headers=h)
    ).status_code == 204


@pytest.mark.parametrize("perfil_alvo", ["admin", "especialista", "auditor", "servico"])
async def test_especialista_nao_suspende_quem_nao_administra(
    owner_engine, app_engine, cliente, canal, perfil_alvo
):
    """Suspending an admin would be a denial of service against whoever administers the platform."""
    alvo = await semear_usuario(
        owner_engine, email=f"alvo-{perfil_alvo}@example.com", perfil=perfil_alvo
    )
    _, h = await _especialista(owner_engine, cliente, email=f"esp-s-{perfil_alvo}@example.com")

    r = await cliente.patch(f"/usuarios/{alvo}/ativo", json={"ativo": False}, headers=h)
    assert r.status_code == 403
    async with owner_engine.begin() as conn:
        ativo = (
            await conn.execute(text("SELECT ativo FROM usuarios WHERE id = :i"), {"i": alvo})
        ).scalar_one()
    assert ativo is True


async def test_alvo_inexistente_responde_403_para_especialista(
    owner_engine, app_engine, cliente, canal
):
    """A 404 would tell a specialist that the id exists and belongs to a profile outside their
    reach."""
    import uuid

    _, h = await _especialista(owner_engine, cliente, email="esp-404@example.com")
    r = await cliente.post(f"/usuarios/{uuid.uuid4()}/reenviar-acesso", headers=h)
    assert r.status_code == 403


async def test_admin_continua_vendo_404_em_alvo_inexistente(
    owner_engine, app_engine, cliente, canal
):
    import uuid

    _, h = await usuario_logado(owner_engine, cliente, email="adm404@example.com", perfil="admin")
    r = await cliente.post(f"/usuarios/{uuid.uuid4()}/reenviar-acesso", headers=h)
    assert r.status_code == 404


async def test_listagem_do_especialista_traz_so_operadores(
    owner_engine, app_engine, cliente, canal
):
    _, ha = await usuario_logado(owner_engine, cliente, email="adm-l@example.com", perfil="admin")
    await semear_usuario(owner_engine, email="aud-l@example.com", perfil="auditor")
    _, he = await _especialista(owner_engine, cliente, email="esp-l@example.com")
    await cliente.post(
        "/usuarios",
        json={"email": "op-l@example.com", "nome": "Op", "perfil": "operador"},
        headers=he,
    )

    do_esp = (await cliente.get("/usuarios", headers=he)).json()
    assert {u["perfil"] for u in do_esp} == {"operador"}
    assert {u["email"] for u in do_esp} == {"op-l@example.com"}
    # the specialist does not appear in their own list: they do not manage themselves
    assert "esp-l@example.com" not in {u["email"] for u in do_esp}

    do_admin = {u["email"] for u in (await cliente.get("/usuarios", headers=ha)).json()}
    assert {"adm-l@example.com", "aud-l@example.com", "esp-l@example.com"} <= do_admin


@pytest.mark.parametrize("perfil", ["operador", "auditor"])
async def test_operador_e_auditor_seguem_sem_gerir_usuario(
    owner_engine, app_engine, cliente, canal, perfil
):
    _, h = await usuario_logado(owner_engine, cliente, email=f"{perfil}@example.com", perfil=perfil)
    assert (await cliente.get("/usuarios", headers=h)).status_code == 403
    assert (
        await cliente.post(
            "/usuarios",
            json={"email": "z@example.com", "nome": "Z", "perfil": "operador"},
            headers=h,
        )
    ).status_code == 403


async def test_especialista_nao_emite_token_de_api(owner_engine, app_engine, cliente, canal):
    """An API token is the service account's credential: issuing stays with the admin."""
    alvo = await semear_usuario(owner_engine, email="svc-tok@example.com", perfil="servico")
    _, h = await _especialista(owner_engine, cliente, email="esp-tok@example.com")
    r = await cliente.post(f"/usuarios/{alvo}/tokens", json={"nome": "x"}, headers=h)
    assert r.status_code == 403
    assert (await cliente.get(f"/usuarios/{alvo}/tokens", headers=h)).status_code == 403

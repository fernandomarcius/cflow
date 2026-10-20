"""`scripts/conta_servico.py`: the account an external scheduler uses to trigger pipelines.

What these tests pin down, and why each one is expensive if it breaks:

- **The account really signs in** (`POST /auth/login` with the given password): an account created
  without a usable password is the outcome in which the script "succeeded" and the 2 a.m. trigger
  answers 401, including the account that already existed, created through the UI, whose first
  password was never set.
- **`executar` only on the requested roots, proven THROUGH THE ROUTE.**
  `POST /pipelines/{id}/executar-encadeado` passes for the requested one and gives 403 for its
  neighbour; `GET /execucoes/{id}` of ANOTHER actor's run gives 200 (that is why the specialist
  profile is used: to follow what it triggered and what the chain triggered).
- **The limit of the `especialista` profile, stated in a test so nobody reads the script as
  delivering more.** The account is NOT "execute only": a specialist creates its own pipelines
  (`POST /pipelines` only requires Editor, and the creator administers), and from there runs
  whatever it wants on the existing connections.
- **The `servico` profile is least privilege:** no password, token in a NEW 0600 file (never in
  stdout, the log or the trail), triggers and follows only what it may execute and gets 403 when
  authoring. An existing file, a password for a service account and a token for a non-service
  account are refused before writing; a failure midway undoes the account, the token and the file.
- **Resetting is a real rotation.** `--reset-password` revokes ALL of the account's sessions and
  burns pending tokens: whoever kept the Bearer of the leaked password gets 401 on the next request.
- **The trail says WHO ran it** (`--actor`, an active admin): never "the first admin" by
  `criado_em`.
- **Neither token nor password leaks.** The core's first-password path generates a token that, over
  the default channel without SMTP (`ProvedorConsole`), is PRINTED to stdout. Here the token is
  consumed in-process and the channel is never called; the test proves both halves (empty channel
  AND clean output).
- **Idempotent.** Running again does not change the password, does not duplicate a permission and
  does not push into the append-only trail a record claiming a change where there was none.
- **All or nothing.** A missing, ambiguous (the name is NOT unique in the database) or `sistema`
  pipeline is refused BEFORE any write: a half-created account, without the permission, is the
  worst way to fail.
"""

from __future__ import annotations

import importlib.util
import io
import json
import logging
import sys
import uuid

import pytest
from sqlalchemy import text

from cflow_api.routes import pipelines as rotas_pipelines
from cflow_core.auth import notificacao, recuperacao
from cflow_core.authz import permissoes
from cflow_core.db import transacao
from cflow_core.errors import ErroAplicacao
from cflow_core.execucao import engine as execucao
from cflow_core.pipelines import service as pipelines
from cflow_core.usuarios import service as usuarios

from ._arnes_auth import login_token, semear_usuario
from ._plataforma import RAIZ_PLATAFORMA as RAIZ

pytestmark = pytest.mark.integration


SENHA = "service-account-password-1"
EMAIL = "svc.etl@example.com"
ADMIN = "admin@example.com"
DEFINICAO = {"nos": [{"id": "n1", "tipo": "sql", "sql": "SELECT 1 AS a"}], "arestas": []}


def _carregar():
    """Load the script by its REAL path (it is not an installable package; copying the snippet
    here would test a copy)."""
    caminho = RAIZ / "scripts" / "conta_servico.py"
    spec = importlib.util.spec_from_file_location("conta_servico", caminho)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["conta_servico"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def script():
    return _carregar()


@pytest.fixture
def canal(monkeypatch):
    """In-memory notification channel: if the script delivered the token through it, it would
    show up here. Without this the default test provider would be the console, which prints the
    token."""
    mem = notificacao.ProvedorMemoria()
    monkeypatch.setattr(notificacao, "_provedor", mem)
    return mem


@pytest.fixture
def tokens(monkeypatch):
    """Capture every password-setting token generated, to prove none leaves the process."""
    capturados: list[str] = []
    original = recuperacao._gerar_token

    async def _espiao(conn, usuario_id, *, segundos):
        token = await original(conn, usuario_id, segundos=segundos)
        capturados.append(token)
        return token

    monkeypatch.setattr(recuperacao, "_gerar_token", _espiao)
    return capturados


async def _admin(owner_engine, email: str = ADMIN) -> uuid.UUID:
    return await semear_usuario(
        owner_engine, email=email, senha="long-admin-password", perfil="admin"
    )


async def _pipeline(nome: str, dono: uuid.UUID, *, publicar: bool = False) -> uuid.UUID:
    async with transacao() as conn:
        pid = await pipelines.criar(conn, nome=nome, descricao=None, dono_id=dono)
        if publicar:
            await pipelines.salvar_rascunho(
                conn, pipeline_id=pid, definicao=DEFINICAO, base_numero=0, autor_id=dono
            )
            await pipelines.publicar(conn, pipeline_id=pid, base_numero=1, nota="v1", autor_id=dono)
        return pid


async def _marco(owner_engine) -> int:
    """Last id of the trail: what the script wrote is what comes AFTER it."""
    async with owner_engine.connect() as conn:
        return (await conn.execute(text("SELECT coalesce(max(id), 0) FROM auditoria"))).scalar_one()


async def _trilha_desde(owner_engine, marco: int) -> list[dict]:
    async with owner_engine.connect() as conn:
        linhas = (
            (
                await conn.execute(
                    text(
                        "SELECT acao, recurso_tipo, recurso_id, ator_id, antes, depois "
                        "FROM auditoria WHERE id > :m ORDER BY id"
                    ),
                    {"m": marco},
                )
            )
            .mappings()
            .all()
        )
    return [dict(linha) for linha in linhas]


async def _usuario(owner_engine, email: str = EMAIL) -> dict | None:
    async with owner_engine.connect() as conn:
        r = (
            (
                await conn.execute(
                    text("SELECT id, email, perfil, senha_hash FROM usuarios WHERE email = :e"),
                    {"e": email},
                )
            )
            .mappings()
            .first()
        )
    return dict(r) if r else None


async def _permissoes_da_conta(owner_engine) -> int:
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT count(*) FROM permissoes p JOIN usuarios u ON u.id = p.sujeito_id "
                    "WHERE p.sujeito_tipo = 'usuario' AND u.email = :e"
                ),
                {"e": EMAIL},
            )
        ).scalar_one()


async def test_cria_conta_que_loga_e_recebe_executar_so_no_pipeline_pedido(
    owner_engine, cliente, script, canal, tokens, capfd, caplog
):
    caplog.set_level(logging.DEBUG)
    admin = await _admin(owner_engine)
    pid_a = await _pipeline("load_a", admin)
    pid_b = await _pipeline("load_b", admin)
    marco = await _marco(owner_engine)

    # E-mail with mixed case and whitespace: login is an exact match, so the script normalises.
    linhas = await script.aplicar(
        email="  Svc.Etl@Example.com ",
        nome="Service account - ETL",
        senha=SENHA,
        pipelines=["load_a"],
        ator=ADMIN,
    )

    u = await _usuario(owner_engine)
    assert u is not None and u["perfil"] == "especialista"
    # Read BEFORE the login: the login itself writes `login/sessao`, which is not the script's.
    trilha = await _trilha_desde(owner_engine, marco)

    # Really signs in, with the given password.
    r = await cliente.post("/auth/login", json={"email": EMAIL, "senha": SENHA})
    assert r.status_code == 200, r.text

    # `executar` on A; nothing on B. (The whole route is proven in
    # `test_pela_rota_dispara_so_a_raiz_pedida_e_acompanha_execucao_de_outro_ator`.)
    async with transacao() as conn:
        await permissoes.exigir(conn, u["id"], "pipeline", pid_a, "executar")
        with pytest.raises(ErroAplicacao) as negado:
            await permissoes.exigir(conn, u["id"], "pipeline", pid_b, "executar")
    assert negado.value.status == 403

    # Trail: user created, password set, permission granted; the actor is the admin.
    resumo = [(t["acao"], t["recurso_tipo"]) for t in trilha]
    assert resumo == [("criar", "usuario"), ("senha_trocada", "usuario"), ("criar", "permissao")]
    assert all(t["ator_id"] == admin for t in trilha)
    assert trilha[1]["recurso_id"] == u["id"]
    perm = trilha[2]
    assert perm["recurso_id"] == pid_a
    assert perm["depois"]["sujeito_id"] == str(u["id"])
    assert perm["depois"]["nivel"] == "executar"

    # No secret: neither password, token nor hash, in the trail, the output or the log.
    assert len(tokens) == 1
    token = tokens[0]
    diff = json.dumps([(t["antes"], t["depois"]) for t in trilha], default=str)
    saida = capfd.readouterr()
    texto = "\n".join([*linhas, saida.out, saida.err, caplog.text])
    for segredo in (SENHA, token, u["senha_hash"]):
        assert segredo not in diff
        assert segredo not in texto
    # And the notification channel was not used: without SMTP it is the console, which prints the
    # token.
    assert canal.enviados == []


async def test_pela_rota_dispara_so_a_raiz_pedida_e_acompanha_execucao_de_outro_ator(
    owner_engine, cliente, script, canal, monkeypatch
):
    """What the external scheduler really does, through the API, with the created account. The
    trigger itself (Temporal) is replaced by a call record: what is proven here is the edge,
    `Editor` profile + `permissoes.exigir(..., 'executar')`, which runs BEFORE it."""
    admin = await _admin(owner_engine)
    pid_a = await _pipeline("load_a", admin, publicar=True)
    pid_b = await _pipeline("load_b", admin, publicar=True)
    await script.aplicar(email=EMAIL, nome="svc", senha=SENHA, pipelines=["load_a"], ator=ADMIN)
    h = {"Authorization": f"Bearer {await login_token(cliente, EMAIL, SENHA)}"}

    disparos: list[uuid.UUID] = []

    async def _disparo_registrado(**kw):
        disparos.append(kw["pipeline_id"])
        return {"id": str(uuid.uuid4()), "status": "executando"}

    monkeypatch.setattr(rotas_pipelines, "_executar_dag", _disparo_registrado)

    r = await cliente.post(f"/pipelines/{pid_a}/executar-encadeado", headers=h)
    assert r.status_code == 200, r.text
    assert disparos == [pid_a]

    r = await cliente.post(f"/pipelines/{pid_b}/executar-encadeado", headers=h)
    assert r.status_code == 403, r.text
    assert r.json()["erro"]["codigo"] == "acesso_negado"
    assert disparos == [pid_a], "the refusal happens before the trigger"

    # A run triggered by ANOTHER actor (the admin): the account follows it; this is the state
    # read that the specialist profile unlocks (`_autorizar_leitura_execucao`).
    ctx = await execucao.criar_execucao(pipeline_id=pid_a, ator_id=admin)
    r = await cliente.get(f"/execucoes/{ctx['execucao_id']}", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "executando"


async def test_limite_conhecido_a_conta_cria_pipeline_proprio(owner_engine, cliente, script, canal):
    """FACT, not wish: with the `especialista` profile the service account is a full specialist.

    `POST /pipelines` only requires the Editor profile and the creator becomes `administrar` of
    what they created, so with the account's password one can create a pipeline, write a node on
    any connection and run it. The `executar` permission granted by name limits what it triggers
    AMONG THE EXISTING PIPELINES, not what it can do. The `servico` profile closes this; see
    `test_perfil_servico_entra_por_token_em_arquivo_0600_e_nao_autora`. This test stays because
    the `especialista` path still exists: if it starts returning 403, the specialist's RBAC
    changed, not the service account."""
    admin = await _admin(owner_engine)
    await _pipeline("load_a", admin, publicar=True)
    await script.aplicar(email=EMAIL, nome="svc", senha=SENHA, pipelines=["load_a"], ator=ADMIN)
    h = {"Authorization": f"Bearer {await login_token(cliente, EMAIL, SENHA)}"}

    r = await cliente.post("/pipelines", json={"nome": "created_by_the_service_account"}, headers=h)

    assert r.status_code == 201, (
        "KNOWN LIMIT: a service account with the specialist profile creates its own pipeline; "
        f"least privilege is the servico profile. Response: {r.status_code} {r.text}"
    )


async def test_segunda_corrida_e_idempotente_e_nao_troca_a_senha(
    owner_engine, cliente, script, canal
):
    admin = await _admin(owner_engine)
    await _pipeline("load_a", admin)
    await script.aplicar(email=EMAIL, nome="svc", senha=SENHA, pipelines=["load_a"], ator=ADMIN)
    hash_antes = (await _usuario(owner_engine))["senha_hash"]
    marco = await _marco(owner_engine)

    linhas = await script.aplicar(
        email=EMAIL,
        nome="svc",
        senha="another-long-password",
        pipelines=["load_a"],
        ator=ADMIN,
    )

    assert await _trilha_desde(owner_engine, marco) == []
    assert (await _usuario(owner_engine))["senha_hash"] == hash_antes
    texto = "\n".join(linhas)
    assert "exists" in texto and "kept" in texto and "existed" in texto
    async with owner_engine.connect() as conn:
        n = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM permissoes WHERE sujeito_tipo = 'usuario' AND nivel = 'executar'"
                )
            )
        ).scalar_one()
    assert n == 1
    # The old password is still valid.
    r = await cliente.post("/auth/login", json={"email": EMAIL, "senha": SENHA})
    assert r.status_code == 200, r.text


async def test_redefinir_senha_troca_e_audita(owner_engine, cliente, script, canal, tokens, capfd):
    admin = await _admin(owner_engine)
    await script.aplicar(email=EMAIL, nome="svc", senha=SENHA, ator=ADMIN)
    marco = await _marco(owner_engine)
    nova = "new-service-account-password"

    await script.aplicar(email=EMAIL, nome="svc", senha=nova, redefinir_senha=True, ator=ADMIN)

    trilha = await _trilha_desde(owner_engine, marco)
    assert [(t["acao"], t["ator_id"]) for t in trilha] == [("senha_trocada", admin)]
    assert (
        await cliente.post("/auth/login", json={"email": EMAIL, "senha": nova})
    ).status_code == 200
    assert (
        await cliente.post("/auth/login", json={"email": EMAIL, "senha": SENHA})
    ).status_code == 401
    saida = capfd.readouterr()
    for segredo in (nova, *tokens):
        assert segredo not in saida.out + saida.err
    assert canal.enviados == []


async def test_redefinir_senha_revoga_sessoes_e_queima_tokens_pendentes(
    owner_engine, cliente, script, canal, tokens, capfd
):
    """Rotation after a leak: whoever logged in with the old password and kept the Bearer cannot
    keep triggering until the session expires. And a recovery link requested earlier (by whoever
    owns the mailbox) dies too, otherwise it would reset over the new password."""
    admin = await _admin(owner_engine)
    await script.aplicar(email=EMAIL, nome="svc", senha=SENHA, ator=ADMIN)
    bearer = await login_token(cliente, EMAIL, SENHA)
    h = {"Authorization": f"Bearer {bearer}"}
    assert (await cliente.get("/auth/me", headers=h)).status_code == 200
    # The ADMIN's session is not the account's: it must survive.
    h_admin = {
        "Authorization": f"Bearer {await login_token(cliente, ADMIN, 'long-admin-password')}"
    }
    async with transacao() as conn:
        pendente = await recuperacao.solicitar(conn, EMAIL)
    assert pendente
    marco = await _marco(owner_engine)
    nova = "new-service-account-password"

    linhas = await script.aplicar(
        email=EMAIL, nome="svc", senha=nova, redefinir_senha=True, ator=ADMIN
    )
    # Read BEFORE any new login: the login writes `login/sessao`, which is not the script's.
    trilha = await _trilha_desde(owner_engine, marco)

    assert (await cliente.get("/auth/me", headers=h)).status_code == 401
    assert (await cliente.get("/auth/me", headers=h_admin)).status_code == 200
    async with transacao() as conn:
        with pytest.raises(ErroAplicacao) as e:
            await recuperacao.confirmar(conn, pendente, "password-through-the-old-email-link")
    assert e.value.codigo == "token_invalido"
    # The new password works; revoking is not locking the account.
    assert (
        await cliente.post("/auth/login", json={"email": EMAIL, "senha": nova})
    ).status_code == 200

    # One row in the trail (the change), with the counts and no secret at all.
    assert [(t["acao"], t["ator_id"]) for t in trilha] == [("senha_trocada", admin)]
    assert trilha[0]["depois"]["sessoes_revogadas"] == 1
    assert trilha[0]["depois"]["tokens_invalidados"] == 1
    diff = json.dumps(trilha[0]["depois"], default=str)
    saida = capfd.readouterr()
    texto = "\n".join([*linhas, saida.out, saida.err])
    for segredo in (nova, bearer, pendente, *tokens):
        assert segredo not in diff
        assert segredo not in texto
    assert "sessions revoked" in "\n".join(linhas)


async def test_conta_da_tela_sem_senha_recebe_a_informada_e_o_link_do_email_morre(
    owner_engine, cliente, script, canal, tokens
):
    """An account created through the UI (`senha_hash` NULL, a 72 h first-password token in the
    e-mail) before the script existed: "password kept" with rc 0 was the outcome that answers 401
    at 2 a.m. The script sets the given password and burns the pending link, otherwise whoever
    owns the mailbox sets another one on top."""
    admin = await _admin(owner_engine)
    pid = await _pipeline("load_a", admin)
    async with transacao() as conn:
        criado = await usuarios.criar(
            conn,
            email=EMAIL,
            nome="svc",
            perfil="especialista",
            ator_id=admin,
            ator_perfil="admin",
        )
    assert (await _usuario(owner_engine))["senha_hash"] is None
    marco = await _marco(owner_engine)

    linhas = await script.aplicar(
        email=EMAIL, nome="svc", senha=SENHA, pipelines=["load_a"], ator=ADMIN
    )
    # Read BEFORE the login: the login writes `login/sessao`, which is not the script's.
    trilha = await _trilha_desde(owner_engine, marco)

    texto = "\n".join(linhas)
    assert "set" in texto and "kept" not in texto
    assert (
        await cliente.post("/auth/login", json={"email": EMAIL, "senha": SENHA})
    ).status_code == 200
    async with transacao() as conn:
        with pytest.raises(ErroAplicacao) as e:
            await recuperacao.confirmar(conn, criado.token_senha, "password-set-through-the-link")
    assert e.value.codigo == "token_invalido"
    assert [(t["acao"], t["recurso_tipo"]) for t in trilha] == [
        ("senha_trocada", "usuario"),
        ("criar", "permissao"),
    ]
    assert trilha[0]["depois"]["motivo"] == "primeira_senha_pendente"
    assert trilha[1]["recurso_id"] == pid
    diff = json.dumps([t["depois"] for t in trilha], default=str)
    for segredo in (SENHA, criado.token_senha, *tokens):
        assert segredo not in diff


async def test_conta_da_tela_sem_senha_e_sem_senha_informada_recusa_sem_gravar(
    owner_engine, script, canal
):
    admin = await _admin(owner_engine)
    await _pipeline("load_a", admin)
    async with transacao() as conn:
        await usuarios.criar(
            conn,
            email=EMAIL,
            nome="svc",
            perfil="especialista",
            ator_id=admin,
            ator_perfil="admin",
        )
    marco = await _marco(owner_engine)

    with pytest.raises(ErroAplicacao) as e:
        await script.aplicar(email=EMAIL, nome="svc", senha=None, pipelines=["load_a"], ator=ADMIN)

    assert e.value.codigo == "conta_sem_senha"
    assert EMAIL in e.value.mensagem
    assert await _trilha_desde(owner_engine, marco) == []
    assert await _permissoes_da_conta(owner_engine) == 0
    assert (await _usuario(owner_engine))["senha_hash"] is None


@pytest.mark.parametrize(
    ("montar", "codigo"),
    [
        ("ausente", "pipeline_ausente"),
        ("ambiguo", "pipeline_ambiguo"),
        ("sistema", "pipeline_sistema"),
    ],
)
async def test_pipeline_invalido_recusa_sem_gravar_nada(
    owner_engine, script, canal, montar, codigo
):
    admin = await _admin(owner_engine)
    await _pipeline("load_a", admin)
    if montar == "ambiguo":
        # `pipelines.nome` is not unique in the database: two with the same name really happen.
        await _pipeline("target", admin)
        await _pipeline("target", admin)
    elif montar == "sistema":
        pid = await _pipeline("target", admin)
        async with owner_engine.begin() as conn:
            await conn.execute(
                text("UPDATE pipelines SET sistema = true WHERE id = :p"), {"p": pid}
            )
    marco = await _marco(owner_engine)

    with pytest.raises(ErroAplicacao) as e:
        await script.aplicar(
            email=EMAIL, nome="svc", senha=SENHA, pipelines=["load_a", "target"], ator=ADMIN
        )

    assert e.value.codigo == codigo
    assert "target" in e.value.mensagem
    # All or nothing: neither the account, nor the valid pipeline's permission, nor any trail.
    assert await _usuario(owner_engine) is None
    assert await _trilha_desde(owner_engine, marco) == []


@pytest.mark.parametrize(
    ("kwargs", "codigo"),
    [
        ({"perfil": "admin"}, "perfil_recusado"),
        ({"perfil": "boss"}, "perfil_invalido"),
        ({"senha": "short"}, "senha_fraca"),
        ({"senha": None}, "senha_ausente"),
    ],
)
async def test_entrada_invalida_recusa_sem_gravar_nada(owner_engine, script, canal, kwargs, codigo):
    await _admin(owner_engine)
    marco = await _marco(owner_engine)
    args = {"email": EMAIL, "nome": "svc", "senha": SENHA, "ator": ADMIN, **kwargs}

    with pytest.raises(ErroAplicacao) as e:
        await script.aplicar(**args)

    assert e.value.codigo == codigo
    assert await _usuario(owner_engine) is None
    assert await _trilha_desde(owner_engine, marco) == []


@pytest.mark.parametrize("ator", [None, "   ", "ninguem@example.com", "operador"])
async def test_ator_obrigatorio_ao_gravar_e_tem_de_ser_admin_ativo(
    owner_engine, script, canal, ator
):
    """The trail is permanent: "the first admin by `criado_em`" would record that the bootstrap
    account created the service account. Missing, unknown or without the profile is a refusal,
    before writing."""
    await _admin(owner_engine)
    if ator == "operador":
        ator = "op@example.com"
        await semear_usuario(
            owner_engine, email=ator, senha="operator-password-1", perfil="operador"
        )
    marco = await _marco(owner_engine)

    with pytest.raises(ErroAplicacao) as e:
        await script.aplicar(email=EMAIL, nome="svc", senha=SENHA, ator=ator)

    assert e.value.codigo == "ator_invalido"
    assert "--actor" in e.value.mensagem
    assert await _usuario(owner_engine) is None
    assert await _trilha_desde(owner_engine, marco) == []


async def test_trilha_assina_o_ator_informado_e_nao_o_primeiro_admin(owner_engine, script, canal):
    await _admin(owner_engine)  # the bootstrap one: the first by `criado_em`
    quem_rodou = await _admin(owner_engine, "who.ran@example.com")
    marco = await _marco(owner_engine)

    await script.aplicar(email=EMAIL, nome="svc", senha=SENHA, ator="Who.Ran@example.com")

    trilha = await _trilha_desde(owner_engine, marco)
    assert trilha and all(t["ator_id"] == quem_rodou for t in trilha)


async def test_conta_existente_com_outro_perfil_nao_e_trocada(owner_engine, script, canal):
    await _admin(owner_engine)
    await semear_usuario(owner_engine, email=EMAIL, senha=SENHA, perfil="operador")
    marco = await _marco(owner_engine)

    with pytest.raises(ErroAplicacao) as e:
        await script.aplicar(email=EMAIL, nome="svc", senha=SENHA, ator=ADMIN)

    assert e.value.codigo == "conta_divergente"
    assert (await _usuario(owner_engine))["perfil"] == "operador"
    assert await _trilha_desde(owner_engine, marco) == []


async def test_simular_nao_grava_nada_e_dispensa_o_ator(owner_engine, script, canal):
    admin = await _admin(owner_engine)
    await _pipeline("load_a", admin)
    marco = await _marco(owner_engine)

    linhas = await script.aplicar(
        email=EMAIL, nome="svc", senha=SENHA, pipelines=["load_a"], simular=True
    )

    assert await _usuario(owner_engine) is None
    assert await _trilha_desde(owner_engine, marco) == []
    assert any("would create" in linha for linha in linhas)


async def test_cli_senha_por_arquivo_e_por_stdin_sem_vazar(
    owner_engine, cliente, script, canal, tokens, capfd, tmp_path, monkeypatch
):
    admin = await _admin(owner_engine)
    await _pipeline("load_a", admin)
    arq = tmp_path / "password.txt"
    arq.write_text(SENHA + "\n")  # a password file usually ends with a line break

    rc = await script.principal(
        [
            "--email",
            EMAIL,
            "--name",
            "svc",
            "--password-file",
            str(arq),
            "--execute",
            "load_a",
            "--actor",
            ADMIN,
        ]
    )

    assert rc == 0
    assert (
        await cliente.post("/auth/login", json={"email": EMAIL, "senha": SENHA})
    ).status_code == 200

    # stdin (non-tty): same account, now reset to another password read from standard input.
    nova = "password-read-from-standard-input"
    monkeypatch.setattr(sys, "stdin", io.StringIO(nova + "\n"))
    rc = await script.principal(
        [
            "--email",
            EMAIL,
            "--name",
            "svc",
            "--password-stdin",
            "--reset-password",
            "--actor",
            ADMIN,
        ]
    )
    assert rc == 0
    assert (
        await cliente.post("/auth/login", json={"email": EMAIL, "senha": nova})
    ).status_code == 200

    saida = capfd.readouterr()
    for segredo in (SENHA, nova, *tokens):
        assert segredo not in saida.out + saida.err
    assert "load_a" in saida.out  # prints what it did
    assert canal.enviados == []


async def test_cli_recusa_senha_por_argumento_sem_ecoar_o_valor(owner_engine, script, capfd):
    """A password as an argument leaks into `ps` and shell history, and argparse's own error
    message ("unrecognized arguments: --password X") would echo the value again. The refusal does
    not repeat the value."""
    await _admin(owner_engine)
    with pytest.raises(SystemExit) as e:
        await script.principal(["--email", EMAIL, "--name", "svc", "--password", SENHA])
    assert e.value.code == 2
    saida = capfd.readouterr()
    assert SENHA not in saida.out + saida.err
    assert "--password-file" in saida.err
    assert await _usuario(owner_engine) is None


async def test_cli_erro_devolve_1_com_mensagem_nomeada(
    owner_engine, script, canal, capfd, tmp_path
):
    await _admin(owner_engine)
    arq = tmp_path / "password.txt"
    arq.write_text(SENHA)
    base = ["--email", EMAIL, "--name", "svc", "--password-file", str(arq)]
    rc = await script.principal([*base, "--execute", "does_not_exist", "--actor", ADMIN])
    assert rc == 1
    assert "does_not_exist" in capfd.readouterr().err

    # Without --actor and without --dry-run: a named refusal, and nothing is created.
    rc = await script.principal(base)
    assert rc == 1
    assert "--actor" in capfd.readouterr().err
    assert await _usuario(owner_engine) is None


# --- `servico` profile: API token in a file, no password -----------------------------------------


async def _tokens_api(owner_engine) -> int:
    async with owner_engine.connect() as conn:
        return (await conn.execute(text("SELECT count(*) FROM tokens_api"))).scalar_one()


async def test_perfil_servico_entra_por_token_em_arquivo_0600_e_nao_autora(
    owner_engine, cliente, script, canal, tokens, capfd, caplog, tmp_path, monkeypatch
):
    """The `servico` path: no password, a token in a NEW 0600 file, and the account a specialist
    could not be: it triggers and follows, and does not author. It reaches EVERY pipeline, not
    only the requested one (the grant is still written but restricts nothing)."""
    caplog.set_level(logging.DEBUG)
    admin = await _admin(owner_engine)
    pid_a = await _pipeline("load_a", admin, publicar=True)
    pid_b = await _pipeline("load_b", admin, publicar=True)
    arq = tmp_path / "cflow-token"
    marco = await _marco(owner_engine)

    linhas = await script.aplicar(
        email=EMAIL,
        nome="svc",
        senha=None,
        perfil="servico",
        pipelines=["load_a"],
        token_arquivo=arq,
        token_nome="ci-adapter test",
        ator=ADMIN,
    )

    u = await _usuario(owner_engine)
    assert u is not None and u["perfil"] == "servico" and u["senha_hash"] is None
    trilha = await _trilha_desde(owner_engine, marco)
    assert [(t["acao"], t["recurso_tipo"]) for t in trilha] == [
        ("criar", "usuario"),
        ("criar", "token_api"),
        ("criar", "permissao"),
    ]
    assert all(t["ator_id"] == admin for t in trilha)
    assert tokens == []  # a service account is not born with a first-password link

    assert arq.stat().st_mode & 0o777 == 0o600
    token = arq.read_text(encoding="utf-8").strip()
    assert token.startswith("cfs_")
    hs = {"Authorization": f"Bearer {token}"}
    assert (await cliente.get("/auth/me", headers=hs)).status_code == 200

    disparos: list[uuid.UUID] = []

    async def _disparo_registrado(**kw):
        disparos.append(kw["pipeline_id"])
        return {"id": str(uuid.uuid4()), "status": "executando"}

    monkeypatch.setattr(rotas_pipelines, "_executar_dag", _disparo_registrado)
    r = await cliente.post(f"/pipelines/{pid_a}/executar-encadeado", headers=hs)
    assert r.status_code == 200, r.text
    # No grant on B, and it triggers anyway: full reach.
    r = await cliente.post(f"/pipelines/{pid_b}/executar-encadeado", headers=hs)
    assert r.status_code == 200, r.text
    assert disparos == [pid_a, pid_b]
    ctx = await execucao.criar_execucao(pipeline_id=pid_a, ator_id=admin)
    assert (await cliente.get(f"/execucoes/{ctx['execucao_id']}", headers=hs)).status_code == 200
    # The specialist's limit, closed: authoring gives 403.
    r = await cliente.post(
        "/pipelines", json={"nome": "created_by_the_service_account"}, headers=hs
    )
    assert r.status_code == 403, r.text

    # The token does not leak: not in the report, stdout/stderr, the log or the trail.
    diff = json.dumps([(t["antes"], t["depois"]) for t in trilha], default=str)
    saida = capfd.readouterr()
    texto = "\n".join([*linhas, saida.out, saida.err, caplog.text])
    assert token not in texto and token not in diff
    assert token[:10] in "\n".join(linhas)  # the PREFIX appears, to say which token it is
    assert canal.enviados == []

    # Running again without --token-file is idempotent: nothing in the trail.
    marco = await _marco(owner_engine)
    linhas = await script.aplicar(
        email=EMAIL,
        nome="svc",
        senha=None,
        perfil="servico",
        pipelines=["load_a"],
        ator=ADMIN,
    )
    assert await _trilha_desde(owner_engine, marco) == []
    assert "exists" in "\n".join(linhas) and "existed" in "\n".join(linhas)


@pytest.mark.parametrize(
    ("caso", "codigo"),
    [
        ("senha_em_servico", "senha_nao_se_aplica"),
        ("redefinir_em_servico", "senha_nao_se_aplica"),
        ("token_em_especialista", "token_so_para_servico"),
        ("arquivo_existe", "token_arquivo_existe"),
        ("pasta_ausente", "token_arquivo_invalido"),
        ("validade_zero", "validade_invalida"),
        ("nome_vazio", "nome_invalido"),
    ],
)
async def test_perfil_servico_entrada_invalida_recusa_sem_gravar(
    owner_engine, script, canal, tmp_path, caso, codigo
):
    await _admin(owner_engine)
    arq = tmp_path / "cflow-token"
    args = {
        "email": EMAIL,
        "nome": "svc",
        "senha": None,
        "perfil": "servico",
        "token_arquivo": arq,
        "ator": ADMIN,
    }
    if caso == "senha_em_servico":
        args["senha"] = SENHA
    elif caso == "redefinir_em_servico":
        args["redefinir_senha"] = True
    elif caso == "token_em_especialista":
        args.update(perfil="especialista", senha=SENHA)
    elif caso == "arquivo_existe":
        arq.write_text("what was already here\n", encoding="utf-8")
    elif caso == "pasta_ausente":
        args["token_arquivo"] = tmp_path / "does-not-exist" / "cflow-token"
    elif caso == "validade_zero":
        args["token_dias"] = 0
    else:
        args["token_nome"] = "   "
    marco = await _marco(owner_engine)

    with pytest.raises(ErroAplicacao) as e:
        await script.aplicar(**args)

    assert e.value.codigo == codigo
    assert await _usuario(owner_engine) is None
    assert await _trilha_desde(owner_engine, marco) == []
    assert await _tokens_api(owner_engine) == 0
    if caso == "arquivo_existe":
        assert arq.read_text(encoding="utf-8") == "what was already here\n"  # not even overwritten
    else:
        assert not arq.exists()


async def test_perfil_servico_falha_no_meio_desfaz_conta_token_e_arquivo(
    owner_engine, script, canal, tmp_path, monkeypatch
):
    """All or nothing applies to the file: the permission fails AFTER the token was written, the
    transaction undoes the account and the token, and the file (holding a token that no longer
    exists) disappears."""
    admin = await _admin(owner_engine)
    await _pipeline("load_a", admin)
    arq = tmp_path / "cflow-token"
    gravou: list[bool] = []
    original = script._gravar_token

    def _espiao(caminho, token, escritos):
        original(caminho, token, escritos)
        gravou.append(caminho.exists())

    async def _quebra(*_a, **_kw):
        raise RuntimeError("simulated failure while granting")

    monkeypatch.setattr(script, "_gravar_token", _espiao)
    monkeypatch.setattr(permissoes, "conceder", _quebra)
    with pytest.raises(RuntimeError):
        await script.aplicar(
            email=EMAIL,
            nome="svc",
            senha=None,
            perfil="servico",
            pipelines=["load_a"],
            token_arquivo=arq,
            ator=ADMIN,
        )

    assert gravou == [True]  # the file did exist: it is the cleanup that is proven here
    assert not arq.exists()
    assert await _usuario(owner_engine) is None
    assert await _tokens_api(owner_engine) == 0


async def test_cli_perfil_servico_grava_o_token_no_arquivo_e_nao_no_stdout(
    owner_engine, cliente, script, canal, capfd, tmp_path
):
    admin = await _admin(owner_engine)
    await _pipeline("load_a", admin)
    arq = tmp_path / "cflow-token"
    base = [
        "--email",
        EMAIL,
        "--name",
        "svc",
        "--profile",
        "servico",
        "--token-file",
        str(arq),
        "--token-days",
        "30",
        "--execute",
        "load_a",
    ]

    assert await script.principal([*base, "--dry-run"]) == 0
    assert not arq.exists()
    assert "would issue" in capfd.readouterr().out

    assert await script.principal([*base, "--actor", ADMIN]) == 0
    assert arq.stat().st_mode & 0o777 == 0o600
    token = arq.read_text(encoding="utf-8").strip()
    eu = await cliente.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert eu.status_code == 200
    saida = capfd.readouterr()
    assert token not in saida.out + saida.err
    assert "issued" in saida.out and "load_a" in saida.out

    # Again with the same path: refused before writing, with no other token and no replaced file.
    assert await script.principal([*base, "--actor", ADMIN]) == 1
    assert "token_arquivo_existe" in capfd.readouterr().err
    assert arq.read_text(encoding="utf-8").strip() == token
    assert await _tokens_api(owner_engine) == 1

"""`email` effect node against a local SMTP server (thread, 127.0.0.1:0).

Covers: sending in both modes (unico/por_linha), attachment BUILT from the previous node (CSV with
header) and READY-MADE attachment coming in a column, placeholder templates, empty guard, failure
tolerance, the `escrita_externa` audit trail (no body/attachment/password), dry-run preview (no
SMTP connection opened) and the authorization edge (connection without `permitir_escrita`).

The server is a real minimal SMTP (it speaks the protocol on the socket), not a smtplib mock: that
is what proves the assembled MIME is actually sent over the socket. STARTTLS is not exercised here (it would need a
certificate); production uses the connection's `seguranca` setting for TLS.
"""

from __future__ import annotations

import base64
import json
import socketserver
import threading
import uuid

import pytest
from sqlalchemy import text

from cflow_core.execucao import engine

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration

SENHA_SMTP = "smtp-secret-password"
USUARIO_SMTP = "no-reply@example.com"
DESTINO = "recipient@example.com"


class _EstadoSmtp:
    def __init__(self) -> None:
        self.mensagens: list[dict] = []  # {de, para, dados}
        self.conexoes = 0
        self.auth: list[str] = []
        self.recusar: set[str] = set()  # addresses the server refuses (RCPT 550)
        self.lock = threading.Lock()


def _handler(estado: _EstadoSmtp):
    class _H(socketserver.StreamRequestHandler):
        def _dizer(self, texto: str) -> None:
            self.wfile.write(texto.encode() + b"\r\n")
            self.wfile.flush()

        def handle(self) -> None:  # noqa: C901 — SMTP state machine, linear on purpose
            with estado.lock:
                estado.conexoes += 1
            self._dizer("220 local-test ESMTP")
            de, para = "", []
            while True:
                linha = self.rfile.readline()
                if not linha:
                    return
                cmd = linha.decode("utf-8", "replace").strip()
                up = cmd.upper()
                if up.startswith(("EHLO", "HELO")):
                    self._dizer("250-local-test")
                    self._dizer("250 AUTH PLAIN")
                elif up.startswith("AUTH PLAIN"):
                    with estado.lock:
                        estado.auth.append(cmd.split(" ", 2)[-1])
                    self._dizer("235 2.7.0 authenticated")
                elif up.startswith("MAIL FROM"):
                    de = cmd.split("<", 1)[-1].rstrip(">") if "<" in cmd else ""
                    self._dizer("250 OK")
                elif up.startswith("RCPT TO"):
                    endereco = cmd.split("<", 1)[-1].rstrip(">") if "<" in cmd else ""
                    if endereco in estado.recusar:
                        self._dizer("550 mailbox does not exist")
                    else:
                        para.append(endereco)
                        self._dizer("250 OK")
                elif up == "DATA":
                    self._dizer("354 go ahead")
                    corpo = []
                    while True:
                        parte = self.rfile.readline()
                        if not parte or parte.strip() == b".":
                            break
                        corpo.append(parte)
                    with estado.lock:
                        # RAW bytes: the stdlib parser decodes the payload by the MIME charset.
                        # Decoding earlier (str) would corrupt accents in the TEST, a harness bug
                        # rather than a product bug (the bytes on the socket are correct UTF-8).
                        estado.mensagens.append(
                            {"de": de, "para": list(para), "dados": b"".join(corpo)}
                        )
                    de, para = "", []
                    self._dizer("250 OK: accepted")
                elif up == "QUIT":
                    self._dizer("221 bye")
                    return
                elif up in ("NOOP", "RSET"):
                    self._dizer("250 OK")
                else:
                    self._dizer("250 OK")

    return _H


class _Servidor(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@pytest.fixture
def smtp():
    estado = _EstadoSmtp()
    srv = _Servidor(("127.0.0.1", 0), _handler(estado))
    porta = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield porta, estado
    srv.shutdown()
    srv.server_close()


async def _conexao_smtp(owner_engine, cliente, h, nome, porta, *, flag=True, tipo="smtp"):
    config = {
        "host": "127.0.0.1",
        "porta": porta,
        "seguranca": "nenhuma",
        "usuario": USUARIO_SMTP,
        "remetente": USUARIO_SMTP,
        "remetente_nome": "Acme",
        "tentativas": 1,
        "espera_tentativa_segundos": 0,
    }
    if tipo != "smtp":  # connection of ANOTHER type, to prove refusal on type mismatch
        config = {"url": f"http://127.0.0.1:{porta}"}
    r = await cliente.post(
        "/conexoes",
        json={"nome": nome, "tipo": tipo, "config": config, "segredo": SENHA_SMTP},
        headers=h,
    )
    assert r.status_code in (200, 201), r.text
    cid = r.json()["id"]
    if flag:
        async with owner_engine.begin() as conn:
            await conn.execute(
                text("UPDATE conexoes SET permitir_escrita = true WHERE id = CAST(:i AS uuid)"),
                {"i": cid},
            )
    return cid


async def _pipeline_email(
    cliente, h, cid, cfg_email, *, fonte_sql=None, com_fonte=True, publicar=True, parametros=None
):
    nos, arestas = [], []
    if com_fonte:
        nos.append(
            {
                "id": "src",
                "tipo": "sql",
                "sql": fonte_sql
                or "SELECT * FROM (VALUES ('ALPHA', 1234.56),('BETA', 78.90)) t(projeto, valor)",
            }
        )
        arestas.append({"de": "src", "para": "mail"})
    nos.append({"id": "mail", "tipo": "email", "config": {"conexao_id": cid, **cfg_email}})
    definicao: dict = {"nos": nos, "arestas": arestas}
    if parametros:
        definicao["parametros"] = parametros
    pid = (
        await cliente.post("/pipelines", json={"nome": f"E-mail {uuid.uuid4().hex[:6]}"}, headers=h)
    ).json()["id"]
    r = await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    assert r.status_code in (200, 204), r.text
    if publicar:
        pub = await cliente.post(
            f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
        )
        assert pub.status_code in (200, 201), pub.text
    return pid, definicao


async def _rodar(pid, uid, parametros=None):
    det = await engine.criar_execucao(
        pipeline_id=uuid.UUID(pid), ator_id=uid, parametros=parametros
    )
    eid = det["execucao_id"]
    res = {}
    for nid in det["ordem"]:
        res[nid] = await engine.executar_no(
            execucao_id=uuid.UUID(eid),
            definicao=det["definicao"],
            no_id=nid,
            parametros=det["parametros"],
        )
    return eid, res


def _ler_snapshot(res_no) -> tuple[list[str], list[dict]]:
    import duckdb

    d = duckdb.connect()
    cur = d.execute(f"SELECT * FROM read_parquet('{res_no['snapshot']}')")
    cols = [c[0] for c in cur.description]
    linhas = [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
    d.close()
    return cols, linhas


def _mime(dados: bytes):
    import email as email_lib
    from email import policy

    return email_lib.message_from_bytes(dados, policy=policy.default)


def _anexos_do_mime(dados: bytes) -> dict[str, bytes]:
    """Extract `{name: content}` from the attachments of a raw message (stdlib parser)."""
    return {p.get_filename(): p.get_payload(decode=True) for p in _mime(dados).iter_attachments()}


def _corpo_do_mime(dados: bytes) -> str:
    corpo = _mime(dados).get_body(preferencelist=("html", "plain"))
    return corpo.get_content() if corpo else ""


# ---------------- sending ----------------


async def test_email_unico_com_anexo_montado(owner_engine, app_engine, cliente, smtp):
    porta, estado = smtp
    uid, h = await usuario_logado(
        owner_engine, cliente, email="mail_un@example.com", perfil="especialista"
    )
    cid = await _conexao_smtp(owner_engine, cliente, h, "smtp-un", porta)
    pid, _ = await _pipeline_email(
        cliente,
        h,
        cid,
        {
            "modo": "unico",
            "para": DESTINO,
            "assunto": "Report {{param.competencia}} - {{contagem}} projects",
            "corpo": "<p>Attached.</p>{{tabela}}",
            "anexos": [
                {"origem": "dados", "formato": "csv", "nome": "projects_{{param.competencia}}.csv"}
            ],
        },
        parametros=[{"nome": "competencia", "tipo": "texto"}],
    )
    _, res = await _rodar(pid, uid, parametros={"competencia": "2026-07"})

    assert len(estado.mensagens) == 1
    msg = estado.mensagens[0]
    assert msg["para"] == [DESTINO] and msg["de"] == USUARIO_SMTP
    assert _mime(msg["dados"])["Subject"].startswith("Report")  # decoded subject
    corpo = _corpo_do_mime(msg["dados"])
    assert "ALPHA" in corpo and "<table" in corpo  # {{tabela}} became an HTML table
    anexos = _anexos_do_mime(msg["dados"])
    assert list(anexos) == ["projects_2026-07.csv"]
    csv_texto = anexos["projects_2026-07.csv"].decode("utf-8-sig")
    assert csv_texto.splitlines()[0] == "projeto;valor"  # header
    assert "ALPHA;1234.56" in csv_texto

    cols, linhas = _ler_snapshot(res["mail"])
    assert cols == [
        "destinatarios",
        "assunto",
        "anexos",
        "bytes_anexos",
        "status",
        "duracao_ms",
        "erro",
    ]
    assert len(linhas) == 1 and linhas[0]["status"] == "ok" and linhas[0]["erro"] is None
    assert linhas[0]["destinatarios"] == DESTINO and int(linhas[0]["anexos"]) == 1


async def test_email_por_linha_usa_coluna_no_destinatario(owner_engine, app_engine, cliente, smtp):
    porta, estado = smtp
    uid, h = await usuario_logado(
        owner_engine, cliente, email="mail_pl@example.com", perfil="especialista"
    )
    cid = await _conexao_smtp(owner_engine, cliente, h, "smtp-pl", porta)
    fonte = (
        "SELECT * FROM (VALUES ('a@example.com','Project A'),"
        "('b@example.com','Project B')) t(email, projeto)"
    )
    pid, _ = await _pipeline_email(
        cliente,
        h,
        cid,
        {
            "modo": "por_linha",
            "para": "{{email}}",
            "assunto": "{{projeto}}",
            "corpo": "<p>Hello, {{projeto}}</p>",
        },
        fonte_sql=fonte,
    )
    _, res = await _rodar(pid, uid)
    assert [m["para"] for m in estado.mensagens] == [["a@example.com"], ["b@example.com"]]
    _, linhas = _ler_snapshot(res["mail"])
    assert [lin["assunto"] for lin in linhas] == ["Project A", "Project B"]
    assert all(lin["status"] == "ok" for lin in linhas)


async def test_email_anexo_pronto_vem_de_coluna(owner_engine, app_engine, cliente, smtp):
    """The previous node ALREADY built the file (content in a column); this node only attaches it."""
    porta, estado = smtp
    uid, h = await usuario_logado(
        owner_engine, cliente, email="mail_ax@example.com", perfil="especialista"
    )
    cid = await _conexao_smtp(owner_engine, cliente, h, "smtp-ax", porta)
    conteudo = b"linha1;linha2\r\nvalor;outro\r\n"
    b64 = base64.b64encode(conteudo).decode()
    fonte = f"SELECT 'pronto.csv' AS arquivo_nome, '{b64}' AS arquivo_conteudo"
    pid, _ = await _pipeline_email(
        cliente,
        h,
        cid,
        {
            "modo": "unico",
            "para": DESTINO,
            "assunto": "Ready file",
            "corpo": "<p>Attached is the file the previous node built.</p>",
            "anexos": [
                {
                    "origem": "coluna",
                    "coluna_nome": "arquivo_nome",
                    "coluna_conteudo": "arquivo_conteudo",
                    "codificacao_conteudo": "base64",
                }
            ],
        },
        fonte_sql=fonte,
    )
    await _rodar(pid, uid)
    anexos = _anexos_do_mime(estado.mensagens[0]["dados"])
    assert anexos == {"pronto.csv": conteudo}


async def test_email_corpo_texto_puro(owner_engine, app_engine, cliente, smtp):
    porta, estado = smtp
    uid, h = await usuario_logado(
        owner_engine, cliente, email="mail_tx@example.com", perfil="especialista"
    )
    cid = await _conexao_smtp(owner_engine, cliente, h, "smtp-tx", porta)
    pid, _ = await _pipeline_email(
        cliente,
        h,
        cid,
        {
            "modo": "unico",
            "para": DESTINO,
            "assunto": "Text",
            "corpo_tipo": "texto",
            "corpo": "Count: {{contagem}}\n{{tabela}}",
            "anexos": [],
        },
    )
    await _rodar(pid, uid)
    corpo = _corpo_do_mime(estado.mensagens[0]["dados"])
    assert corpo.startswith("Count: 2") and "projeto\tvalor" in corpo  # TAB table, no HTML
    assert "<table" not in corpo


# ---------------- guards ----------------


async def test_email_zero_linhas_falha(owner_engine, app_engine, cliente, smtp):
    porta, estado = smtp
    uid, h = await usuario_logado(
        owner_engine, cliente, email="mail_z0@example.com", perfil="especialista"
    )
    cid = await _conexao_smtp(owner_engine, cliente, h, "smtp-z0", porta)
    pid, _ = await _pipeline_email(
        cliente,
        h,
        cid,
        {"modo": "unico", "para": DESTINO, "assunto": "Empty", "corpo": "<p>{{contagem}}</p>"},
        fonte_sql="SELECT 'x' AS projeto WHERE 1=0",
    )
    with pytest.raises(Exception) as exc:  # noqa: PT011 — wrapped ErroAplicacao
        await _rodar(pid, uid)
    assert "0 input rows" in getattr(exc.value, "mensagem", str(exc.value))
    assert estado.mensagens == []  # did not send an empty report


async def test_email_zero_linhas_permitido(owner_engine, app_engine, cliente, smtp):
    porta, estado = smtp
    uid, h = await usuario_logado(
        owner_engine, cliente, email="mail_zp@example.com", perfil="especialista"
    )
    cid = await _conexao_smtp(owner_engine, cliente, h, "smtp-zp", porta)
    pid, _ = await _pipeline_email(
        cliente,
        h,
        cid,
        {
            "modo": "unico",
            "para": DESTINO,
            "assunto": "Nothing to report",
            "corpo": "<p>{{contagem}} rows</p>",
            "permitir_vazio": True,
        },
        fonte_sql="SELECT 'x' AS projeto WHERE 1=0",
    )
    await _rodar(pid, uid)
    assert len(estado.mensagens) == 1
    assert "0 rows" in _corpo_do_mime(estado.mensagens[0]["dados"])


async def test_email_destinatario_recusado_aborta(owner_engine, app_engine, cliente, smtp):
    porta, estado = smtp
    estado.recusar.add("naoexiste@example.com")
    uid, h = await usuario_logado(
        owner_engine, cliente, email="mail_rc@example.com", perfil="especialista"
    )
    cid = await _conexao_smtp(owner_engine, cliente, h, "smtp-rc", porta)
    fonte = "SELECT * FROM (VALUES ('naoexiste@example.com'),('ok@example.com')) t(email)"
    pid, _ = await _pipeline_email(
        cliente,
        h,
        cid,
        {"modo": "por_linha", "para": "{{email}}", "assunto": "x", "corpo": "<p>x</p>"},
        fonte_sql=fonte,
    )
    with pytest.raises(Exception) as exc:  # noqa: PT011
        await _rodar(pid, uid)
    msg = getattr(exc.value, "mensagem", str(exc.value))
    assert "refused" in msg and "partial effect" in msg
    assert estado.mensagens == []  # the 1st failed at RCPT; the 2nd was never attempted


async def test_email_tolerancia_continuar_registra_falha(owner_engine, app_engine, cliente, smtp):
    porta, estado = smtp
    estado.recusar.add("naoexiste@example.com")
    uid, h = await usuario_logado(
        owner_engine, cliente, email="mail_ct@example.com", perfil="especialista"
    )
    cid = await _conexao_smtp(owner_engine, cliente, h, "smtp-ct", porta)
    fonte = "SELECT * FROM (VALUES ('naoexiste@example.com'),('ok@example.com')) t(email)"
    pid, _ = await _pipeline_email(
        cliente,
        h,
        cid,
        {
            "modo": "por_linha",
            "para": "{{email}}",
            "assunto": "x",
            "corpo": "<p>x</p>",
            "tolerancia_falha": "continuar",
        },
        fonte_sql=fonte,
    )
    _, res = await _rodar(pid, uid)
    _, linhas = _ler_snapshot(res["mail"])
    por_destino = {lin["destinatarios"]: lin for lin in linhas}
    assert por_destino["naoexiste@example.com"]["status"] == "falha"
    assert por_destino["ok@example.com"]["status"] == "ok"
    assert [m["para"] for m in estado.mensagens] == [["ok@example.com"]]


async def test_email_conexao_sem_escrita_nega(owner_engine, app_engine, cliente, smtp):
    porta, estado = smtp
    uid, h = await usuario_logado(
        owner_engine, cliente, email="mail_fl@example.com", perfil="especialista"
    )
    cid = await _conexao_smtp(owner_engine, cliente, h, "smtp-fl", porta, flag=False)
    # `validar_conexoes_efeito` blocks at publish time (422)
    pid, definicao = await _pipeline_email(
        cliente,
        h,
        cid,
        {"modo": "unico", "para": DESTINO, "assunto": "x", "corpo": "<p>x</p>"},
        publicar=False,
    )
    r = await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    assert r.status_code == 422, r.text
    assert "permitir_escrita" in json.dumps(r.json(), ensure_ascii=False)
    assert estado.mensagens == []


async def test_email_conexao_tipo_incompativel_bloqueia_publicar(
    owner_engine, app_engine, cliente, smtp
):
    porta, _ = smtp
    uid, h = await usuario_logado(
        owner_engine, cliente, email="mail_ti@example.com", perfil="especialista"
    )
    cid = await _conexao_smtp(owner_engine, cliente, h, "smtp-ti", porta, tipo="api_rest")
    pid, _ = await _pipeline_email(
        cliente,
        h,
        cid,
        {"modo": "unico", "para": DESTINO, "assunto": "x", "corpo": "<p>x</p>"},
        publicar=False,
    )
    r = await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    assert r.status_code == 422, r.text
    assert "smtp" in json.dumps(r.json(), ensure_ascii=False)


async def test_email_parametro_segredo_no_corpo_recusado(owner_engine, app_engine, cliente, smtp):
    porta, _ = smtp
    uid, h = await usuario_logado(
        owner_engine, cliente, email="mail_sg@example.com", perfil="especialista"
    )
    cid = await _conexao_smtp(owner_engine, cliente, h, "smtp-sg", porta)
    definicao = {
        "parametros": [{"nome": "senha_api", "tipo": "texto", "segredo": True}],
        "nos": [
            {
                "id": "mail",
                "tipo": "email",
                "config": {
                    "conexao_id": cid,
                    "modo": "unico",
                    "para": DESTINO,
                    "assunto": "x",
                    "corpo": "<p>password: {{param.senha_api}}</p>",
                },
            }
        ],
        "arestas": [],
    }
    pid = (await cliente.post("/pipelines", json={"nome": "Secret in body"}, headers=h)).json()[
        "id"
    ]
    # Structural validation runs already when the draft is saved: the refusal does not wait for
    # publish (a credential in an e-mail body is a definition error, not a connection-state one).
    r = await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    assert r.status_code == 422, r.text
    assert "senha_api" in json.dumps(r.json(), ensure_ascii=False)


# ---------------- audit trail + secret ----------------


async def test_email_trilha_registra_sem_corpo_nem_senha(owner_engine, app_engine, cliente, smtp):
    porta, _ = smtp
    uid, h = await usuario_logado(
        owner_engine, cliente, email="mail_au@example.com", perfil="especialista"
    )
    cid = await _conexao_smtp(owner_engine, cliente, h, "smtp-au", porta)
    pid, definicao = await _pipeline_email(
        cliente,
        h,
        cid,
        {
            "modo": "unico",
            "para": DESTINO,
            "assunto": "Audit",
            "corpo": "<p>body-that-must-not-reach-the-trail</p>",
            "anexos": [{"origem": "dados", "formato": "csv", "nome": "a.csv"}],
        },
    )
    eid, _ = await _rodar(pid, uid)
    async with owner_engine.begin() as conn:
        aud = (
            (
                await conn.execute(
                    text(
                        "SELECT depois FROM auditoria WHERE recurso_tipo = 'escrita_externa' "
                        "ORDER BY id DESC LIMIT 1"
                    )
                )
            )
            .mappings()
            .first()
        )
        erros = (
            await conn.execute(
                text(
                    "SELECT coalesce(string_agg(coalesce(erro,''), '|'), '') FROM execucao_nos "
                    "WHERE execucao_id = CAST(:e AS uuid)"
                ),
                {"e": eid},
            )
        ).scalar_one()
    assert aud is not None
    dep = aud["depois"]
    texto_trilha = json.dumps(dep, ensure_ascii=False)
    assert dep["canal"] == "email" and dep["enviados"] == 1 and dep["falhas"] == 0
    assert dep["destinatarios"] == [DESTINO] and dep["assuntos"] == ["Audit"] and dep["anexos"] == 1
    assert "corpo-que-nao-deve-ir-pra-trilha" not in texto_trilha
    assert SENHA_SMTP not in texto_trilha
    assert SENHA_SMTP not in json.dumps(definicao) and SENHA_SMTP not in (erros or "")


async def test_email_senha_nao_volta_na_api(owner_engine, app_engine, cliente, smtp):
    porta, _ = smtp
    _, h = await usuario_logado(
        owner_engine, cliente, email="mail_ap@example.com", perfil="especialista"
    )
    await _conexao_smtp(owner_engine, cliente, h, "smtp-ap", porta)
    r = await cliente.get("/conexoes", headers=h)
    assert r.status_code == 200
    assert SENHA_SMTP not in r.text and "credencial_ref" not in r.text


async def test_conexao_smtp_testar_nao_envia(owner_engine, app_engine, cliente, smtp):
    porta, estado = smtp
    _, h = await usuario_logado(
        owner_engine, cliente, email="mail_ts@example.com", perfil="especialista"
    )
    cid = await _conexao_smtp(owner_engine, cliente, h, "smtp-ts", porta)
    r = await cliente.post(f"/conexoes/{cid}/testar", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert estado.conexoes == 1 and estado.mensagens == []  # connected, authenticated, did NOT send
    assert estado.auth, "should have authenticated with the vault credential"
    assert base64.b64decode(estado.auth[0]).decode().endswith(SENHA_SMTP)


async def test_conexao_smtp_config_invalida_422(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="mail_cf@example.com", perfil="especialista"
    )
    r = await cliente.post(
        "/conexoes",
        json={
            "nome": "smtp-ruim",
            "tipo": "smtp",
            "config": {"host": "smtp://servidor:587", "porta": 99999, "seguranca": "magia"},
            "segredo": "x",
        },
        headers=h,
    )
    assert r.status_code == 422, r.text
    corpo = json.dumps(r.json(), ensure_ascii=False)
    assert "host" in corpo and "port" in corpo and "security" in corpo


# ---------------- dry-run preview ----------------


async def test_email_preview_dry_run_nao_conecta(owner_engine, app_engine, cliente, smtp):
    porta, estado = smtp
    uid, h = await usuario_logado(
        owner_engine, cliente, email="mail_pv@example.com", perfil="especialista"
    )
    cid = await _conexao_smtp(owner_engine, cliente, h, "smtp-pv", porta)
    pid, definicao = await _pipeline_email(
        cliente,
        h,
        cid,
        {
            "modo": "unico",
            "para": DESTINO,
            "assunto": "Preview {{contagem}}",
            "corpo": "<p>{{tabela}}</p><p>{{inexistente}}</p>",
            "anexos": [{"origem": "dados", "formato": "csv", "nome": "preview.csv"}],
        },
    )
    r = await cliente.post(
        f"/pipelines/{pid}/nos/mail/preview", json={"definicao": definicao}, headers=h
    )
    assert r.status_code == 200, r.text
    assert estado.conexoes == 0 and estado.mensagens == []  # dry-run does NOT open an SMTP socket
    efeito = r.json()["efeito"]
    assert efeito["tipo"] == "email" and efeito["modo"] == "unico"
    assert efeito["destino"] == DESTINO and efeito["assunto"] == "Preview 2"
    assert efeito["servidor"] == "127.0.0.1" and efeito["remetente"] == USUARIO_SMTP
    assert efeito["anexos"][0]["nome"] == "preview.csv" and efeito["anexos"][0]["bytes"] > 0
    assert efeito["nao_resolvidos"] == ["inexistente"]  # warns instead of sending a hole
    assert "Dry-run" in efeito["aviso"]
    # the simulated summary has the SAME schema as the execution snapshot (dry-run does not lie about shape)
    assert r.json()["colunas"] == [
        "destinatarios",
        "assunto",
        "anexos",
        "bytes_anexos",
        "status",
        "duracao_ms",
        "erro",
    ]
    assert r.json()["linhas"][0]["status"] == "simulado"
    # and the dry-run body/attachment is the SAME the execution sends (built by the same code)
    _, res = await _rodar(pid, uid)
    anexos = _anexos_do_mime(estado.mensagens[0]["dados"])
    assert len(anexos["preview.csv"]) == efeito["anexos"][0]["bytes"]
    assert _corpo_do_mime(estado.mensagens[0]["dados"]).strip() == efeito["corpo"].strip()
    assert _ler_snapshot(res["mail"])[1][0]["status"] == "ok"


# ---------------- http chain (per row) -> e-mail (per row) ----------------


async def test_http_por_linha_extrai_valor_e_manda_um_email_por_linha(
    owner_engine, app_engine, cliente, smtp
):
    """One request per row against an external API, the returned value becomes a column and each
    row triggers its own e-mail carrying that value."""
    import threading
    from http.server import ThreadingHTTPServer

    from .test_no_http import _Estado, _handler

    porta_smtp, estado_smtp = smtp
    estado_http = _Estado()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(estado_http, atraso=0.0))
    porta_http = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        uid, h = await usuario_logado(
            owner_engine, cliente, email="mail_ch@example.com", perfil="especialista"
        )
        cid_smtp = await _conexao_smtp(owner_engine, cliente, h, "smtp-ch", porta_smtp)
        cid_http = (
            await cliente.post(
                "/conexoes",
                json={
                    "nome": "api-ch",
                    "tipo": "api_rest",
                    "config": {"url": f"http://127.0.0.1:{porta_http}", "auth_tipo": "bearer"},
                    "segredo": "tok",
                },
                headers=h,
            )
        ).json()["id"]
        async with owner_engine.begin() as conn:
            await conn.execute(
                text("UPDATE conexoes SET permitir_escrita = true WHERE id = CAST(:i AS uuid)"),
                {"i": cid_http},
            )

        definicao = {
            "nos": [
                {
                    "id": "parcelas",
                    "tipo": "sql",
                    "sql": (
                        "SELECT * FROM (VALUES "
                        "('a@example.com','P-1'),('b@example.com','P-2')) t(email, pedido)"
                    ),
                },
                {
                    "id": "charge",
                    "tipo": "http",
                    "config": {
                        "conexao_id": cid_http,
                        "metodo": "POST",
                        "caminho": "/ok",
                        "modo": "por_linha",
                        "concorrencia": 1,  # one request at a time
                        "corpo_tipo": "colunas",
                        # the `id` the server returns becomes a COLUMN of this node
                        "resposta_campos": [{"de": "id", "para": "recibo"}],
                        "incluir_resposta_completa": False,
                    },
                },
                {
                    "id": "avisar",
                    "tipo": "email",
                    "config": {
                        "conexao_id": cid_smtp,
                        "modo": "por_linha",
                        "para": "{{email}}",
                        "assunto": "Payment of order {{pedido}}: receipt {{recibo}}",
                        "corpo": "<p>Status {{_status}} · receipt {{recibo}}</p>",
                    },
                },
            ],
            "arestas": [
                {"de": "parcelas", "para": "charge"},
                {"de": "charge", "para": "avisar"},
            ],
        }
        pid = (
            await cliente.post("/pipelines", json={"nome": "Charge and notify"}, headers=h)
        ).json()["id"]
        r = await cliente.put(
            f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
        )
        assert r.status_code in (200, 204), r.text
        pub = await cliente.post(
            f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
        )
        assert pub.status_code in (200, 201), pub.text

        _, res = await _rodar(pid, uid)

        # 2 rows -> 2 requests, one at a time
        assert len(estado_http.requisicoes) == 2
        assert estado_http.max_simultaneos == 1
        cols, linhas = _ler_snapshot(res["charge"])
        # extracted value present; full response discarded
        assert "recibo" in cols and "_resposta" not in cols
        recibos = sorted(lin["recibo"] for lin in linhas)
        assert recibos == ["1", "2"]  # the test server returns {"id": n}

        # ...and one e-mail per row, each with ITS OWN receipt
        assert len(estado_smtp.mensagens) == 2
        assert sorted(m["para"][0] for m in estado_smtp.mensagens) == [
            "a@example.com",
            "b@example.com",
        ]
        corpos = [_corpo_do_mime(m["dados"]) for m in estado_smtp.mensagens]
        assert sorted(c.strip() for c in corpos) == [
            "<p>Status 200 · receipt 1</p>",
            "<p>Status 200 · receipt 2</p>",
        ]
        _, resumo = _ler_snapshot(res["avisar"])
        assert [lin["status"] for lin in resumo] == ["ok", "ok"]
        assert sorted(lin["assunto"] for lin in resumo) == [
            "Payment of order P-1: receipt 1",
            "Payment of order P-2: receipt 2",
        ]
    finally:
        srv.shutdown()
        srv.server_close()


async def test_http_por_linha_consolidado_em_no_a_frente(owner_engine, app_engine, cliente, smtp):
    """Many rows running one by one and, at the end, ONE consolidated result: the http node snapshot
    carries the INPUT columns + the values extracted from the response in the same table, so a SQL
    node downstream consolidates everything (here: 3 rows, one of them failing) and a single e-mail
    carries the summary."""
    import threading
    from http.server import ThreadingHTTPServer

    from .test_no_http import _Estado, _handler

    porta_smtp, estado_smtp = smtp
    estado_http = _Estado()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(estado_http, atraso=0.0))
    porta_http = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        uid, h = await usuario_logado(
            owner_engine, cliente, email="mail_cs@example.com", perfil="especialista"
        )
        cid_smtp = await _conexao_smtp(owner_engine, cliente, h, "smtp-cs", porta_smtp)
        cid_http = (
            await cliente.post(
                "/conexoes",
                json={
                    "nome": "api-cs",
                    "tipo": "api_rest",
                    "config": {"url": f"http://127.0.0.1:{porta_http}", "auth_tipo": "bearer"},
                    "segredo": "tok",
                },
                headers=h,
            )
        ).json()["id"]
        async with owner_engine.begin() as conn:
            await conn.execute(
                text("UPDATE conexoes SET permitir_escrita = true WHERE id = CAST(:i AS uuid)"),
                {"i": cid_http},
            )

        # `/maybe` answers 500 when the body contains "bad": row 2 fails on purpose.
        definicao = {
            "nos": [
                {
                    "id": "itens",
                    "tipo": "sql",
                    "sql": (
                        "SELECT * FROM (VALUES ('P-1','good'),('P-2','bad'),('P-3','good')) "
                        "t(pedido, nome)"
                    ),
                },
                {
                    "id": "chamar",
                    "tipo": "http",
                    "config": {
                        "conexao_id": cid_http,
                        "metodo": "POST",
                        "caminho": "/maybe",
                        "modo": "por_linha",
                        "concorrencia": 1,
                        "corpo_tipo": "colunas",
                        "tolerancia_falha": "continuar",
                        "resposta_campos": [{"de": "id", "para": "recibo"}],
                    },
                },
                {
                    "id": "consolidado",
                    "tipo": "sql",
                    "sql": (
                        "SELECT pedido, recibo, _status, "
                        "CASE WHEN _erro IS NULL THEN 'ok' ELSE 'falha' END AS resultado "
                        "FROM chamar ORDER BY pedido"
                    ),
                },
                {
                    "id": "resumo",
                    "tipo": "email",
                    "config": {
                        "conexao_id": cid_smtp,
                        "modo": "unico",
                        "para": DESTINO,
                        "assunto": "Consolidated - {{contagem}} row(s)",
                        "corpo": "<p>Result:</p>{{tabela}}",
                        "anexos": [
                            {"origem": "dados", "formato": "csv", "nome": "consolidado.csv"}
                        ],
                    },
                },
            ],
            "arestas": [
                {"de": "itens", "para": "chamar"},
                {"de": "chamar", "para": "consolidado"},
                {"de": "consolidado", "para": "resumo"},
            ],
        }
        pid = (await cliente.post("/pipelines", json={"nome": "Consolidated"}, headers=h)).json()[
            "id"
        ]
        r = await cliente.put(
            f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
        )
        assert r.status_code in (200, 204), r.text
        pub = await cliente.post(
            f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
        )
        assert pub.status_code in (200, 201), pub.text

        _, res = await _rodar(pid, uid)

        assert len(estado_http.requisicoes) == 3 and estado_http.max_simultaneos == 1
        cols, linhas = _ler_snapshot(res["consolidado"])
        assert cols == ["pedido", "recibo", "_status", "resultado"]
        por_pedido = {lin["pedido"]: lin for lin in linhas}
        assert por_pedido["P-2"]["resultado"] == "falha" and por_pedido["P-2"]["recibo"] is None
        assert por_pedido["P-1"]["resultado"] == "ok" and por_pedido["P-1"]["recibo"] is not None

        # ONE e-mail at the end, with the 3 rows in the table and the consolidated CSV attached
        assert len(estado_smtp.mensagens) == 1
        corpo = _corpo_do_mime(estado_smtp.mensagens[0]["dados"])
        assert "P-1" in corpo and "P-2" in corpo and "P-3" in corpo and "falha" in corpo
        csv_texto = _anexos_do_mime(estado_smtp.mensagens[0]["dados"])["consolidado.csv"].decode(
            "utf-8-sig"
        )
        assert csv_texto.splitlines()[0] == "pedido;recibo;_status;resultado"
        assert len(csv_texto.strip().splitlines()) == 4  # header + 3 rows
    finally:
        srv.shutdown()
        srv.server_close()

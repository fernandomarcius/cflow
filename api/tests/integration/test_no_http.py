"""`http` effect node against a local HTTPServer (thread, 127.0.0.1:0). Covers the 3 modes,
failure tolerance (abortar/continuar), concurrency <= cap, non-JSON response, empty predecessor,
`escrita_externa` audit and, critically, the Bearer being present in the REQUEST and ABSENT from
the snapshot/error/trail. Includes the dry-run preview: 0 requests, payload byte-identical to the
execution one over the same sample.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sqlalchemy import text

from cflow_core.execucao import engine

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration

TOKEN = "tok-secret"


class _Estado:
    def __init__(self) -> None:
        self.requisicoes: list[dict] = []
        self.lock = threading.Lock()
        self.simultaneos = 0
        self.max_simultaneos = 0
        self.contador = 0


def _handler(estado: _Estado, atraso: float):
    class _H(BaseHTTPRequestHandler):
        def _tratar(self):  # shared by all methods
            length = int(self.headers.get("Content-Length") or 0)
            corpo = self.rfile.read(length).decode() if length else ""
            with estado.lock:
                estado.requisicoes.append(
                    {
                        "metodo": self.command,
                        "path": self.path,
                        "corpo": corpo,
                        "auth": self.headers.get("Authorization"),
                    }
                )
                estado.simultaneos += 1
                estado.max_simultaneos = max(estado.max_simultaneos, estado.simultaneos)
            if atraso:
                time.sleep(atraso)
            with estado.lock:
                estado.simultaneos -= 1
                estado.contador += 1
                n = estado.contador
            p = self.path
            if (
                p.startswith("/protegido")
                and self.headers.get("Authorization") != f"Bearer {TOKEN}"
            ):
                self._responder(401, '{"erro":"no token"}')
            elif p.startswith("/erro"):
                self._responder(500, '{"erro":"boom"}')
            elif p.startswith("/maybe"):
                ruim = '"bad"' in corpo
                self._responder(500, '{"erro":"bad"}') if ruim else self._responder(
                    200, json.dumps({"id": n})
                )
            elif p.startswith("/texto"):
                self._responder(200, "this-is-not-json", tipo="text/plain")
            else:  # /ok, /protegido (with token), etc.
                self._responder(200, json.dumps({"id": n}))

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _tratar

        def _responder(self, code: int, corpo: str, tipo: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", tipo)
            self.end_headers()
            self.wfile.write(corpo.encode())

        def log_message(self, *a):  # silence
            pass

    return _H


@pytest.fixture
def servidor():
    estado = _Estado()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(estado, atraso=0.0))
    porta = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield porta, estado
    srv.shutdown()


@pytest.fixture
def servidor_lento():
    """Server that sleeps per request, used only to observe the concurrency cap."""
    estado = _Estado()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(estado, atraso=0.05))
    porta = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield porta, estado
    srv.shutdown()


async def _conexao_http(owner_engine, cliente, h, nome, porta, *, flag=True, segredo=TOKEN):
    config = {"url": f"http://127.0.0.1:{porta}", "auth_tipo": "bearer"}
    cid = (
        await cliente.post(
            "/conexoes",
            json={"nome": nome, "tipo": "api_rest", "config": config, "segredo": segredo},
            headers=h,
        )
    ).json()["id"]
    if flag:
        async with owner_engine.begin() as conn:
            await conn.execute(
                text("UPDATE conexoes SET permitir_escrita = true WHERE id = CAST(:i AS uuid)"),
                {"i": cid},
            )
    return cid


async def _pipeline_http(cliente, h, cid, cfg_http, *, fonte_sql=None, com_fonte=True):
    nos, arestas = [], []
    if com_fonte:
        nos.append(
            {
                "id": "src",
                "tipo": "sql",
                "sql": fonte_sql or "SELECT * FROM (VALUES ('a'),('b')) t(nome)",
            }
        )
        arestas.append({"de": "src", "para": "h"})
    nos.append({"id": "h", "tipo": "http", "config": {"conexao_id": cid, **cfg_http}})
    definicao = {"nos": nos, "arestas": arestas}
    pid = (await cliente.post("/pipelines", json={"nome": "HTTP E2E"}, headers=h)).json()["id"]
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    return pid, definicao


async def _rodar(pid, uid):
    det = await engine.criar_execucao(pipeline_id=uuid.UUID(pid), ator_id=uid)
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


# ---------------- modes ----------------


async def test_http_por_linha_ok(owner_engine, app_engine, cliente, servidor):
    porta, estado = servidor
    uid, h = await usuario_logado(
        owner_engine, cliente, email="http_pl@example.com", perfil="especialista"
    )
    cid = await _conexao_http(owner_engine, cliente, h, "http-pl", porta)
    pid, _ = await _pipeline_http(
        cliente,
        h,
        cid,
        {"metodo": "POST", "caminho": "/ok", "modo": "por_linha", "corpo_tipo": "colunas"},
    )
    _, res = await _rodar(pid, uid)
    assert len(estado.requisicoes) == 2  # 2 rows -> 2 requests
    cols, linhas = _ler_snapshot(res["h"])
    assert "nome" in cols and {"_payload", "_status", "_erro", "_resposta"} <= set(cols)
    assert [lin["nome"] for lin in linhas] == ["a", "b"]
    assert all(lin["_status"] == "200" and lin["_erro"] is None for lin in linhas)
    assert json.loads(linhas[0]["_payload"]) == {"nome": "a"}
    assert "id" in json.loads(linhas[0]["_resposta"])  # raw response preserved


async def test_http_unico(owner_engine, app_engine, cliente, servidor):
    porta, estado = servidor
    uid, h = await usuario_logado(
        owner_engine, cliente, email="http_un@example.com", perfil="especialista"
    )
    cid = await _conexao_http(owner_engine, cliente, h, "http-un", porta)
    pid, _ = await _pipeline_http(
        cliente, h, cid, {"metodo": "POST", "caminho": "/ok", "modo": "unico"}, com_fonte=False
    )
    _, res = await _rodar(pid, uid)
    assert len(estado.requisicoes) == 1
    cols, linhas = _ler_snapshot(res["h"])
    assert "id" in cols and "_status" in cols  # flattened response + _status
    assert linhas[0]["_status"] == "200"


async def test_http_lote(owner_engine, app_engine, cliente, servidor):
    porta, estado = servidor
    uid, h = await usuario_logado(
        owner_engine, cliente, email="http_lt@example.com", perfil="especialista"
    )
    cid = await _conexao_http(owner_engine, cliente, h, "http-lt", porta)
    fonte = "SELECT * FROM (VALUES ('a'),('b'),('c')) t(nome)"
    pid, _ = await _pipeline_http(
        cliente,
        h,
        cid,
        {"metodo": "POST", "caminho": "/ok", "modo": "lote", "lote_tamanho": 2},
        fonte_sql=fonte,
    )
    _, res = await _rodar(pid, uid)
    assert len(estado.requisicoes) == 2  # 3 rows / batch of 2 -> 2 chunks
    assert json.loads(estado.requisicoes[0]["corpo"]) == [{"nome": "a"}, {"nome": "b"}]
    cols, linhas = _ler_snapshot(res["h"])
    assert cols[:2] == ["_chunk", "linhas_enviadas"]
    assert [lin["linhas_enviadas"] for lin in linhas] == ["2", "1"]


async def test_http_concorrencia_respeita_teto(owner_engine, app_engine, cliente, servidor_lento):
    porta, estado = servidor_lento
    uid, h = await usuario_logado(
        owner_engine, cliente, email="http_cc@example.com", perfil="especialista"
    )
    cid = await _conexao_http(owner_engine, cliente, h, "http-cc", porta)
    fonte = "SELECT * FROM (VALUES (1),(2),(3),(4),(5),(6)) t(n)"
    pid, _ = await _pipeline_http(
        cliente,
        h,
        cid,
        {"metodo": "POST", "caminho": "/ok", "modo": "por_linha", "concorrencia": 2},
        fonte_sql=fonte,
    )
    await _rodar(pid, uid)
    assert len(estado.requisicoes) == 6
    assert estado.max_simultaneos <= 2  # semaphore respected


# ---------------- failure tolerance ----------------


async def test_http_abortar_reporta_disparados(owner_engine, app_engine, cliente, servidor):
    porta, estado = servidor
    uid, h = await usuario_logado(
        owner_engine, cliente, email="http_ab@example.com", perfil="especialista"
    )
    cid = await _conexao_http(owner_engine, cliente, h, "http-ab", porta)
    fonte = "SELECT * FROM (VALUES ('a'),('b'),('c')) t(nome)"
    # concorrencia 1 (sequential) + /erro (always 500) + abortar (default): the 1st failure aborts
    # the pending ones; exactly 1 request fired, so the message reports the partial effect.
    pid, _ = await _pipeline_http(
        cliente,
        h,
        cid,
        {"metodo": "POST", "caminho": "/erro", "modo": "por_linha", "concorrencia": 1},
        fonte_sql=fonte,
    )
    with pytest.raises(Exception) as exc:  # noqa: PT011 - ErroAplicacao wrapped as erro_execucao
        await _rodar(pid, uid)
    msg = getattr(exc.value, "mensagem", str(exc.value))
    assert "'h'" in msg and "#0" in msg
    assert len(estado.requisicoes) == 1  # aborted the other 2


async def test_http_continuar_preenche_erro(owner_engine, app_engine, cliente, servidor):
    porta, estado = servidor
    uid, h = await usuario_logado(
        owner_engine, cliente, email="http_ct@example.com", perfil="especialista"
    )
    cid = await _conexao_http(owner_engine, cliente, h, "http-ct", porta)
    fonte = "SELECT * FROM (VALUES ('good'),('bad')) t(nome)"
    pid, _ = await _pipeline_http(
        cliente,
        h,
        cid,
        {
            "metodo": "POST",
            "caminho": "/maybe",
            "modo": "por_linha",
            "tolerancia_falha": "continuar",
        },
        fonte_sql=fonte,
    )
    _, res = await _rodar(pid, uid)  # not 100% failed -> node completes
    _, linhas = _ler_snapshot(res["h"])
    por_nome = {lin["nome"]: lin for lin in linhas}
    assert por_nome["good"]["_status"] == "200" and por_nome["good"]["_erro"] is None
    assert por_nome["bad"]["_status"] == "500" and por_nome["bad"]["_erro"] is not None


async def test_http_resposta_nao_json_unico_falha(owner_engine, app_engine, cliente, servidor):
    porta, _ = servidor
    uid, h = await usuario_logado(
        owner_engine, cliente, email="http_nj@example.com", perfil="especialista"
    )
    cid = await _conexao_http(owner_engine, cliente, h, "http-nj", porta)
    pid, _ = await _pipeline_http(
        cliente, h, cid, {"metodo": "GET", "caminho": "/texto", "modo": "unico"}, com_fonte=False
    )
    with pytest.raises(Exception) as exc:  # noqa: PT011
        await _rodar(pid, uid)
    assert "JSON" in getattr(exc.value, "mensagem", str(exc.value))


async def test_http_por_linha_resposta_crua_preservada(owner_engine, app_engine, cliente, servidor):
    porta, _ = servidor
    uid, h = await usuario_logado(
        owner_engine, cliente, email="http_cr@example.com", perfil="especialista"
    )
    cid = await _conexao_http(owner_engine, cliente, h, "http-cr", porta)
    # por_linha does NOT flatten: a non-JSON response becomes a raw `_resposta` (downstream decides), without failing.
    pid, _ = await _pipeline_http(
        cliente,
        h,
        cid,
        {"metodo": "GET", "caminho": "/texto", "modo": "por_linha"},
        fonte_sql="SELECT 'a' AS nome",
    )
    _, res = await _rodar(pid, uid)
    _, linhas = _ler_snapshot(res["h"])
    assert linhas[0]["_resposta"] == "this-is-not-json" and linhas[0]["_status"] == "200"


async def test_http_predecessor_zero_linhas(owner_engine, app_engine, cliente, servidor):
    porta, estado = servidor
    uid, h = await usuario_logado(
        owner_engine, cliente, email="http_z0@example.com", perfil="especialista"
    )
    cid = await _conexao_http(owner_engine, cliente, h, "http-z0", porta)
    pid, _ = await _pipeline_http(
        cliente,
        h,
        cid,
        {"metodo": "POST", "caminho": "/ok", "modo": "por_linha"},
        fonte_sql="SELECT 'x' AS nome WHERE 1=0",
    )
    _, res = await _rodar(pid, uid)
    assert estado.requisicoes == []  # 0 rows -> 0 requests, no error
    cols, linhas = _ler_snapshot(res["h"])
    assert linhas == [] and "nome" in cols  # empty snapshot, but with a schema


# ---------------- security + audit ----------------


async def test_http_bearer_ausente_de_snapshot_erro_trilha(
    owner_engine, app_engine, cliente, servidor
):
    porta, estado = servidor
    uid, h = await usuario_logado(
        owner_engine, cliente, email="http_sec@example.com", perfil="especialista"
    )
    cid = await _conexao_http(owner_engine, cliente, h, "http-sec", porta)
    pid, definicao = await _pipeline_http(
        cliente, h, cid, {"metodo": "POST", "caminho": "/protegido", "modo": "por_linha"}
    )
    eid, res = await _rodar(pid, uid)
    # the token WAS sent in the header (authenticated) ...
    assert estado.requisicoes[0]["auth"] == f"Bearer {TOKEN}"
    assert all(r["path"].startswith("/protegido") for r in estado.requisicoes)
    # ... and did NOT leak to the snapshot, the trail, the error column or the definition.
    _, linhas = _ler_snapshot(res["h"])
    assert TOKEN not in json.dumps(linhas, default=str)
    assert TOKEN not in json.dumps(definicao)
    async with owner_engine.begin() as conn:
        aud = (
            (
                await conn.execute(
                    text(
                        "SELECT depois FROM auditoria WHERE recurso_tipo = 'escrita_externa' ORDER BY id DESC LIMIT 1"
                    )
                )
            )
            .mappings()
            .first()
        )
        erros = (
            await conn.execute(
                text(
                    "SELECT coalesce(string_agg(coalesce(erro,''), '|'), '') AS e FROM execucao_nos WHERE execucao_id = CAST(:e AS uuid)"
                ),
                {"e": eid},
            )
        ).scalar_one()
    assert aud is not None
    dep = aud["depois"]
    assert dep["metodo"] == "POST" and dep["requisicoes"] == 2 and dep["falhas"] == 0
    assert TOKEN not in json.dumps(dep) and TOKEN not in (erros or "")


# ---------------- dry-run preview ----------------


async def test_http_preview_dry_run_zero_requests(owner_engine, app_engine, cliente, servidor):
    porta, estado = servidor
    uid, h = await usuario_logado(
        owner_engine, cliente, email="http_pv@example.com", perfil="especialista"
    )
    cid = await _conexao_http(owner_engine, cliente, h, "http-pv", porta)
    pid, definicao = await _pipeline_http(
        cliente,
        h,
        cid,
        {"metodo": "POST", "caminho": "/ok", "modo": "por_linha", "corpo_tipo": "colunas"},
    )
    # PREVIEW: no request goes out
    r = await cliente.post(
        f"/pipelines/{pid}/nos/h/preview", json={"definicao": definicao}, headers=h
    )
    assert r.status_code == 200, r.text
    assert estado.requisicoes == []  # the server received nothing
    efeito = r.json()["efeito"]
    assert efeito["tipo"] == "http" and efeito["modo"] == "por_linha" and efeito["metodo"] == "POST"
    assert efeito["contagem_alvo"] == 2 and "Dry-run" in efeito["aviso"]
    assert json.loads(efeito["payloads"][0]) == {"nome": "a"}
    # the preview payload is byte-identical to the execution one over the same sample.
    _, res = await _rodar(pid, uid)
    _, linhas = _ler_snapshot(res["h"])
    assert efeito["payloads"][0] == linhas[0]["_payload"]


async def test_http_preview_dry_run_simula_campos_extraidos(
    owner_engine, app_engine, cliente, servidor
):
    """Dry-run with `resposta_campos`: the extracted columns appear in the simulated schema (from
    `resposta_exemplo`), the same schema execution materializes."""
    porta, estado = servidor
    uid, h = await usuario_logado(
        owner_engine, cliente, email="http_ce@example.com", perfil="especialista"
    )
    cid = await _conexao_http(owner_engine, cliente, h, "http-ce", porta)
    cfg = {
        "metodo": "POST",
        "caminho": "/ok",
        "modo": "por_linha",
        "corpo_tipo": "colunas",
        "resposta_campos": [{"de": "id", "para": "ticket"}],
        "incluir_resposta_completa": False,
        "resposta_exemplo": '{"id": 42}',
    }
    pid, definicao = await _pipeline_http(cliente, h, cid, cfg)
    r = await cliente.post(
        f"/pipelines/{pid}/nos/h/preview", json={"definicao": definicao}, headers=h
    )
    assert r.status_code == 200, r.text
    assert estado.requisicoes == []  # dry-run does not call the API
    corpo = r.json()
    assert corpo["colunas"] == ["nome", "ticket", "_payload", "_status", "_erro"]
    assert [lin["ticket"] for lin in corpo["linhas"]] == ["42", "42"]

    # execution: SAME schema, now with the real API value
    _, res = await _rodar(pid, uid)
    cols, linhas = _ler_snapshot(res["h"])
    assert cols == corpo["colunas"]
    assert sorted(lin["ticket"] for lin in linhas) == ["1", "2"]


async def test_http_campo_de_resposta_em_conflito_recusado(
    owner_engine, app_engine, cliente, servidor
):
    """An extracted column named like an existing column is refused: silently overwriting would
    swap the data sent for what the API answered."""
    porta, _ = servidor
    _, h = await usuario_logado(
        owner_engine, cliente, email="http_cf@example.com", perfil="especialista"
    )
    cid = await _conexao_http(owner_engine, cliente, h, "http-cf", porta)
    cfg = {
        "metodo": "POST",
        "caminho": "/ok",
        "modo": "por_linha",
        "resposta_campos": [{"de": "id", "para": "nome"}],  # `nome` comes from the input node
    }
    pid, definicao = await _pipeline_http(cliente, h, cid, cfg)
    r = await cliente.post(
        f"/pipelines/{pid}/nos/h/preview", json={"definicao": definicao}, headers=h
    )
    assert r.status_code == 400, r.text
    assert "conflicts" in r.text.lower()


async def test_http_campo_de_resposta_reservado_recusado_na_definicao(
    owner_engine, app_engine, cliente, servidor
):
    porta, _ = servidor
    _, h = await usuario_logado(
        owner_engine, cliente, email="http_cr@example.com", perfil="especialista"
    )
    cid = await _conexao_http(owner_engine, cliente, h, "http-cr", porta)
    definicao = {
        "nos": [
            {
                "id": "h",
                "tipo": "http",
                "config": {
                    "conexao_id": cid,
                    "metodo": "POST",
                    "caminho": "/ok",
                    "modo": "por_linha",
                    "resposta_campos": [{"de": "id", "para": "_status"}],
                },
            }
        ],
        "arestas": [],
    }
    novo = await cliente.post("/pipelines", json={"nome": "HTTP reserved"}, headers=h)
    pid = novo.json()["id"]
    r = await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    assert r.status_code == 422, r.text
    assert "_chunk" in r.text

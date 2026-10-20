"""PURE assembly of the http node (no I/O): URL by concatenation, `{{col}}`/`:param` placeholders,
body by mode (unico/por_linha/lote), chunking, and the snapshot schema. Invariant: the payload/URL
assembled here is EXACTLY what the run fires and what the preview shows (same `montar_requisicoes`).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from cflow_core.errors import ErroAplicacao
from cflow_core.execucao.no_http import (
    achatar_unico,
    colunas_snapshot,
    executar_http,
    extrair_valor,
    linha_snapshot,
    montar_requisicoes,
    montar_url,
)

BASE = "https://api.example.com/v2"


# ---------------- URL by concatenation (security: never switches host) ----------------


def test_montar_url_com_e_sem_barra_final():
    assert montar_url("http://x/", "/a/b") == "http://x/a/b"
    assert montar_url("http://x", "/a/b") == "http://x/a/b"


def test_url_valor_com_barra_e_encodado_nao_vira_host():
    # {{p}} = '//evil.com' is URL-encoded (`%2F%2Fevil.com`); the final URL stays on the base host
    # and never becomes protocol-relative //evil.com (token exfiltration). This is the central defence.
    cfg = {"metodo": "GET", "modo": "por_linha", "caminho": "/r/{{p}}"}
    plano = montar_requisicoes(cfg, ["p"], [{"p": "//evil.com"}], None, BASE)
    url = plano.requisicoes[0].url
    assert url == "https://api.example.com/v2/r/%2F%2Fevil.com"
    assert "//evil.com" not in url.split("://", 1)[1]  # no '//' after the scheme


def test_caminho_url_encode_valores():
    cfg = {"metodo": "GET", "modo": "por_linha", "caminho": "/x/{{id}}"}
    plano = montar_requisicoes(cfg, ["id"], [{"id": "a/b c"}], None, BASE)
    assert plano.requisicoes[0].url == "https://api.example.com/v2/x/a%2Fb%20c"


# ---------------- unico mode ----------------


def test_unico_uma_requisicao_so_param():
    # unico has no row: {{col}} vanishes (empty); only :param resolves in the path.
    cfg = {"metodo": "POST", "modo": "unico", "caminho": "/job/:lote", "corpo_tipo": "colunas"}
    plano = montar_requisicoes(cfg, [], [], {"lote": 7}, BASE)
    assert len(plano.requisicoes) == 1
    assert plano.requisicoes[0].url == "https://api.example.com/v2/job/7"
    assert plano.requisicoes[0].corpo is None  # columns without a row: no body


def test_unico_corpo_template_so_param():
    cfg = {
        "metodo": "POST",
        "modo": "unico",
        "caminho": "/x",
        "corpo_tipo": "template",
        "corpo": '{"competencia": :comp}',
    }
    plano = montar_requisicoes(cfg, [], [], {"comp": "2026-07"}, BASE)
    assert plano.requisicoes[0].corpo == {"competencia": "2026-07"}


# ---------------- por_linha mode ----------------


def test_por_linha_n_requisicoes_corpo_colunas():
    cfg = {"metodo": "POST", "modo": "por_linha", "caminho": "/reg", "corpo_tipo": "colunas"}
    linhas = [{"nome": "a", "v": 1}, {"nome": "b", "v": 2}]
    plano = montar_requisicoes(cfg, ["nome", "v"], linhas, None, BASE)
    assert len(plano.requisicoes) == 2
    assert plano.requisicoes[0].corpo == {"nome": "a", "v": 1}
    assert plano.requisicoes[1].corpo == {"nome": "b", "v": 2}
    assert plano.requisicoes[0].indice == 0 and plano.requisicoes[1].indice == 1


def test_por_linha_corpo_campos_de_para():
    cfg = {
        "metodo": "POST",
        "modo": "por_linha",
        "caminho": "/reg",
        "corpo_tipo": "colunas",
        "corpo_campos": [{"de": "emissor", "para": "cod"}, {"de": "v", "para": "valor"}],
    }
    plano = montar_requisicoes(
        cfg, ["emissor", "v"], [{"emissor": "X", "v": 9, "ignorado": "z"}], None, BASE
    )
    assert plano.requisicoes[0].corpo == {"cod": "X", "valor": 9}


def test_por_linha_template_json_encode():
    # {{nome}} is JSON-encoded: the template WITHOUT surrounding quotes becomes valid JSON with quotes.
    cfg = {
        "metodo": "POST",
        "modo": "por_linha",
        "caminho": "/reg",
        "corpo_tipo": "template",
        "corpo": '{"nome": {{nome}}, "ativo": {{ativo}}}',
    }
    plano = montar_requisicoes(cfg, ["nome", "ativo"], [{"nome": 'a"b', "ativo": True}], None, BASE)
    # inner quotes escaped by json.dumps: the body stays valid JSON
    assert plano.requisicoes[0].corpo == {"nome": 'a"b', "ativo": True}


def test_template_param_desconhecido_fica_literal():
    # ':true' inside a JSON must not blindly become 'null' (it would break the body): without the
    # param in the dict, the token stays literal. Only known params are substituted.
    cfg = {
        "metodo": "POST",
        "modo": "unico",
        "caminho": "/x",
        "corpo_tipo": "template",
        "corpo": '{"ativo": true, "n": :n}',
    }
    plano = montar_requisicoes(cfg, [], [], {"n": 5}, BASE)
    assert plano.requisicoes[0].corpo == {"ativo": True, "n": 5}


def test_template_invalido_falha():
    cfg = {
        "metodo": "POST",
        "modo": "unico",
        "caminho": "/x",
        "corpo_tipo": "template",
        "corpo": '{"nome": {{nome}}',  # the brace never closes
    }
    with pytest.raises(ErroAplicacao) as exc:
        montar_requisicoes(cfg, [], [], None, BASE)
    assert exc.value.codigo == "http_corpo_invalido"


# ---------------- lote mode (chunking) ----------------


def test_lote_chunking_e_array():
    cfg = {
        "metodo": "POST",
        "modo": "lote",
        "caminho": "/bulk",
        "lote_tamanho": 2,
        "corpo_tipo": "colunas",
    }
    linhas = [{"n": i} for i in range(5)]
    plano = montar_requisicoes(cfg, ["n"], linhas, None, BASE)
    assert len(plano.requisicoes) == 3  # 2 + 2 + 1
    assert plano.requisicoes[0].corpo == [{"n": 0}, {"n": 1}]
    assert plano.requisicoes[2].corpo == [{"n": 4}]
    assert [r.linhas_enviadas for r in plano.requisicoes] == [2, 2, 1]
    assert [r.indice for r in plano.requisicoes] == [0, 1, 2]


def test_lote_zero_linhas_zero_requisicoes():
    cfg = {"metodo": "POST", "modo": "lote", "caminho": "/bulk", "lote_tamanho": 100}
    plano = montar_requisicoes(cfg, ["n"], [], None, BASE)
    assert plano.requisicoes == []


def test_lote_mesclar_campo_vira_objeto_unico_com_lista_concatenada():
    """lote + coluna_json + lote_mesclar_campo: the chunk becomes ONE object (not an array),
    concatenating the field's list (e.g. `items`); scalars come from the 1st body."""
    cfg = {
        "metodo": "POST",
        "modo": "lote",
        "caminho": "/api/v1/Sales/CreateInvoice",
        "corpo_tipo": "coluna_json",
        "corpo_coluna": "body",
        "lote_mesclar_campo": "items",
        "lote_tamanho": 2,
    }
    linhas = [
        {"body": {"items": [{"item_no": 10}], "billing_mode": 2}},
        {"body": {"items": [{"item_no": 20}], "billing_mode": 2}},
        {"body": {"items": [{"item_no": 30}, {"item_no": 31}], "billing_mode": 2}},
    ]
    plano = montar_requisicoes(cfg, ["body"], linhas, None, BASE)
    assert len(plano.requisicoes) == 2  # chunks of 2
    c0 = plano.requisicoes[0].corpo
    assert isinstance(c0, dict)  # object, not array
    assert [p["item_no"] for p in c0["items"]] == [10, 20]  # concatenated 2 sales
    assert c0["billing_mode"] == 2  # scalar from the 1st body
    assert [p["item_no"] for p in plano.requisicoes[1].corpo["items"]] == [30, 31]


def test_lote_sem_mesclar_campo_continua_array():
    """Backward compatibility: without lote_mesclar_campo, the batch stays an ARRAY (original behaviour)."""
    cfg = {
        "metodo": "POST",
        "modo": "lote",
        "caminho": "/bulk",
        "lote_tamanho": 10,
        "corpo_tipo": "coluna_json",
        "corpo_coluna": "body",
    }
    linhas = [{"body": {"items": [{"x": 1}]}}, {"body": {"items": [{"x": 2}]}}]
    plano = montar_requisicoes(cfg, ["body"], linhas, None, BASE)
    assert isinstance(plano.requisicoes[0].corpo, list)
    assert len(plano.requisicoes[0].corpo) == 2


# ---------------- plan: metadata ----------------


def test_plano_carrega_metadados():
    cfg = {
        "metodo": "PUT",
        "modo": "por_linha",
        "caminho": "/r",
        "concorrencia": 4,
        "taxa_por_segundo": 10,
        "resposta_json_path": "data",
    }
    plano = montar_requisicoes(cfg, ["a"], [{"a": 1}], None, BASE)
    assert plano.metodo == "PUT" and plano.concorrencia == 4
    assert plano.taxa_por_segundo == 10.0 and plano.resposta_json_path == "data"
    assert (
        plano.url_destino == "https://api.example.com/v2/r"
    )  # template path (literal placeholders)


# ---------------- snapshot: schema per mode ----------------


def test_colunas_snapshot_por_linha_e_lote():
    assert colunas_snapshot("por_linha", ["a", "b"]) == [
        "a",
        "b",
        "_payload",
        "_status",
        "_erro",
        "_resposta",
    ]
    assert colunas_snapshot("lote", ["a"]) == [
        "_chunk",
        "linhas_enviadas",
        "_payload",
        "_status",
        "_erro",
        "_resposta",
    ]


def test_linha_snapshot_por_linha_carrega_colunas_e_meta():
    cfg = {"metodo": "POST", "modo": "por_linha", "caminho": "/r", "corpo_tipo": "colunas"}
    plano = montar_requisicoes(cfg, ["nome"], [{"nome": "a"}], None, BASE)
    res = {"status": 201, "resposta": '{"id": 9}', "erro": None}
    lin = linha_snapshot("por_linha", plano.requisicoes[0], res, ["nome"])
    assert lin["nome"] == "a" and lin["_status"] == 201 and lin["_erro"] is None
    assert json.loads(lin["_payload"]) == {"nome": "a"} and lin["_resposta"] == '{"id": 9}'


def test_achatar_unico_objeto_e_array():
    # object -> 1 row + _status; json_path navigates to the list
    cols, linhas = achatar_unico('{"id": 123}', 200, None)
    assert linhas == [{"id": 123, "_status": 200}] and "id" in cols
    cols2, linhas2 = achatar_unico('{"data": [{"x": 1}, {"x": 2}]}', 200, "data")
    assert [r["x"] for r in linhas2] == [1, 2] and all(r["_status"] == 200 for r in linhas2)


def test_achatar_unico_nao_json_falha():
    with pytest.raises(ErroAplicacao) as exc:
        achatar_unico("<html>error</html>", 200, None)
    assert exc.value.codigo == "http_resposta_nao_json"


# ---------------- "coluna_json" body (raw JSON from ONE column) ----------------


def test_coluna_json_string_vira_json():
    # column `body` arrives as a JSON STRING: it is parsed (json.loads), not re-encoded.
    cfg = {
        "metodo": "POST",
        "modo": "por_linha",
        "caminho": "/r",
        "corpo_tipo": "coluna_json",
        "corpo_coluna": "body",
    }
    linha = {"id": 1, "body": '{"a": 1, "b": [2, 3]}'}
    plano = montar_requisicoes(cfg, ["id", "body"], [linha], None, BASE)
    assert plano.requisicoes[0].corpo == {"a": 1, "b": [2, 3]}


def test_coluna_json_objeto_vai_como_esta():
    # Postgres jsonb arrives as dict/list: used verbatim, untouched.
    cfg = {
        "metodo": "POST",
        "modo": "por_linha",
        "caminho": "/r",
        "corpo_tipo": "coluna_json",
        "corpo_coluna": "body",
    }
    linha = {"id": 1, "body": {"a": 1, "lista": [1, 2]}}
    plano = montar_requisicoes(cfg, ["id", "body"], [linha], None, BASE)
    assert plano.requisicoes[0].corpo == {"a": 1, "lista": [1, 2]}


def test_coluna_json_invalido_falha():
    cfg = {
        "metodo": "POST",
        "modo": "por_linha",
        "caminho": "/r",
        "corpo_tipo": "coluna_json",
        "corpo_coluna": "body",
    }
    with pytest.raises(ErroAplicacao) as exc:
        montar_requisicoes(cfg, ["body"], [{"body": "this is not json {"}], None, BASE)
    assert exc.value.codigo == "http_corpo_invalido"


def test_coluna_json_lote_array_de_valores():
    cfg = {
        "metodo": "POST",
        "modo": "lote",
        "caminho": "/bulk",
        "lote_tamanho": 2,
        "corpo_tipo": "coluna_json",
        "corpo_coluna": "body",
    }
    linhas = [{"body": '{"n": 1}'}, {"body": {"n": 2}}]
    plano = montar_requisicoes(cfg, ["body"], linhas, None, BASE)
    assert plano.requisicoes[0].corpo == [{"n": 1}, {"n": 2}]


# ---------------- per-request dynamic headers ----------------


def test_headers_extra_resolve_col_e_param_por_requisicao():
    # {{col}} = row value; :param = pipeline parameter; both WITHOUT url-encoding (literal).
    cfg = {
        "metodo": "POST",
        "modo": "por_linha",
        "caminho": "/r",
        "corpo_tipo": "colunas",
        "headers_extra": [
            {"nome": "Authorization", "valor": "Bearer :token"},
            {"nome": "X-Tenant", "valor": "{{tenant}}"},
        ],
    }
    plano = montar_requisicoes(cfg, ["tenant"], [{"tenant": "acme/2"}], {"token": "T0KEN"}, BASE)
    hdr = plano.requisicoes[0].headers
    assert hdr["Authorization"] == "Bearer T0KEN"
    assert hdr["X-Tenant"] == "acme/2"  # no %2F: literal header value


def test_headers_extra_formato_objeto():
    cfg = {
        "metodo": "GET",
        "modo": "unico",
        "caminho": "/x",
        "headers_extra": {"X-Api-Version": "2", "X-Env": ":env"},
    }
    plano = montar_requisicoes(cfg, [], [], {"env": "prod"}, BASE)
    assert plano.requisicoes[0].headers == {"X-Api-Version": "2", "X-Env": "prod"}


def test_sem_headers_extra_fica_vazio_compat():
    cfg = {"metodo": "POST", "modo": "por_linha", "caminho": "/r"}
    plano = montar_requisicoes(cfg, ["a"], [{"a": 1}], None, BASE)
    assert plano.requisicoes[0].headers == {}


def test_token_de_header_dinamico_nao_vaza_no_snapshot():
    # SECURITY: the resolved value of a dynamic header (token) must NEVER enter the node snapshot
    # (_payload/_status/_erro/_resposta/predecessor columns).
    cfg = {
        "metodo": "POST",
        "modo": "por_linha",
        "caminho": "/r",
        "corpo_tipo": "colunas",
        "headers_extra": [{"nome": "Authorization", "valor": "Bearer :token"}],
    }
    plano = montar_requisicoes(cfg, ["nome"], [{"nome": "a"}], {"token": "SECRET123"}, BASE)
    req = plano.requisicoes[0]
    assert req.headers["Authorization"] == "Bearer SECRET123"  # it is in the request
    res = {"status": 201, "resposta": '{"id": 9}', "erro": None}
    lin = linha_snapshot("por_linha", req, res, ["nome"])
    # the token appears in NO field of the serialized snapshot
    assert "SECRET123" not in json.dumps(lin)


def test_executar_http_mescla_headers_estatico_e_dinamico_sem_vazar(monkeypatch):
    # Proves the real MERGE in executar_http (connection's static headers + dynamic on top) and that
    # the token does not leak into the result. Uses MockTransport (no network) injected in AsyncClient.
    import httpx

    capturado: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        capturado["headers"] = dict(request.headers)
        return httpx.Response(200, json={"ok": True})

    transporte = httpx.MockTransport(handler)
    orig = httpx.AsyncClient

    def fake_client(*a, **k):
        k["transport"] = transporte
        return orig(*a, **k)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)

    config = {"headers": {"X-Base": "b"}, "auth_tipo": "bearer"}  # connection header + Bearer
    cfg = {
        "metodo": "POST",
        "modo": "por_linha",
        "caminho": "/r",
        "corpo_tipo": "colunas",
        "headers_extra": [
            {"nome": "X-Row", "valor": "{{id}}"},
            {"nome": "X-Base", "valor": "overridden"},  # dynamic merges ON TOP of static
        ],
    }
    plano = montar_requisicoes(cfg, ["id"], [{"id": "abc"}], {}, BASE)
    resultados, disparados = asyncio.run(
        executar_http(config, "TOKENCX", plano, timeout=5, concorrencia_max=4, tolerancia="abortar")
    )
    assert disparados == 1
    h = capturado["headers"]
    assert h["authorization"] == "Bearer TOKENCX"  # connection's static header preserved
    assert h["x-row"] == "abc"  # dynamic {{col}} resolved
    assert h["x-base"] == "overridden"  # dynamic won over static
    # not even the connection token leaks into the result (only status/response/error)
    assert "TOKENCX" not in json.dumps(resultados)


def test_lote_headers_extra_resolve_da_primeira_linha_do_chunk():
    """Batch mode resolves headers_extra from the chunk's 1st row (not None), which allows
    Authorization={{token}} per batch (same token on every row, via cross join with auth)."""
    cfg = {
        "metodo": "POST",
        "modo": "lote",
        "caminho": "/gera",
        "lote_tamanho": 2,
        "corpo_tipo": "coluna_json",
        "corpo_coluna": "body",
        "lote_mesclar_campo": "items",
        "headers_extra": [{"nome": "Authorization", "valor": "{{token}}"}],
    }
    linhas = [
        {"token": "BEARER-X", "body": {"items": [{"p": 1}]}},
        {"token": "BEARER-X", "body": {"items": [{"p": 2}]}},
    ]
    plano = montar_requisicoes(cfg, ["token", "body"], linhas, None, BASE)
    assert plano.requisicoes[0].headers.get("Authorization") == "BEARER-X"


def test_lote_chave_nenhuma_chunk_cruza_o_valor_da_chave():
    """lote_chave: rows grouped by key, chunked INSIDE the group; no chunk mixes key values (even
    with unordered input). Each merged body inherits the scalar of its group's 1st body (e.g.
    billing_mode), so nothing leaks across companies."""
    cfg = {
        "metodo": "POST",
        "modo": "lote",
        "caminho": "/gera",
        "lote_tamanho": 50,
        "corpo_tipo": "coluna_json",
        "corpo_coluna": "body",
        "lote_mesclar_campo": "items",
        "lote_chave": "company",
    }
    linhas = [
        {"company": 7, "body": {"items": [{"item_no": 1}], "billing_mode": 2}},
        {"company": 76, "body": {"items": [{"item_no": 9}], "billing_mode": 3}},
        {"company": 7, "body": {"items": [{"item_no": 2}], "billing_mode": 2}},
    ]
    plano = montar_requisicoes(cfg, ["company", "body"], linhas, None, BASE)
    assert len(plano.requisicoes) == 2  # 1 chunk for company 7 (2 rows), 1 for company 76
    c_emp7 = plano.requisicoes[0].corpo
    assert c_emp7["billing_mode"] == 2
    assert [p["item_no"] for p in c_emp7["items"]] == [
        1,
        2,
    ]  # only company 7, nothing from company 76
    c_emp76 = plano.requisicoes[1].corpo
    assert c_emp76["billing_mode"] == 3 and [p["item_no"] for p in c_emp76["items"]] == [9]


def test_lote_chave_respeita_lote_tamanho_dentro_do_grupo():
    cfg = {
        "metodo": "POST",
        "modo": "lote",
        "caminho": "/g",
        "lote_tamanho": 2,
        "corpo_tipo": "coluna_json",
        "corpo_coluna": "body",
        "lote_chave": "emp",
    }
    linhas = [{"emp": 1, "body": {"items": [{"n": i}]}} for i in range(5)]  # 1 group, 5 rows
    plano = montar_requisicoes(cfg, ["emp", "body"], linhas, None, BASE)
    assert [r.linhas_enviadas for r in plano.requisicoes] == [
        2,
        2,
        1,
    ]  # chunked by 2 inside the group


# ---------------- value extraction from the response (por_linha/lote) ----------------


def test_extrair_valor_navega_objeto_lista_e_indice():
    corpo = {"dados": [{"numero": "0001", "nested": {"ok": True}}], "total": 1}
    assert extrair_valor(corpo, "total") == 1
    assert extrair_valor(corpo, "dados.0.numero") == "0001"
    assert extrair_valor(corpo, "dados.0.nested.ok") is True


def test_extrair_valor_caminho_inexistente_e_nulo():
    corpo = {"a": {"b": 1}}
    assert extrair_valor(corpo, "a.z") is None
    assert extrair_valor(corpo, "a.b.c") is None  # traverses a scalar
    assert extrair_valor(corpo, "lista.0") is None
    assert extrair_valor(None, "a") is None


def test_extrair_valor_nao_escalar_vira_json_texto():
    # object/array keep their structure as TEXT so downstream can still parse it
    # (str(dict) with single quotes would not be valid JSON).
    assert json.loads(extrair_valor({"a": {"b": 1}}, "a")) == {"b": 1}
    assert json.loads(extrair_valor({"a": [1, 2]}, "a")) == [1, 2]


def test_colunas_snapshot_com_campos_extraidos():
    campos = (("dados.0.numero", "invoice_no"), ("mensagem", "msg"))
    assert colunas_snapshot("por_linha", ["a"], campos) == [
        "a",
        "invoice_no",
        "msg",
        "_payload",
        "_status",
        "_erro",
        "_resposta",
    ]
    # without the full response: `_resposta` leaves the schema (lean snapshot)
    assert colunas_snapshot("por_linha", ["a"], campos, False) == [
        "a",
        "invoice_no",
        "msg",
        "_payload",
        "_status",
        "_erro",
    ]
    assert colunas_snapshot("lote", ["a"], campos) == [
        "_chunk",
        "linhas_enviadas",
        "invoice_no",
        "msg",
        "_payload",
        "_status",
        "_erro",
        "_resposta",
    ]


def test_linha_snapshot_extrai_campos_da_resposta():
    cfg = {
        "metodo": "POST",
        "modo": "por_linha",
        "caminho": "/r",
        "corpo_tipo": "colunas",
        "resposta_campos": [
            {"de": "dados.0.numero", "para": "invoice_no"},
            {"de": "erro", "para": "msg"},
        ],
    }
    plano = montar_requisicoes(cfg, ["nome"], [{"nome": "a"}], None, BASE)
    res = {"status": 201, "resposta": '{"dados": [{"numero": "0001"}]}', "erro": None}
    lin = linha_snapshot(
        "por_linha",
        plano.requisicoes[0],
        res,
        ["nome"],
        plano.resposta_campos,
        plano.incluir_resposta_completa,
    )
    assert lin["nome"] == "a" and lin["invoice_no"] == "0001"
    assert lin["msg"] is None  # missing path = empty, without breaking the row
    assert lin["_resposta"] == '{"dados": [{"numero": "0001"}]}'


def test_linha_snapshot_resposta_nao_json_deixa_campos_vazios():
    # It is precisely on failure that the API answers HTML/text; the row must still get a `_status`.
    cfg = {
        "metodo": "POST",
        "modo": "por_linha",
        "caminho": "/r",
        "resposta_campos": [{"de": "numero", "para": "invoice_no"}],
    }
    plano = montar_requisicoes(cfg, ["nome"], [{"nome": "a"}], None, BASE)
    res = {"status": 500, "resposta": "<html>error</html>", "erro": "HTTP 500 response"}
    lin = linha_snapshot(
        "por_linha", plano.requisicoes[0], res, ["nome"], plano.resposta_campos, True
    )
    assert (
        lin["invoice_no"] is None and lin["_status"] == 500 and lin["_erro"] == "HTTP 500 response"
    )


def test_linha_snapshot_sem_resposta_completa_omite_coluna():
    cfg = {
        "metodo": "POST",
        "modo": "por_linha",
        "caminho": "/r",
        "resposta_campos": [{"de": "numero", "para": "invoice_no"}],
        "incluir_resposta_completa": False,
    }
    plano = montar_requisicoes(cfg, ["nome"], [{"nome": "a"}], None, BASE)
    res = {"status": 200, "resposta": '{"numero": "77"}', "erro": None}
    lin = linha_snapshot(
        "por_linha",
        plano.requisicoes[0],
        res,
        ["nome"],
        plano.resposta_campos,
        plano.incluir_resposta_completa,
    )
    assert lin["invoice_no"] == "77" and "_resposta" not in lin


def test_campo_de_resposta_em_conflito_falha_na_montagem():
    # Silently overwriting the INPUT column would replace what was sent with what came back.
    cfg = {
        "metodo": "POST",
        "modo": "por_linha",
        "caminho": "/r",
        "resposta_campos": [{"de": "x", "para": "nome"}],
    }
    with pytest.raises(ErroAplicacao) as exc:
        montar_requisicoes(cfg, ["nome"], [{"nome": "a"}], None, BASE)
    assert exc.value.codigo == "http_resposta_campo_conflito"


def test_campo_de_resposta_com_nome_reservado_falha():
    cfg = {
        "metodo": "POST",
        "modo": "por_linha",
        "caminho": "/r",
        "resposta_campos": [{"de": "x", "para": "_status"}],
    }
    with pytest.raises(ErroAplicacao) as exc:
        montar_requisicoes(cfg, ["nome"], [{"nome": "a"}], None, BASE)
    assert exc.value.codigo == "http_resposta_campo_conflito"

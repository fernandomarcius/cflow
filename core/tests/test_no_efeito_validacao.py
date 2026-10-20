"""Structural validation of EFFECT nodes (escrita_sql / http). Pure logic, no database.

Focus: only the STRUCTURE of the definition. The `permitir_escrita` flag and connection
compatibility are checked at publish time (`validar_conexoes_efeito`), not here.
"""

from __future__ import annotations

import pytest

from cflow_core.errors import ErroAplicacao
from cflow_core.pipelines.definicao import validar


def _grafo(config: dict, tipo: str) -> dict:
    # 1 isolated node (no edges): confines the errors to the effect node's config.
    return {"nos": [{"id": "n1", "tipo": tipo, "config": config}], "arestas": []}


def _erro_de(definicao) -> ErroAplicacao:
    with pytest.raises(ErroAplicacao) as exc:
        validar(definicao)
    assert exc.value.codigo == "grafo_invalido"
    return exc.value


# ---------------- escrita_sql ----------------


def test_escrita_tabela_valida_passa():
    validar(
        _grafo(
            {
                "conexao_id": "c1",
                "modo": "tabela",
                "schema": "public",
                "tabela": "alvo",
                "estrategia": "upsert",
                "chave": ["id"],
                "mapeamento": [{"de": "x", "para": "y"}],
            },
            "escrita_sql",
        )
    )


def test_escrita_sql_valida_passa():
    validar(
        _grafo(
            {"conexao_id": "c1", "modo": "sql", "sql": "INSERT INTO alvo SELECT * FROM _entrada"},
            "escrita_sql",
        )
    )


def test_escrita_sem_conexao_falha():
    e = _erro_de(_grafo({"modo": "tabela", "schema": "public", "tabela": "a"}, "escrita_sql"))
    assert any("conexao_id" in d.mensagem for d in e.detalhes)


def test_escrita_modo_invalido_falha():
    e = _erro_de(_grafo({"conexao_id": "c1", "modo": "merge"}, "escrita_sql"))
    assert any("invalid modo 'merge'" in d.mensagem for d in e.detalhes)


@pytest.mark.parametrize("ident", ["public.alvo", "1tabela", "a-b", "drop table x", ""])
def test_escrita_identificador_invalido_falha(ident):
    e = _erro_de(
        _grafo(
            {"conexao_id": "c1", "modo": "tabela", "schema": "public", "tabela": ident},
            "escrita_sql",
        )
    )
    assert any("simple SQL identifier" in d.mensagem for d in e.detalhes)


def test_escrita_upsert_sem_chave_falha():
    e = _erro_de(
        _grafo(
            {
                "conexao_id": "c1",
                "modo": "tabela",
                "schema": "public",
                "tabela": "a",
                "estrategia": "upsert",
            },
            "escrita_sql",
        )
    )
    assert any("requires config.chave" in d.mensagem for d in e.detalhes)


def test_escrita_estrategia_invalida_falha():
    e = _erro_de(
        _grafo(
            {
                "conexao_id": "c1",
                "modo": "tabela",
                "schema": "public",
                "tabela": "a",
                "estrategia": "delete",
            },
            "escrita_sql",
        )
    )
    assert any("invalid estrategia 'delete'" in d.mensagem for d in e.detalhes)


def test_escrita_chave_identificador_invalido_falha():
    e = _erro_de(
        _grafo(
            {
                "conexao_id": "c1",
                "modo": "tabela",
                "schema": "public",
                "tabela": "a",
                "estrategia": "upsert",
                "chave": ["id; drop"],
            },
            "escrita_sql",
        )
    )
    assert any("is not a simple SQL identifier" in d.mensagem for d in e.detalhes)


def test_escrita_mapeamento_para_invalido_falha():
    e = _erro_de(
        _grafo(
            {
                "conexao_id": "c1",
                "modo": "tabela",
                "schema": "public",
                "tabela": "a",
                "mapeamento": [{"de": "x", "para": "y-z"}],
            },
            "escrita_sql",
        )
    )
    assert any("mapeamento with invalid 'para'" in d.mensagem for d in e.detalhes)


@pytest.mark.parametrize("mapa", [5, True, "abc", {"de": "x", "para": "y"}])
def test_escrita_mapeamento_nao_lista_falha_sem_crash(mapa):
    # non-list mapeamento (including a truthy non-iterable) -> structural 422, NEVER TypeError/500.
    cfg = {"conexao_id": "c1", "modo": "tabela", "schema": "s", "tabela": "a", "mapeamento": mapa}
    e = _erro_de(_grafo(cfg, "escrita_sql"))
    assert any("mapeamento" in d.mensagem for d in e.detalhes)


def test_escrita_sql_ponto_e_virgula_em_literal_ok():
    # ';' INSIDE a string literal is data, not a separator: a valid single statement.
    validar(
        _grafo(
            {"conexao_id": "c1", "modo": "sql", "sql": "INSERT INTO t(msg) VALUES ('a;b')"},
            "escrita_sql",
        )
    )


def test_escrita_sql_degenerado_falha():
    # Only ';'/spaces = an empty DML statement -> rejected.
    e = _erro_de(_grafo({"conexao_id": "c1", "modo": "sql", "sql": " ; "}, "escrita_sql"))
    assert any("empty DML statement" in d.mensagem for d in e.detalhes)


def test_escrita_sql_multi_statement_falha():
    e = _erro_de(
        _grafo(
            {"conexao_id": "c1", "modo": "sql", "sql": "DELETE FROM a; DROP TABLE b"}, "escrita_sql"
        )
    )
    assert any("only one statement" in d.mensagem for d in e.detalhes)


def test_escrita_sql_terminador_final_ok():
    # A `;` only at the end (terminator) is accepted: it is not an inner statement.
    validar(_grafo({"conexao_id": "c1", "modo": "sql", "sql": "DELETE FROM a;"}, "escrita_sql"))


def test_escrita_sql_vazio_falha():
    e = _erro_de(_grafo({"conexao_id": "c1", "modo": "sql", "sql": "   "}, "escrita_sql"))
    assert any("requires config.sql" in d.mensagem for d in e.detalhes)


# ---------------- http ----------------


def test_http_valido_passa():
    validar(
        _grafo(
            {
                "conexao_id": "c1",
                "metodo": "POST",
                "caminho": "/v1/records",
                "modo": "por_linha",
                "concorrencia": 4,
                "taxa_por_segundo": 10,
                "timeout_segundos": 30,
                "resposta_exemplo": '{"id": 1}',
            },
            "http",
        )
    )


def test_http_metodo_invalido_falha():
    e = _erro_de(
        _grafo({"conexao_id": "c1", "metodo": "TRACE", "caminho": "/x", "modo": "unico"}, "http")
    )
    assert any("invalid metodo 'TRACE'" in d.mensagem for d in e.detalhes)


def test_http_modo_invalido_falha():
    e = _erro_de(
        _grafo({"conexao_id": "c1", "metodo": "GET", "caminho": "/x", "modo": "batch"}, "http")
    )
    assert any("invalid modo 'batch'" in d.mensagem for d in e.detalhes)


@pytest.mark.parametrize("caminho", ["v1/x", "//other-host/x", "https://evil/x", ""])
def test_http_caminho_nao_relativo_falha(caminho):
    # Anti-exfiltration: '//host' and absolute URLs are rejected; only a relative path starting with '/'.
    e = _erro_de(
        _grafo({"conexao_id": "c1", "metodo": "GET", "caminho": caminho, "modo": "unico"}, "http")
    )
    assert any("must be relative" in d.mensagem for d in e.detalhes)


@pytest.mark.parametrize(
    "campo,valor",
    [
        ("concorrencia", 0),
        ("concorrencia", 17),
        ("lote_tamanho", 0),
        ("lote_tamanho", 10001),
        ("timeout_segundos", 0),
        ("timeout_segundos", 601),
    ],
)
def test_http_bounds_falha(campo, valor):
    cfg = {"conexao_id": "c1", "metodo": "GET", "caminho": "/x", "modo": "unico", campo: valor}
    e = _erro_de(_grafo(cfg, "http"))
    assert any(campo in d.mensagem and "between" in d.mensagem for d in e.detalhes)


@pytest.mark.parametrize("taxa", [0, -1, -0.5])
def test_http_taxa_nao_positiva_falha(taxa):
    e = _erro_de(
        _grafo(
            {
                "conexao_id": "c1",
                "metodo": "GET",
                "caminho": "/x",
                "modo": "unico",
                "taxa_por_segundo": taxa,
            },
            "http",
        )
    )
    assert any("taxa_por_segundo" in d.mensagem for d in e.detalhes)


def test_http_resposta_exemplo_invalida_falha():
    e = _erro_de(
        _grafo(
            {
                "conexao_id": "c1",
                "metodo": "GET",
                "caminho": "/x",
                "modo": "unico",
                "resposta_exemplo": "{not json",
            },
            "http",
        )
    )
    assert any("not parseable JSON" in d.mensagem for d in e.detalhes)


def test_http_sem_conexao_falha():
    e = _erro_de(_grafo({"metodo": "GET", "caminho": "/x", "modo": "unico"}, "http"))
    assert any("conexao_id" in d.mensagem for d in e.detalhes)


# ---------------- grupo_serial (serial chain per group) ----------------

_HTTP_OK = {"conexao_id": "c1", "metodo": "GET", "caminho": "/x", "modo": "unico"}


def test_grupo_serial_em_no_de_efeito_passa():
    validar(_grafo({**_HTTP_OK, "grupo_serial": "db_b1"}, "http"))


def test_grupo_serial_em_saida_arquivo_passa_e_em_saida_lake_falha():
    validar(
        {
            "nos": [{"id": "s", "tipo": "saida", "config": {"nome": "x", "grupo_serial": "g"}}],
            "arestas": [],
        }
    )
    e = _erro_de(
        {
            "nos": [
                {
                    "id": "s",
                    "tipo": "saida",
                    "config": {"destino": "lake", "dataset": "d", "grupo_serial": "g"},
                }
            ],
            "arestas": [],
        }
    )
    assert any("grupo_serial" in d.mensagem for d in e.detalhes)


@pytest.mark.parametrize("valor", ["", "   ", ["b1"], 1, {"g": 1}, None])
def test_grupo_serial_que_nao_e_texto_nao_vazio_falha(valor):
    # `["b1"]` would become a queue named "['b1']"; `""` and `None` look declared but are not.
    e = _erro_de(_grafo({**_HTTP_OK, "grupo_serial": valor}, "http"))
    assert any("grupo_serial" in d.mensagem for d in e.detalhes)


@pytest.mark.parametrize("tipo", ["sql", "fonte", "python", "comparacao"])
def test_grupo_serial_em_no_que_nao_e_serial_falha(tipo):
    # A config that is silently accepted and has no effect is worse than a rejected one: the node
    # would look queued in a queue it never joins.
    base = (
        {"codigo": "x"} if tipo == "python" else {"chave": ["id"]} if tipo == "comparacao" else {}
    )
    e = _erro_de(_grafo({**base, "grupo_serial": "g"}, tipo))
    assert any("grupo_serial" in d.mensagem for d in e.detalhes)
